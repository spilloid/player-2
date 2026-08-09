"""Contract for player2.record -- turning a play session into training data.

This is the module the whole project is secretly for. Everything else can be rewritten;
recordings cannot be re-recorded, because the session that produced them is gone. So the
bias here is different from everywhere else in the runtime: prefer recording MORE, prefer
recording uncertainty explicitly, and never let a producer thread wait on a disk.

Three rules carried over from the architecture review:

  * Record what the device ACTUALLY DID, not what the model proposed. Proposals get
    preempted, rejected, clamped, deadman'd, and overridden by a human. Training against
    proposals teaches actions the game never received.
  * Gaps must be explicit. "Unknown missing interval" is unrecoverable a year later;
    "labelled damage" is merely inconvenient.
  * A recorder that blocks its producers is worse than one that drops. The producers are a
    120Hz scheduler that must never stall and a graphics callback that must never stall.

Executed pad states are captured with a decorator around the controller output rather than
by changing the scheduler, so the recording boundary sits exactly where the bytes leave.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest

from player2.clock import ManualClock
from player2.contracts import NEUTRAL, ActionChunk, Button, Frame, Keyframe, PadState
from player2.control.null import NullControllerAdapter
from player2.record.writer import RecordingOutput, SessionRecorder, load_session


def a_frame(seq: int, session_ms: float) -> Frame:
    buffer = np.zeros((8, 8, 4), dtype=np.uint8)
    buffer[:, :] = (seq % 256, 0, 0, 255)
    return Frame(seq=seq, session_ms=session_ms, source_ms=session_ms + 1.0,
                 width=8, height=8, pixel_format="BGRA8", data=buffer)


def a_chunk(seq: int = 0) -> ActionChunk:
    return ActionChunk(
        keyframes=(Keyframe(t_ms=0.0, left_stick=(1.0, 0.0)), Keyframe(t_ms=100.0)),
        decision_seq=seq,
    )


@pytest.fixture
def rec(tmp_path: Path) -> SessionRecorder:
    return SessionRecorder(root=tmp_path, clock=ManualClock(),
                           metadata={"game": "example", "profile": "example.toml"})


class TestSessionLayout:
    def test_start_creates_a_session_directory(self, rec: SessionRecorder) -> None:
        rec.start()
        rec.stop()
        assert rec.directory.is_dir()

    def test_writes_a_manifest(self, rec: SessionRecorder) -> None:
        rec.start()
        rec.stop()
        assert (rec.directory / "manifest.json").is_file()

    def test_sessions_do_not_collide(self, tmp_path: Path) -> None:
        first = SessionRecorder(root=tmp_path, clock=ManualClock())
        second = SessionRecorder(root=tmp_path, clock=ManualClock())
        first.start()
        first.stop()
        second.start()
        second.stop()
        assert first.directory != second.directory


class TestManifest:
    def test_carries_schema_and_provenance(self, rec: SessionRecorder) -> None:
        """A year from now the code will have moved on. A recording that cannot say which
        schema and which runtime produced it is a puzzle, not a dataset."""
        rec.start()
        rec.stop()
        manifest = json.loads((rec.directory / "manifest.json").read_text())
        assert isinstance(manifest["schema_version"], int)
        assert "runtime_revision" in manifest
        assert "started_wall" in manifest

    def test_carries_opaque_metadata_without_interpreting_it(self, rec: SessionRecorder) -> None:
        """Game name, profile, resolution and bindings are all useful to record and none of
        them are things the runtime is allowed to understand."""
        rec.start()
        rec.stop()
        manifest = json.loads((rec.directory / "manifest.json").read_text())
        assert manifest["metadata"]["game"] == "example"

    def test_marks_a_clean_shutdown(self, rec: SessionRecorder) -> None:
        rec.start()
        rec.stop()
        assert json.loads((rec.directory / "manifest.json").read_text())["clean_shutdown"] is True

    def test_is_written_at_start_so_a_crash_still_leaves_one(self, rec: SessionRecorder) -> None:
        """If the process dies mid-session the manifest must already exist and must NOT
        claim a clean shutdown. A recording that lies about its own completeness is worse
        than one that is obviously truncated."""
        rec.start()
        try:
            manifest = json.loads((rec.directory / "manifest.json").read_text())
            assert manifest["clean_shutdown"] is False
        finally:
            rec.stop()

    def test_records_final_counts(self, rec: SessionRecorder) -> None:
        rec.start()
        rec.record_pad(0.0, NEUTRAL)
        rec.record_frame(a_frame(0, 0.0))
        rec.stop()
        counts = json.loads((rec.directory / "manifest.json").read_text())["counts"]
        assert counts["pad"] >= 1 and counts["frames"] >= 1


class TestStreams:
    def test_records_executed_pad_states(self, rec: SessionRecorder) -> None:
        rec.start()
        rec.record_pad(0.0, NEUTRAL)
        rec.record_pad(8.3, PadState(left_stick=(1.0, 0.0), buttons=frozenset({Button.A})))
        rec.stop()
        rows = [json.loads(line) for line in
                (rec.directory / "pad.jsonl").read_text().splitlines() if line.strip()]
        assert len(rows) == 2
        assert rows[1]["session_ms"] == pytest.approx(8.3)
        assert rows[1]["buttons"] == ["A"]

    def test_records_proposed_chunks_separately_from_executed_states(
        self, rec: SessionRecorder
    ) -> None:
        """Two different streams because they are two different facts. A chunk is what the
        model asked for; the pad stream is what the game could actually have received."""
        rec.start()
        rec.record_chunk(0.0, a_chunk(seq=1))
        rec.record_pad(0.0, NEUTRAL)
        rec.stop()
        assert (rec.directory / "chunks.jsonl").read_text().strip()
        assert (rec.directory / "pad.jsonl").read_text().strip()

    def test_records_lifecycle_events(self, rec: SessionRecorder) -> None:
        rec.start()
        rec.record_event("deadman", 250.0, "command exceeded deadline")
        rec.stop()
        rows = [json.loads(line) for line in
                (rec.directory / "events.jsonl").read_text().splitlines() if line.strip()]
        assert rows[0]["kind"] == "deadman"

    def test_streams_are_line_delimited_json(self, rec: SessionRecorder) -> None:
        """Append-only JSONL so a session killed mid-write loses one line, not the file."""
        rec.start()
        for i in range(20):
            rec.record_pad(float(i), NEUTRAL)
        rec.stop()
        for line in (rec.directory / "pad.jsonl").read_text().splitlines():
            if line.strip():
                json.loads(line)


class TestFrames:
    def test_writes_frame_images_and_an_index(self, rec: SessionRecorder) -> None:
        rec.start()
        for i in range(3):
            rec.record_frame(a_frame(i, i * 33.0))
        rec.stop()
        index = [json.loads(line) for line in
                 (rec.directory / "frames.jsonl").read_text().splitlines() if line.strip()]
        assert len(index) == 3
        for row in index:
            assert (rec.directory / row["path"]).is_file()

    def test_frame_index_carries_both_timestamps(self, rec: SessionRecorder) -> None:
        """session_ms aligns with controller output; source_ms is the capture clock. Losing
        either makes one kind of alignment impossible after the fact."""
        rec.start()
        rec.record_frame(a_frame(0, 100.0))
        rec.stop()
        row = json.loads((rec.directory / "frames.jsonl").read_text().splitlines()[0])
        assert row["session_ms"] == pytest.approx(100.0)
        assert row["source_ms"] == pytest.approx(101.0)
        assert row["seq"] == 0


class TestFrameStorageIsAffordable:
    """A live 12-second run wrote 1.8GB of lossless PNG -- about 150MB/s, or 540GB per hour
    of play. Faithfully recording an unusable amount of data is still unusable.

    The fix is principled rather than merely pragmatic: the model is handed downscaled JPEG,
    so recording lossless full-resolution frames stores something the policy never saw. The
    dataset should contain what the policy actually observed, which is also 40x smaller.
    """

    def big_frame(self, seq: int = 0) -> Frame:
        rng = np.random.default_rng(seq)  # noise, so compression cannot cheat
        buffer = rng.integers(0, 255, (720, 1280, 4), dtype=np.uint8)
        return Frame(seq=seq, session_ms=0.0, source_ms=None, width=1280, height=720,
                     pixel_format="BGRA8", data=buffer)

    def test_defaults_to_jpeg(self, tmp_path: Path) -> None:
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        recorder.record_frame(self.big_frame())
        recorder.stop()
        row = json.loads((recorder.directory / "frames.jsonl").read_text().splitlines()[0])
        assert row["path"].endswith(".jpg")

    def test_png_is_still_available_for_a_lossless_capture(self, tmp_path: Path) -> None:
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), frame_format="png")
        recorder.start()
        recorder.record_frame(self.big_frame())
        recorder.stop()
        row = json.loads((recorder.directory / "frames.jsonl").read_text().splitlines()[0])
        assert row["path"].endswith(".png")

    def test_jpeg_is_dramatically_smaller_than_png(self, tmp_path: Path) -> None:
        sizes = {}
        for fmt in ("png", "jpeg"):
            recorder = SessionRecorder(root=tmp_path / fmt, clock=ManualClock(),
                                       frame_format=fmt)
            recorder.start()
            recorder.record_frame(self.big_frame())
            recorder.stop()
            row = json.loads((recorder.directory / "frames.jsonl").read_text().splitlines()[0])
            sizes[fmt] = (recorder.directory / row["path"]).stat().st_size
        assert sizes["jpeg"] * 4 < sizes["png"]

    def test_frames_are_downscaled_to_the_configured_limit(self, tmp_path: Path) -> None:
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), frame_max_dim=320)
        recorder.start()
        recorder.record_frame(self.big_frame())
        recorder.stop()
        row = json.loads((recorder.directory / "frames.jsonl").read_text().splitlines()[0])
        assert max(row["width"], row["height"]) == 320

    def test_the_index_records_stored_dimensions_not_captured_ones(self,
                                                                   tmp_path: Path) -> None:
        """Replay must know the size of the image actually on disk. Recording the capture
        dimensions instead would make every downscaled frame silently mislabelled."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), frame_max_dim=320)
        recorder.start()
        recorder.record_frame(self.big_frame())
        recorder.stop()
        row = json.loads((recorder.directory / "frames.jsonl").read_text().splitlines()[0])
        import cv2

        image = cv2.imread(str(recorder.directory / row["path"]))
        assert (image.shape[1], image.shape[0]) == (row["width"], row["height"])

    def test_capture_dimensions_are_preserved_for_provenance(self, tmp_path: Path) -> None:
        """Knowing what was thrown away matters as much as knowing what was kept."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), frame_max_dim=320)
        recorder.start()
        recorder.record_frame(self.big_frame())
        recorder.stop()
        row = json.loads((recorder.directory / "frames.jsonl").read_text().splitlines()[0])
        assert row["capture_width"] == 1280
        assert row["capture_height"] == 720

    def test_manifest_records_the_frame_encoding_settings(self, tmp_path: Path) -> None:
        """A year later, 'why do these frames look like that' must be answerable."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(),
                                   frame_format="jpeg", frame_max_dim=640, jpeg_quality=70)
        recorder.start()
        recorder.stop()
        manifest = json.loads((recorder.directory / "manifest.json").read_text())
        encoding = manifest["frame_encoding"]
        assert encoding["format"] == "jpeg"
        assert encoding["max_dim"] == 640
        assert encoding["jpeg_quality"] == 70

    def test_rejects_an_unknown_format(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            SessionRecorder(root=tmp_path, clock=ManualClock(), frame_format="tiff")


class TestGapsAreExplicit:
    def test_a_skipped_frame_sequence_is_recorded_as_a_gap(self, rec: SessionRecorder) -> None:
        """The recorder polls a ring buffer that drops under load. A missing seq must appear
        as a labelled gap, not as silence -- otherwise a policy trained on this data learns
        from a timeline with invisible holes in it."""
        rec.start()
        rec.record_frame(a_frame(0, 0.0))
        rec.record_frame(a_frame(5, 165.0))
        rec.stop()
        events = [json.loads(line) for line in
                  (rec.directory / "events.jsonl").read_text().splitlines() if line.strip()]
        gaps = [e for e in events if e["kind"] == "frame_gap"]
        assert gaps, "a skipped frame sequence produced no gap record"
        assert "1" in gaps[0]["detail"] or "4" in gaps[0]["detail"]

    def test_dropped_records_are_counted_in_the_manifest(self, tmp_path: Path) -> None:
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), queue_size=4)
        recorder.start()
        for i in range(5000):
            recorder.record_pad(float(i), NEUTRAL)
        recorder.stop()
        counts = json.loads((recorder.directory / "manifest.json").read_text())["counts"]
        assert "dropped" in counts


class TestProducersAreNeverBlocked:
    def test_recording_never_blocks_the_caller(self, tmp_path: Path) -> None:
        """The producers are a 120Hz scheduler that must never stall and a graphics callback
        that must never stall. A bounded queue that drops is correct here; a queue that
        applies backpressure would wedge the motor system to protect a log file."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), queue_size=8)
        recorder.start()
        done = threading.Event()

        def hammer() -> None:
            for i in range(50_000):
                recorder.record_pad(float(i), NEUTRAL)
            done.set()

        threading.Thread(target=hammer, daemon=True).start()
        blocked = not done.wait(20.0)
        recorder.stop()
        assert not blocked, "recording blocked its producer"

    def test_recording_before_start_is_ignored_not_fatal(self, rec: SessionRecorder) -> None:
        rec.record_pad(0.0, NEUTRAL)
        rec.record_frame(a_frame(0, 0.0))

    def test_recording_after_stop_is_ignored_not_fatal(self, rec: SessionRecorder) -> None:
        rec.start()
        rec.stop()
        rec.record_pad(0.0, NEUTRAL)

    def test_stop_is_idempotent(self, rec: SessionRecorder) -> None:
        rec.start()
        rec.stop()
        rec.stop()


class TestRecordingOutput:
    """The decorator that captures what the controller was ACTUALLY told to do."""

    def test_forwards_to_the_wrapped_adapter(self, rec: SessionRecorder) -> None:
        inner = NullControllerAdapter()
        clock = ManualClock()
        out = RecordingOutput(inner=inner, recorder=rec, clock=clock)
        rec.start()
        out.set_state(PadState(left_trigger=1.0))
        rec.stop()
        assert inner.states[-1].left_trigger == 1.0

    def test_records_every_state_written(self, rec: SessionRecorder) -> None:
        inner = NullControllerAdapter()
        clock = ManualClock()
        out = RecordingOutput(inner=inner, recorder=rec, clock=clock)
        rec.start()
        for i in range(5):
            clock.advance(8.3)
            out.set_state(PadState(left_trigger=i / 10.0))
        rec.stop()
        rows = [json.loads(line) for line in
                (rec.directory / "pad.jsonl").read_text().splitlines() if line.strip()]
        assert len(rows) == 5

    def test_reset_is_recorded_too(self, rec: SessionRecorder) -> None:
        """A release is the single most safety-relevant thing the pad ever does. It must
        appear in the record."""
        inner = NullControllerAdapter()
        out = RecordingOutput(inner=inner, recorder=rec, clock=ManualClock())
        rec.start()
        out.reset()
        rec.stop()
        assert (rec.directory / "pad.jsonl").read_text().strip()

    def test_a_recorder_failure_never_breaks_the_controller(self, rec: SessionRecorder) -> None:
        """Recording is strictly subordinate to playing. If the disk fills, the agent keeps
        its hands; it does not freeze mid-input because a log write failed."""
        class ExplodingRecorder:
            def record_pad(self, session_ms: float, state: PadState) -> None:
                raise RuntimeError("disk full")

        inner = NullControllerAdapter()
        out = RecordingOutput(inner=inner, recorder=ExplodingRecorder(),  # type: ignore[arg-type]
                              clock=ManualClock())
        out.set_state(PadState(left_trigger=1.0))
        assert inner.states[-1].left_trigger == 1.0


class TestReplay:
    def test_a_session_can_be_read_back(self, rec: SessionRecorder) -> None:
        rec.start()
        rec.record_frame(a_frame(0, 0.0))
        rec.record_pad(0.0, NEUTRAL)
        rec.record_chunk(0.0, a_chunk())
        rec.record_event("takeover", 10.0, "human took controller")
        rec.stop()
        session = load_session(rec.directory)
        assert session.manifest["clean_shutdown"] is True
        assert len(session.frames) == 1
        assert len(session.pad) == 1
        assert len(session.chunks) == 1
        assert len(session.events) == 1

    def test_frames_and_pad_states_share_one_timeline(self, rec: SessionRecorder) -> None:
        """The single property that makes this a dataset instead of two log files: given a
        frame, you can ask what the controller was doing at that instant."""
        rec.start()
        rec.record_pad(0.0, NEUTRAL)
        rec.record_frame(a_frame(0, 16.0))
        rec.record_pad(16.6, PadState(left_stick=(1.0, 0.0)))
        rec.record_frame(a_frame(1, 49.0))
        rec.stop()
        session = load_session(rec.directory)
        frame_times = [f["session_ms"] for f in session.frames]
        pad_times = [p["session_ms"] for p in session.pad]
        assert frame_times == sorted(frame_times)
        assert pad_times == sorted(pad_times)
        # for each frame there is a most-recent preceding pad state
        for t in frame_times:
            assert any(p <= t for p in pad_times)

    def test_replay_reports_a_truncated_session_rather_than_failing(
        self, rec: SessionRecorder
    ) -> None:
        """A crashed session is exactly when the data matters most. A half-written final
        line must cost that line, not the session."""
        rec.start()
        rec.record_pad(0.0, NEUTRAL)
        rec.stop()
        with (rec.directory / "pad.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"session_ms": 1.0, "left_st')  # torn write
        session = load_session(rec.directory)
        assert len(session.pad) == 1
        assert session.truncated is True
