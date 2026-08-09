"""Tick-driven safety boundary between hostile action proposals and a live controller."""

from __future__ import annotations

import math
import threading
from collections import deque
from dataclasses import dataclass
from enum import StrEnum

from player2.clock import Clock
from player2.contracts import (
    NEUTRAL,
    TICK_MS,
    ActionChunk,
    InvalidChunk,
    PadState,
    ResolvedKeyframe,
    resolve,
    sample,
)
from player2.control.base import IControllerOutput


class EventKind(StrEnum):
    """Describe control-boundary state changes for the session recorder."""

    REJECTED = "rejected"
    PREEMPTED = "preempted"
    DEADMAN = "deadman"
    TAKEOVER = "takeover"
    RETURNED = "returned"


@dataclass(frozen=True)
class SchedulerEvent:
    """Record a meaningful scheduler decision with its session-relative time."""

    kind: EventKind
    session_ms: float
    detail: str


@dataclass(frozen=True)
class _AcceptedChunk:
    """Keep resolved input and its acceptance-time anchor together."""

    resolved: tuple[ResolvedKeyframe, ...]
    accepted_at_ms: float
    deadline_ms: float
    decision_seq: int


class Scheduler:
    """Advance controller state once per caller-provided tick, never sleeping or threading.

    State changes are separate from device writes so a wedged driver cannot block the human's
    takeover or shutdown path and leave a game character holding an input.
    """

    def __init__(self, output: IControllerOutput, clock: Clock, max_hold_ms: float) -> None:
        """Create the state machine with a finite non-negative deadman extension."""
        if not isinstance(max_hold_ms, (int, float)) or isinstance(max_hold_ms, bool):
            raise ValueError("max_hold_ms must be a finite non-negative number")
        self._max_hold_ms = float(max_hold_ms)
        if not math.isfinite(self._max_hold_ms) or self._max_hold_ms < 0.0:
            raise ValueError("max_hold_ms must be a finite non-negative number")
        self._output = output
        self._clock = clock
        self._lock = threading.Lock()
        self._io_lock = threading.Lock()
        self._pending: _AcceptedChunk | None = None
        self._running: _AcceptedChunk | None = None
        self._events: deque[SchedulerEvent] = deque(maxlen=4096)
        self._last_decision_seq = -1
        self._epoch = 0
        self._taken_over = False
        self._closing = False
        self._closed = False
        self._max_lateness_ms = 0.0

    @property
    def clock(self) -> Clock:
        """Return the sole time source so cadence and deadman cannot disagree in a game."""
        return self._clock

    @property
    def epoch(self) -> int:
        """Return the epoch that new proposals must carry to be considered current."""
        with self._lock:
            return self._epoch

    @property
    def max_lateness_ms(self) -> float:
        """Return the greatest non-negative lateness observed by the thread wrapper."""
        with self._lock:
            return self._max_lateness_ms

    def submit(self, chunk: object) -> bool:
        """Validate and queue a proposal, never allowing hostile input to escape.

        Resolving first and retaining only one queued command prevents malformed or chatty model
        output from stopping the process that is responsible for releasing a physical control.
        """
        try:
            if type(chunk) is not ActionChunk:
                raise InvalidChunk("expected an action chunk")
            resolved = resolve(chunk)
            duration_ms = resolved[-1].t_ms
            with self._lock:
                accepted_at_ms = self._now_ms()
                if self._closed or self._closing:
                    self._event(EventKind.REJECTED, accepted_at_ms, "scheduler is closed")
                    return False
                if self._taken_over:
                    self._event(EventKind.REJECTED, accepted_at_ms, "controller is taken over")
                    return False
                if chunk.epoch != self._epoch:
                    self._event(EventKind.REJECTED, accepted_at_ms, "chunk epoch is stale")
                    return False
                if chunk.decision_seq <= self._last_decision_seq:
                    self._event(EventKind.REJECTED, accepted_at_ms, "decision sequence is stale")
                    return False
                if self._pending is not None:
                    self._event(
                        EventKind.PREEMPTED,
                        accepted_at_ms,
                        "decision "
                        f"{self._pending.decision_seq} replaced by {chunk.decision_seq}",
                    )
                self._last_decision_seq = chunk.decision_seq
                self._pending = _AcceptedChunk(
                    resolved=resolved,
                    accepted_at_ms=accepted_at_ms,
                    deadline_ms=accepted_at_ms + duration_ms + self._max_hold_ms,
                    decision_seq=chunk.decision_seq,
                )
                return True
        except Exception as error:
            self._reject_invalid(error)
            return False

    def tick(self) -> None:
        """Calculate a safe report under the state lock, then write it after releasing it.

        A driver can block inside ``set_state``; holding only the I/O lock there preserves an
        immediate takeover path instead of trapping a character with a stick held down.
        """
        try:
            with self._lock:
                now_ms = self._now_ms()
                if self._pending is not None:
                    if self._running is not None:
                        self._event(
                            EventKind.PREEMPTED,
                            now_ms,
                            "decision "
                            f"{self._running.decision_seq} replaced by "
                            f"{self._pending.decision_seq}",
                        )
                    self._running = self._pending
                    self._pending = None
                state = self._state_at(now_ms)
        except Exception:
            state = NEUTRAL
        try:
            self._write(state)
        finally:
            self._write_deferred_neutral()

    def take_controller(self) -> None:
        """Give the human control and request a neutral report without waiting on a wedged I/O.

        A blocked device write must not prevent a player from invalidating stale model actions;
        the releasing report is deferred behind that write only when serialization requires it.
        """
        with self._lock:
            if self._taken_over or self._closed or self._closing:
                return
            self._epoch += 1
            self._taken_over = True
            self._running = None
            self._pending = None
            self._event(EventKind.TAKEOVER, self._event_time(), "human took controller")
        self._try_write(NEUTRAL)

    def return_controller(self) -> None:
        """Return control with a new epoch so decisions from human play cannot reactivate input."""
        with self._lock:
            if not self._taken_over or self._closed or self._closing:
                return
            self._epoch += 1
            self._taken_over = False
            self._event(EventKind.RETURNED, self._event_time(), "human returned controller")

    def close(self) -> None:
        """Release synchronously when possible and keep retrying until a neutral write succeeds.

        Marking shutdown complete before a release succeeds would make a transient driver error
        permanently strand a held input, so failure leaves this method safe to call again.
        """
        with self._lock:
            if self._closed:
                return
            self._closing = True
            self._running = None
            self._pending = None
        if not self._try_write(NEUTRAL):
            return
        with self._lock:
            self._closed = True
            self._closing = False

    def drain_events(self) -> list[SchedulerEvent]:
        """Return and clear recorder events atomically with respect to scheduler changes."""
        with self._lock:
            events = list(self._events)
            self._events.clear()
            return events

    def record_lateness(self, lateness_ms: float) -> None:
        """Store observed loop lateness without letting a negative clock value reduce it."""
        with self._lock:
            self._max_lateness_ms = max(self._max_lateness_ms, max(0.0, lateness_ms))

    def _state_at(self, now_ms: float) -> PadState:
        """Compute a safe complete state, emitting one event when a command expires."""
        if self._closed or self._closing or self._taken_over or self._running is None:
            return NEUTRAL
        running = self._running
        if now_ms > running.deadline_ms:
            self._running = None
            self._event(EventKind.DEADMAN, now_ms, "accepted command exceeded deadman deadline")
            return NEUTRAL
        return sample(running.resolved, now_ms - running.accepted_at_ms)

    def _write(self, state: PadState) -> None:
        """Serialize one physical report while deliberately holding no scheduler state lock."""
        with self._io_lock:
            self._output.set_state(state)

    def _try_write(self, state: PadState) -> bool:
        """Write now unless another driver call is wedged; never block a release escape hatch."""
        if not self._io_lock.acquire(blocking=False):
            return False
        try:
            try:
                self._output.set_state(state)
            except Exception:
                try:
                    self._output.reset()
                except Exception:
                    return False
            return True
        finally:
            self._io_lock.release()

    def _write_deferred_neutral(self) -> None:
        """Release after an in-flight report if takeover or close changed state during that I/O."""
        with self._lock:
            needs_neutral = self._taken_over or self._closing or self._closed
        if needs_neutral:
            self._try_write(NEUTRAL)

    def _now_ms(self) -> float:
        """Read finite time or fail safe, because NaN can otherwise disable the deadman forever."""
        value = self._clock.now_ms()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock value must be a finite number")
        now_ms = float(value)
        if not math.isfinite(now_ms):
            raise ValueError("clock value must be a finite number")
        return now_ms

    def _event_time(self) -> float:
        """Use a harmless timestamp if the untrusted clock has already failed."""
        try:
            return self._now_ms()
        except Exception:
            return 0.0

    def _event(self, kind: EventKind, session_ms: float, detail: str) -> None:
        """Append a bounded event while the caller holds the scheduler state lock."""
        self._events.append(SchedulerEvent(kind, session_ms, detail))

    def _reject_invalid(self, error: Exception) -> None:
        """Record malformed input safely; rejection must never disturb a live command."""
        session_ms = self._event_time()
        with self._lock:
            self._event(EventKind.REJECTED, session_ms, str(error) or "invalid chunk")


class SchedulerThread:
    """Run a scheduler at absolute deadlines, skipping late periods instead of bursting.

    Skipping missed deadlines prevents stale game actions being replayed rapidly after a stall,
    which could otherwise turn a brief controller hold into unintended movement.
    """

    def __init__(self, scheduler: Scheduler, period_ms: float = TICK_MS) -> None:
        """Prepare a stopped daemon thread wrapper around an already-created scheduler."""
        if not isinstance(period_ms, (int, float)) or isinstance(period_ms, bool):
            raise ValueError("period_ms must be a positive finite number")
        self._period_ms = float(period_ms)
        if not math.isfinite(self._period_ms) or self._period_ms <= 0.0:
            raise ValueError("period_ms must be a positive finite number")
        self._scheduler = scheduler
        self._clock = scheduler.clock
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    @property
    def clock(self) -> Clock:
        """Return the scheduler's clock so loop cadence uses the same safety deadlines."""
        return self._clock

    def start(self) -> None:
        """Start the loop once; repeated starts while running are harmless."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop one captured worker and release before joining, so a blocked tick has an escape."""
        with self._thread_lock:
            self._stop_event.set()
            thread = self._thread
            self._scheduler.close()
            if thread is not None and thread is not threading.current_thread():
                thread.join()

    def is_alive(self) -> bool:
        """Return whether the wrapper's worker is currently running."""
        with self._thread_lock:
            return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        """Tick at start plus whole periods, always releasing input even on BaseException."""
        try:
            start_ms = self._clock.now_ms()
            deadline_ms = start_ms
            while not self._stop_event.is_set():
                now_ms = self._clock.now_ms()
                remaining_ms = deadline_ms - now_ms
                if remaining_ms > 0.0:
                    self._stop_event.wait(remaining_ms / 1000.0)
                    continue
                self._scheduler.record_lateness(-remaining_ms)
                self._scheduler.tick()
                now_ms = self._clock.now_ms()
                periods = max(1, math.floor((now_ms - start_ms) / self._period_ms) + 1)
                deadline_ms = start_ms + periods * self._period_ms
        except BaseException:
            # The finally below is the safety boundary: a SystemExit from a device driver must
            # not be allowed to leave a virtual stick held merely because it is not Exception.
            pass
        finally:
            self._scheduler.close()
