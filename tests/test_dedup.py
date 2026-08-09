"""Contract for frame de-duplication and the shared encode cache.

Two changes with one motivation: stop paying twice for the same pixels.

De-duplication measured on a real session: 215 recorded frames collapsed to 14 unique
images, 43.4MB down to 2.8MB. That session had the game paused behind a menu, so it is the
best case rather than the typical one -- but a runtime that records an idle screen at 30fps
for an hour should not produce 16GB of the same picture.

The encode cache is a correctness change first and a performance change second. Without it
the recorder encodes a frame for disk and the agent path encodes the same frame again for
the model: two artifacts that are only *probably* identical until a quality setting drifts.
Sharing one encode means the dataset literally contains the bytes the model saw.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np
import pytest

from player2.clock import ManualClock
from player2.contracts import Frame
from player2.record.writer import SessionRecorder, load_session
from player2.video.encode import EncodeCache


def frame(seq: int, value: int = 30, size: int = 64) -> Frame:
    array = np.full((size, size, 4), value, dtype=np.uint8)
    array[:, :, 3] = 255
    return Frame(seq=seq, session_ms=float(seq) * 33.0, source_ms=None,
                 width=size, height=size, pixel_format="BGRA8", data=array)


def index_rows(directory: Path) -> list[dict]:
    return [json.loads(line) for line in
            (directory / "frames.jsonl").read_text().splitlines() if line.strip()]


class TestRecorderDeduplication:
    def test_identical_frames_are_stored_once(self, tmp_path: Path) -> None:
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        for seq in range(20):
            recorder.record_frame(frame(seq))
        recorder.stop()
        stored = list((recorder.directory / "frames").iterdir())
        assert len(stored) == 1, f"expected 1 unique image, found {len(stored)}"

    def test_every_frame_still_appears_in_the_index(self, tmp_path: Path) -> None:
        """De-duplication is a STORAGE optimisation. It must not lose observations: the
        timeline still had twenty frames, and a policy trained on this data needs to know
        the screen was unchanged for that whole interval rather than that it went blind."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        for seq in range(20):
            recorder.record_frame(frame(seq))
        recorder.stop()
        rows = index_rows(recorder.directory)
        assert len(rows) == 20
        assert [r["seq"] for r in rows] == list(range(20))

    def test_duplicate_rows_point_at_the_same_file(self, tmp_path: Path) -> None:
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        recorder.record_frame(frame(0))
        recorder.record_frame(frame(1))
        recorder.stop()
        rows = index_rows(recorder.directory)
        assert rows[0]["path"] == rows[1]["path"]
        assert (recorder.directory / rows[0]["path"]).is_file()

    def test_different_frames_are_stored_separately(self, tmp_path: Path) -> None:
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        recorder.record_frame(frame(0, value=10))
        recorder.record_frame(frame(1, value=200))
        recorder.stop()
        rows = index_rows(recorder.directory)
        assert rows[0]["path"] != rows[1]["path"]
        assert len(list((recorder.directory / "frames").iterdir())) == 2

    def test_index_records_the_content_hash(self, tmp_path: Path) -> None:
        """So a consumer can group identical observations without re-reading the images."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        recorder.record_frame(frame(0))
        recorder.stop()
        assert index_rows(recorder.directory)[0]["content_hash"]

    def test_manifest_reports_unique_versus_total(self, tmp_path: Path) -> None:
        """This ratio is a diagnostic, not just an efficiency number: a session that
        collapses to a handful of unique images is telling you nothing happened in it."""
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        for seq in range(10):
            recorder.record_frame(frame(seq))
        recorder.record_frame(frame(10, value=99))
        recorder.stop()
        counts = json.loads((recorder.directory / "manifest.json").read_text())["counts"]
        assert counts["frames"] == 11
        assert counts["frames_unique"] == 2

    def test_dedup_can_be_disabled(self, tmp_path: Path) -> None:
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock(), deduplicate=False)
        recorder.start()
        recorder.record_frame(frame(0))
        recorder.record_frame(frame(1))
        recorder.stop()
        assert len(list((recorder.directory / "frames").iterdir())) == 2

    def test_a_replayed_session_still_resolves_every_frame(self, tmp_path: Path) -> None:
        recorder = SessionRecorder(root=tmp_path, clock=ManualClock())
        recorder.start()
        for seq in range(5):
            recorder.record_frame(frame(seq))
        recorder.stop()
        session = load_session(recorder.directory)
        assert len(session.frames) == 5
        for row in session.frames:
            assert (recorder.directory / row["path"]).is_file()


class TestEncodeCache:
    def test_encoding_the_same_frame_twice_encodes_once(self) -> None:
        calls = {"n": 0}

        def encoder(f: Frame) -> bytes:
            calls["n"] += 1
            return b"encoded"

        cache = EncodeCache(capacity=8)
        target = frame(0)
        assert cache.get(target, "jpeg", 512, encoder) == b"encoded"
        assert cache.get(target, "jpeg", 512, encoder) == b"encoded"
        assert calls["n"] == 1

    def test_different_settings_are_cached_separately(self) -> None:
        """The recorder and the model may want different sizes. Returning one for the other
        would silently hand a consumer an image it did not ask for."""
        calls = {"n": 0}

        def encoder(f: Frame) -> bytes:
            calls["n"] += 1
            return bytes([calls["n"]])

        cache = EncodeCache(capacity=8)
        target = frame(0)
        cache.get(target, "jpeg", 512, encoder)
        cache.get(target, "jpeg", 256, encoder)
        cache.get(target, "png", 512, encoder)
        assert calls["n"] == 3

    def test_different_frames_are_cached_separately(self) -> None:
        calls = {"n": 0}

        def encoder(f: Frame) -> bytes:
            calls["n"] += 1
            return bytes([calls["n"]])

        cache = EncodeCache(capacity=8)
        cache.get(frame(0, value=10), "jpeg", 512, encoder)
        cache.get(frame(1, value=200), "jpeg", 512, encoder)
        assert calls["n"] == 2

    def test_identical_pixels_hit_even_with_different_metadata(self) -> None:
        """Two captures of an unchanged screen differ in seq and timestamp but are the same
        image. Keying on seq would make the cache useless in exactly the case it helps most."""
        calls = {"n": 0}

        def encoder(f: Frame) -> bytes:
            calls["n"] += 1
            return b"x"

        cache = EncodeCache(capacity=8)
        cache.get(frame(0), "jpeg", 512, encoder)
        cache.get(frame(7), "jpeg", 512, encoder)
        assert calls["n"] == 1

    def test_capacity_is_bounded(self) -> None:
        """An unbounded cache inside the process that owns the runtime's eyes is a slow leak
        that ends with the OOM killer taking out the thing holding the controller."""
        cache = EncodeCache(capacity=4)
        for seq in range(50):
            cache.get(frame(seq, value=seq + 1), "jpeg", 512, lambda f: b"x")
        assert len(cache) <= 4

    def test_evicts_least_recently_used(self) -> None:
        calls = {"n": 0}

        def encoder(f: Frame) -> bytes:
            calls["n"] += 1
            return b"x"

        cache = EncodeCache(capacity=2)
        a, b, c = frame(0, 10), frame(1, 20), frame(2, 30)
        cache.get(a, "jpeg", 512, encoder)
        cache.get(b, "jpeg", 512, encoder)
        cache.get(a, "jpeg", 512, encoder)  # refresh a
        cache.get(c, "jpeg", 512, encoder)  # should evict b
        before = calls["n"]
        cache.get(a, "jpeg", 512, encoder)
        assert calls["n"] == before, "the recently used entry was evicted"

    def test_rejects_a_useless_capacity(self) -> None:
        for bad in (0, -1):
            with pytest.raises(ValueError):
                EncodeCache(capacity=bad)

    def test_is_thread_safe(self) -> None:
        """The recorder's writer thread and the agent thread both use it."""
        cache = EncodeCache(capacity=32)
        errors: list[BaseException] = []

        def worker(offset: int) -> None:
            try:
                for i in range(200):
                    cache.get(frame(i, value=(i + offset) % 250), "jpeg", 512, lambda f: b"x")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,), daemon=True) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        assert not errors, f"concurrent cache access failed: {errors}"
