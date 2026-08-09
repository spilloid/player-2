"""Dataset-integrity hardening, from the Units 5+6 adversarial review.

Every test here defends the same property: a recorded session must never look usable while
being causally impossible. These failures do not raise, do not crash, and do not show up in
a green suite -- they show up a year later as a policy that learned from a timeline with
invisible holes and impossible orderings in it.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from player2.agent.loop import AgentLoop
from player2.clock import ManualClock, SessionClock
from player2.contracts import NEUTRAL, ActionChunk, Frame, Keyframe
from player2.control.null import NullControllerAdapter
from player2.control.scheduler import Scheduler
from player2.record.writer import RecordingOutput, SessionRecorder, load_session
from player2.video.fake import FakeVideoSource


def a_frame(seq: int, session_ms: float = 0.0) -> Frame:
    return Frame(seq=seq, session_ms=session_ms, source_ms=None, width=8, height=8,
                 pixel_format="BGRA8", data=np.zeros((8, 8, 4), dtype=np.uint8))


def rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class TestLossIsAlwaysAttributable:
    def test_drops_are_counted_per_stream(self, tmp_path: Path) -> None:
        """One aggregate counter cannot say whether a missing item was a frame, an executed
        pad state, or a takeover event -- and therefore cannot say which interval of the
        session must be excluded from training."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), queue_size=2)
        recorder.start()
        for i in range(3000):
            recorder.record_pad(float(i), NEUTRAL)
            recorder.record_frame(a_frame(i, float(i)))
        recorder.stop()
        counts = json.loads((recorder.directory / "manifest.json").read_text())["counts"]
        assert "dropped_pad" in counts and "dropped_frames" in counts

    def test_drop_counts_survive_a_crash(self, tmp_path: Path) -> None:
        """Counts held only in memory until stop() are gone in exactly the situation the
        crash manifest exists to describe."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), queue_size=2)
        recorder.start()
        try:
            for i in range(5000):
                recorder.record_pad(float(i), NEUTRAL)
            deadline = time.time() + 5.0
            while time.time() < deadline:
                counts = json.loads((recorder.directory / "manifest.json").read_text())["counts"]
                if any(v for k, v in counts.items() if k.startswith("dropped")):
                    return
                time.sleep(0.05)
            pytest.fail("drop counts were never checkpointed to the durable manifest")
        finally:
            recorder.stop()

    def test_a_session_that_lost_data_is_not_marked_clean(self, tmp_path: Path) -> None:
        """clean_shutdown must mean 'this recording is complete', not merely 'stop() ran'."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), queue_size=2)
        recorder.start()
        for i in range(5000):
            recorder.record_pad(float(i), NEUTRAL)
        recorder.stop()
        manifest = json.loads((recorder.directory / "manifest.json").read_text())
        assert manifest["clean_shutdown"] is False or not any(
            v for k, v in manifest["counts"].items() if k.startswith("dropped")
        )


class TestFrameGapsAtTheEdges:
    def test_a_gap_before_the_first_recorded_frame_is_reported(self, tmp_path: Path) -> None:
        """Capture starts before recording attaches, so the first frame the recorder sees is
        rarely seq 0. Starting silently at seq 17 hides seventeen missing observations."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        recorder.record_frame(a_frame(17, 500.0))
        recorder.stop()
        gaps = [e for e in rows(recorder.directory / "events.jsonl") if e["kind"] == "frame_gap"]
        assert gaps, "frames 0..16 vanished without a gap record"


class TestHostileClockAtTheRecordingBoundary:
    def test_a_bad_clock_cannot_write_impossible_pad_timestamps(self, tmp_path: Path) -> None:
        """RecordingOutput runs on the 120Hz scheduler thread and reads a user-supplied
        clock. NaN serialises as non-standard JSON; a backwards value silently reverses pad
        chronology, which is the one ordering the dataset depends on."""
        class BadClock:
            def __init__(self) -> None:
                self.values = [100.0, float("nan"), 90.0, 110.0]
                self.i = 0

            def now_ms(self) -> float:
                value = self.values[min(self.i, len(self.values) - 1)]
                self.i += 1
                return value

        recorder = SessionRecorder(root=tmp_path, clock=SessionClock())
        recorder.start()
        out = RecordingOutput(inner=NullControllerAdapter(), recorder=recorder,
                              clock=BadClock())
        for _ in range(4):
            out.set_state(NEUTRAL)
        recorder.stop()
        stamps = [float(r["session_ms"]) for r in rows(recorder.directory / "pad.jsonl")]
        assert all(s == s for s in stamps), "NaN reached the recording"
        assert stamps == sorted(stamps), f"pad chronology reversed: {stamps}"

    def test_a_raising_clock_does_not_break_the_controller(self, tmp_path: Path) -> None:
        class RaisingClock:
            def now_ms(self) -> float:
                raise RuntimeError("clock died")

        inner = NullControllerAdapter()
        recorder = SessionRecorder(root=tmp_path, clock=SessionClock())
        recorder.start()
        out = RecordingOutput(inner=inner, recorder=recorder, clock=RaisingClock())
        out.set_state(NEUTRAL)
        recorder.stop()
        assert inner.states, "a broken clock cost the agent its hands"


class TestReplayRecoversWhatItCan:
    def test_a_torn_row_does_not_hide_the_rows_after_it(self, tmp_path: Path) -> None:
        """A transient disk-full can tear one row and then valid rows follow. Stopping at
        the first damaged line discards everything after it -- data that is right there."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        recorder.record_pad(0.0, NEUTRAL)
        recorder.stop()
        with (recorder.directory / "pad.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"session_ms": 1.0, "left_st\n')
            handle.write(json.dumps({"session_ms": 2.0, "left_stick": [0.0, 0.0],
                                     "right_stick": [0.0, 0.0], "left_trigger": 0.0,
                                     "right_trigger": 0.0, "buttons": []}) + "\n")
        session = load_session(recorder.directory)
        assert len(session.pad) == 2, "recoverable rows after the torn one were discarded"
        assert session.truncated is True


class TestManifestProvenanceIsImmutable:
    def test_metadata_is_deep_copied(self, tmp_path: Path) -> None:
        """Otherwise the final manifest can disagree with the crash marker written at start,
        and neither is identifiable as the wrong one."""
        metadata = {"config": {"version": 1}}
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), metadata=metadata)
        recorder.start()
        metadata["config"]["version"] = 99
        recorder.stop()
        written = json.loads((recorder.directory / "manifest.json").read_text())
        assert written["metadata"]["config"]["version"] == 1


class TestAgentLoopSurvivesAndReports:
    def _loop(self, policy: object, clock: object | None = None) -> tuple[AgentLoop,
                                                                          Scheduler,
                                                                          FakeVideoSource]:
        the_clock = clock if clock is not None else SessionClock()
        scheduler = Scheduler(output=NullControllerAdapter(), clock=the_clock,  # type: ignore[arg-type]
                              max_hold_ms=250.0)
        video = FakeVideoSource(clock=the_clock)  # type: ignore[arg-type]
        video.start()
        video.emit(2)
        loop = AgentLoop(policy=policy, video=video, scheduler=scheduler,  # type: ignore[arg-type]
                         clock=the_clock, goal=None)  # type: ignore[arg-type]
        return loop, scheduler, video

    def test_a_hung_policy_does_not_stop_events_being_drained(self) -> None:
        """The loop is the SOLE drainer of scheduler events, and it was calling the policy
        on the same thread. A model that hangs therefore also stopped takeovers, deadman
        fires and rejections from ever reaching the recorder -- they accumulate in a bounded
        deque and the oldest are overwritten unseen. Coupling the only witness to the
        slowest component was the design error."""
        class HungPolicy:
            def propose(self, observation: object) -> None:
                time.sleep(30.0)

        loop, scheduler, _video = self._loop(HungPolicy())
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and loop.stats.events_seen == 0:
                scheduler.take_controller()
                scheduler.return_controller()
                time.sleep(0.02)
            assert loop.stats.events_seen > 0, "a hung policy blocked the event drain"
        finally:
            loop.stop()

    def test_stop_returns_even_if_the_policy_never_does(self) -> None:
        """An unbounded join means one stuck model response prevents shutdown forever."""
        class HungPolicy:
            def propose(self, observation: object) -> None:
                time.sleep(30.0)

        loop, _scheduler, _video = self._loop(HungPolicy())
        loop.start()
        time.sleep(0.2)
        done = threading.Event()
        threading.Thread(target=lambda: (loop.stop(), done.set()), daemon=True).start()
        assert done.wait(10.0), "stop() hung waiting on the policy"

    def test_a_dying_thread_is_reported_rather_than_silent(self) -> None:
        """`except Exception` misses SystemExit. The daemon thread ends with no stored
        error, no event, no counter -- the agent simply stops thinking and nothing says so."""
        class ExitingPolicy:
            def propose(self, observation: object) -> None:
                raise SystemExit("bye")

        loop, _scheduler, _video = self._loop(ExitingPolicy())
        loop.start()
        deadline = time.time() + 3.0
        while time.time() < deadline and loop.is_running:
            time.sleep(0.02)
        loop.stop()
        assert loop.stats.last_error is not None, "the loop died without reporting why"

    def test_decision_sequence_continues_past_a_previous_loop(self) -> None:
        """A replacement loop starting at zero against a scheduler whose last accepted
        sequence was high has every proposal silently rejected as stale."""
        clock = SessionClock()
        scheduler = Scheduler(output=NullControllerAdapter(), clock=clock, max_hold_ms=250.0)
        scheduler.submit(ActionChunk(
            keyframes=(Keyframe(t_ms=0.0), Keyframe(t_ms=50.0)), decision_seq=10_000))
        video = FakeVideoSource(clock=clock)
        video.start()
        video.emit(2)

        class Always:
            def propose(self, observation: object) -> ActionChunk:
                return ActionChunk(keyframes=(Keyframe(t_ms=0.0), Keyframe(t_ms=50.0)))

        loop = AgentLoop(policy=Always(), video=video, scheduler=scheduler, clock=clock)
        loop.start()
        try:
            deadline = time.time() + 3.0
            while time.time() < deadline and loop.stats.chunks_accepted == 0:
                time.sleep(0.02)
        finally:
            loop.stop()
        assert loop.stats.chunks_accepted >= 1, (
            "a fresh loop's proposals were all rejected as stale against an existing scheduler"
        )
