"""Threaded perception-to-intent loop that never writes to a controller directly."""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from typing import Protocol

from player2.agent.base import IFastPolicy, Observation
from player2.clock import Clock
from player2.contracts import ActionChunk, Frame
from player2.control.scheduler import Scheduler, SchedulerEvent
from player2.video.base import IVideoSource

_DEFAULT_MIN_INTERVAL_MS = 50.0
_DEFAULT_FRAMES_PER_OBSERVATION = 4
_HISTORY_CAPACITY = 256
_IDLE_POLL_SECONDS = 0.01
_STOP_JOIN_SECONDS = 2.0
_STOP_HANDOFF_SECONDS = 0.05


class _Recorder(Protocol):
    """Describe only the non-blocking recorder calls owned by the agent seam."""

    def record_frame(self, frame: Frame) -> None:
        """Queue one captured frame without waiting for disk I/O."""
        ...

    def record_chunk(self, session_ms: float, chunk: ActionChunk) -> None:
        """Queue one scheduler-accepted proposal with its session timestamp."""
        ...

    def record_event(self, kind: str, session_ms: float, detail: str) -> None:
        """Queue one scheduler lifecycle event without draining the scheduler itself."""
        ...


@dataclass(frozen=True)
class AgentStats:
    """Expose stable cognition accounting without leaking mutable thread state."""

    chunks_proposed: int
    chunks_accepted: int
    chunks_rejected: int
    policy_errors: int
    events_seen: int
    last_error: str | None


class AgentLoop:
    """Run policy work beside the scheduler and preserve executed-event provenance.

    The scheduler remains the sole controller writer. This thread only submits intent, so a
    slow or malformed model response cannot delay a 120 Hz device report.
    """

    def __init__(
        self,
        *,
        policy: IFastPolicy,
        video: IVideoSource,
        scheduler: Scheduler,
        clock: Clock,
        goal: str | None = None,
        recorder: _Recorder | None = None,
        min_interval_ms: float = _DEFAULT_MIN_INTERVAL_MS,
        frames_per_observation: int = _DEFAULT_FRAMES_PER_OBSERVATION,
    ) -> None:
        """Prepare an idle loop with bounded history and caller-owned dependencies."""
        if isinstance(min_interval_ms, bool) or not isinstance(min_interval_ms, (int, float)):
            raise ValueError("min_interval_ms must be a positive finite number")
        interval_ms = float(min_interval_ms)
        if not math.isfinite(interval_ms) or interval_ms <= 0.0:
            raise ValueError("min_interval_ms must be a positive finite number")
        if (isinstance(frames_per_observation, bool)
                or not isinstance(frames_per_observation, int)
                or frames_per_observation <= 0):
            raise ValueError("frames_per_observation must be a positive integer")
        if goal is not None and not isinstance(goal, str):
            raise ValueError("goal must be a string or None")

        self._policy = policy
        self._video = video
        self._scheduler = scheduler
        self._clock = clock
        self._goal = goal
        self._recorder = recorder
        self._min_interval_ms = interval_ms
        self._frames_per_observation = frames_per_observation
        self._history: deque[SchedulerEvent] = deque(maxlen=_HISTORY_CAPACITY)
        self._history_lock = threading.Lock()
        self._next_decision_seq = self._scheduler_next_decision_seq()
        self._last_recorded_frame_seq: int | None = None

        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._event_thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._stats_lock = threading.Lock()
        self._chunks_proposed = 0
        self._chunks_accepted = 0
        self._chunks_rejected = 0
        self._policy_errors = 0
        self._events_seen = 0
        self._last_error: str | None = None

    def start(self) -> None:
        """Start cognition and one dedicated event drainer without duplicate witnesses."""
        with self._thread_lock:
            workers = (self._thread, self._event_thread)
            if any(worker is not None and worker.is_alive() for worker in workers):
                return
            self._stop_event.clear()
            self._next_decision_seq = self._scheduler_next_decision_seq()
            with self._stats_lock:
                self._last_error = None
            self._event_thread = threading.Thread(target=self._run_events, daemon=True)
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._event_thread.start()
            self._thread.start()

    def stop(self) -> None:
        """Stop idempotently within a bounded wait even when policy work never returns."""
        with self._thread_lock:
            self._stop_event.set()
            workers = (self._thread, self._event_thread)
        with self._stats_lock:
            admitted = self._chunks_accepted > 0
        if admitted:
            # Admission remains reported immediately. This bounded shutdown handoff only gives
            # an already-running scheduler one report opportunity before it is closed.
            time.sleep(_STOP_HANDOFF_SECONDS)
        deadline = time.monotonic() + _STOP_JOIN_SECONDS
        for worker in workers:
            if worker is None or worker is threading.current_thread():
                continue
            worker.join(max(0.0, deadline - time.monotonic()))

    @property
    def is_running(self) -> bool:
        """Report worker liveness instead of a flag that could survive a thread failure."""
        with self._thread_lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def stats(self) -> AgentStats:
        """Copy counters under one lock so callers never observe a torn accounting update."""
        with self._stats_lock:
            return AgentStats(
                chunks_proposed=self._chunks_proposed,
                chunks_accepted=self._chunks_accepted,
                chunks_rejected=self._chunks_rejected,
                policy_errors=self._policy_errors,
                events_seen=self._events_seen,
                last_error=self._last_error,
            )

    def _run(self) -> None:
        """Poll frames and propose at bounded cadence while remaining interruptible."""
        next_decision_ms: float | None = None
        try:
            while not self._stop_event.is_set():
                frames = self._video.latest(self._frames_per_observation)
                self._record_new_frames(frames)
                if not frames:
                    self._stop_event.wait(_IDLE_POLL_SECONDS)
                    continue

                now_ms = self._now_ms()
                if next_decision_ms is not None and now_ms < next_decision_ms:
                    remaining_seconds = (next_decision_ms - now_ms) / 1000.0
                    self._stop_event.wait(min(_IDLE_POLL_SECONDS, remaining_seconds))
                    continue

                next_decision_ms = now_ms + self._min_interval_ms
                self._decide(frames, now_ms)
        except BaseException as error:
            self._set_last_error(error)

    def _run_events(self) -> None:
        """Drain scheduler evidence independently so a hung policy cannot overwrite it."""
        try:
            while not self._stop_event.is_set():
                self._drain_events()
                self._stop_event.wait(_IDLE_POLL_SECONDS)
        except BaseException as error:
            self._set_last_error(error)
        finally:
            try:
                self._drain_events()
            except BaseException as error:
                self._set_last_error(error)

    def _decide(self, frames: tuple[Frame, ...], now_ms: float) -> None:
        """Stamp one causal observation before slow policy work can make its epoch stale."""
        cutoff_ms = max(frame.session_ms for frame in frames)
        decision_seq = self._next_decision_seq
        self._next_decision_seq += 1
        epoch = self._scheduler.epoch
        with self._history_lock:
            history = tuple(self._history)
        observation = Observation(
            frames=frames,
            goal=self._goal,
            history=history,
            observation_cutoff_ms=cutoff_ms,
            deadline_ms=now_ms + self._min_interval_ms,
            epoch=epoch,
            decision_seq=decision_seq,
        )
        try:
            proposal = self._policy.propose(observation)
        except Exception:
            self._increment("policy_errors")
            return
        if proposal is None or self._stop_event.is_set():
            return

        self._increment("chunks_proposed")
        try:
            stamped = replace(
                proposal,
                decision_seq=decision_seq,
                epoch=epoch,
                observation_cutoff_ms=cutoff_ms,
            )
        except Exception:
            self._increment("chunks_rejected")
            return

        # This is the acceptance instant, sampled before submit. Sampling after submit can
        # place a stale-epoch chunk after a takeover that happened in the intervening tick.
        accepted_at_ms = self._now_ms()
        accepted = self._scheduler.submit(stamped)
        if not accepted:
            self._increment("chunks_rejected")
            return
        self._record_chunk(accepted_at_ms, stamped)
        # This counter describes scheduler admission, not eventual device execution. The pad
        # stream and scheduler events carry the separate evidence of what actually happened.
        self._increment("chunks_accepted")

    def _drain_events(self) -> None:
        """Drain once and fan out, preventing split execution history between consumers."""
        events = self._scheduler.drain_events()
        if not events:
            return
        with self._history_lock:
            self._history.extend(events)
        with self._stats_lock:
            self._events_seen += len(events)
        recorder = self._recorder
        if recorder is None:
            return
        for event in events:
            try:
                recorder.record_event(event.kind.value, event.session_ms, event.detail)
            except Exception:
                # Recording is subordinate to control; a broken log must not kill cognition.
                continue

    def _record_new_frames(self, frames: tuple[Frame, ...]) -> None:
        """Forward each sequence once so repeated observations do not duplicate training data."""
        recorder = self._recorder
        if recorder is None:
            return
        for frame in frames:
            previous = self._last_recorded_frame_seq
            if previous is not None and frame.seq <= previous:
                continue
            self._last_recorded_frame_seq = frame.seq
            try:
                recorder.record_frame(frame)
            except Exception:
                # The sequence remains consumed: retrying could create ambiguous duplicates.
                continue

    def _record_chunk(self, session_ms: float, chunk: ActionChunk) -> None:
        """Record only accepted intent so rejected proposals cannot masquerade as actions."""
        recorder = self._recorder
        if recorder is None:
            return
        try:
            recorder.record_chunk(session_ms, chunk)
        except Exception:
            # Controller acceptance already happened and must never be rolled back for logging.
            pass

    def _increment(self, counter: str) -> None:
        """Update one known counter under lock so snapshots remain internally consistent."""
        with self._stats_lock:
            if counter == "chunks_proposed":
                self._chunks_proposed += 1
            elif counter == "chunks_accepted":
                self._chunks_accepted += 1
            elif counter == "chunks_rejected":
                self._chunks_rejected += 1
            elif counter == "policy_errors":
                self._policy_errors += 1

    def _set_last_error(self, error: BaseException) -> None:
        """Expose daemon-worker death because silent cognition loss leaves no safe witness."""
        detail = str(error) or type(error).__name__
        with self._stats_lock:
            self._last_error = detail

    def _scheduler_next_decision_seq(self) -> int:
        """Continue the scheduler sequence so a replacement loop is not stale on arrival.

        Scheduler intentionally owns this value. Its current implementation does not expose a
        public read-only accessor, so this compatibility read is kept isolated until it does.
        """
        value: object = getattr(self._scheduler, "_last_decision_seq", -1)
        if isinstance(value, int) and not isinstance(value, bool) and value >= -1:
            return value + 1
        return 0

    def _now_ms(self) -> float:
        """Reject invalid session time before it can corrupt observation ordering."""
        value = self._clock.now_ms()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock value must be a finite number")
        now_ms = float(value)
        if not math.isfinite(now_ms):
            raise ValueError("clock value must be a finite number")
        return now_ms
