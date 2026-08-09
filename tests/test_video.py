"""Contract for player2.video -- the runtime's eyes.

Capture has the same shape of danger as the motor system, from the opposite direction. The
scheduler must never be blocked because it is the only thing that can release the controller;
the capture callback must never be blocked because it runs inside a graphics-API callback,
and stalling there stalls the compositor's frame delivery, not just us.

Two hazards drive most of this file:

  * The frame buffer handed to a capture callback is MAPPED memory, valid only until the
    callback returns. Retaining it without copying yields frames that silently mutate or
    decay into garbage -- and the failure is invisible until someone trains on the data.
  * A ring buffer that blocks when full converts "we are running behind" into "the capture
    thread is wedged". It must drop, and it must count what it dropped, because an
    unrecorded gap in a demonstration dataset is worse than a labelled one.

`FrameRing` and `FakeVideoSource` are deterministic and carry the weight of the testing;
the real backend is verified by a live smoke test that skips when no window is available.
"""

import threading
import time

import pytest

from player2.clock import ManualClock, SessionClock
from player2.contracts import Frame
from player2.video.base import CaptureStats, IVideoSource
from player2.video.fake import FakeVideoSource
from player2.video.ringbuffer import FrameRing


def frame(seq: int, session_ms: float = 0.0, data: object = b"") -> Frame:
    return Frame(seq=seq, session_ms=session_ms, source_ms=None, width=4, height=4,
                 pixel_format="BGRA8", data=data)


class TestFrameRingBasics:
    def test_starts_empty(self) -> None:
        ring = FrameRing(capacity=4)
        assert len(ring) == 0
        assert ring.newest() is None
        assert ring.latest(1) == ()

    def test_rejects_a_useless_capacity(self) -> None:
        for bad in (0, -1):
            with pytest.raises(ValueError):
                FrameRing(capacity=bad)

    def test_holds_frames_up_to_capacity(self) -> None:
        ring = FrameRing(capacity=3)
        for i in range(3):
            ring.push(frame(i))
        assert len(ring) == 3
        assert ring.dropped == 0

    def test_newest_returns_the_most_recent_frame(self) -> None:
        ring = FrameRing(capacity=3)
        for i in range(3):
            ring.push(frame(i))
        newest = ring.newest()
        assert newest is not None and newest.seq == 2

    def test_clear_empties_the_ring(self) -> None:
        ring = FrameRing(capacity=3)
        ring.push(frame(0))
        ring.clear()
        assert len(ring) == 0 and ring.newest() is None


class TestFrameRingOrdering:
    def test_latest_returns_chronological_order(self) -> None:
        """Oldest first. A model reasoning about 'what just happened' reads a sequence
        forwards; handing it reversed history is a silent correctness bug in the prompt."""
        ring = FrameRing(capacity=8)
        for i in range(5):
            ring.push(frame(i))
        assert [f.seq for f in ring.latest(3)] == [2, 3, 4]

    def test_latest_returns_everything_when_asked_for_more_than_it_holds(self) -> None:
        ring = FrameRing(capacity=8)
        for i in range(3):
            ring.push(frame(i))
        assert [f.seq for f in ring.latest(99)] == [0, 1, 2]

    def test_latest_defaults_to_one(self) -> None:
        ring = FrameRing(capacity=4)
        ring.push(frame(7))
        assert [f.seq for f in ring.latest()] == [7]

    def test_latest_rejects_a_non_positive_count(self) -> None:
        ring = FrameRing(capacity=4)
        for bad in (0, -1):
            with pytest.raises(ValueError):
                ring.latest(bad)

    def test_returns_an_immutable_snapshot(self) -> None:
        """Callers hold these while new frames keep arriving. Handing out the live
        container would let the buffer mutate under a model mid-decision."""
        ring = FrameRing(capacity=4)
        ring.push(frame(0))
        snapshot = ring.latest(4)
        ring.push(frame(1))
        assert isinstance(snapshot, tuple)
        assert [f.seq for f in snapshot] == [0]


class TestFrameRingDropsAreExplicit:
    def test_overflow_discards_the_oldest(self) -> None:
        ring = FrameRing(capacity=3)
        for i in range(5):
            ring.push(frame(i))
        assert [f.seq for f in ring.latest(3)] == [2, 3, 4]

    def test_drops_are_counted(self) -> None:
        """A gap nobody recorded is indistinguishable from a gap that never happened. A
        year later, 'unknown missing interval' is far worse than 'labelled damage'."""
        ring = FrameRing(capacity=3)
        for i in range(10):
            ring.push(frame(i))
        assert ring.dropped == 7

    def test_push_never_blocks_when_full(self) -> None:
        """Blocking here would wedge a graphics-API callback, which stalls frame delivery
        for the whole compositor rather than just for us."""
        ring = FrameRing(capacity=2)
        done = threading.Event()

        def hammer() -> None:
            for i in range(10000):
                ring.push(frame(i))
            done.set()

        threading.Thread(target=hammer, daemon=True).start()
        assert done.wait(10.0), "push blocked when the ring was full"


class TestFrameRingIsThreadSafe:
    def test_concurrent_push_and_read_stay_consistent(self) -> None:
        ring = FrameRing(capacity=64)
        stop = threading.Event()
        errors: list[BaseException] = []

        def producer() -> None:
            try:
                for i in range(20000):
                    ring.push(frame(i))
            except BaseException as exc:  # noqa: BLE001 - recorded and re-raised in the test
                errors.append(exc)
            finally:
                stop.set()

        def consumer() -> None:
            try:
                while not stop.is_set():
                    seqs = [f.seq for f in ring.latest(16)]
                    assert seqs == sorted(seqs), "frames came back out of order"
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=producer, daemon=True),
                   threading.Thread(target=consumer, daemon=True)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30.0)
        assert not errors, f"concurrent access failed: {errors}"


class TestVideoSourceProtocol:
    def test_fake_source_satisfies_the_interface(self) -> None:
        assert isinstance(FakeVideoSource(clock=ManualClock()), IVideoSource)

    def test_stats_are_reported(self) -> None:
        source = FakeVideoSource(clock=ManualClock())
        assert isinstance(source.stats, CaptureStats)
        assert source.stats.frames_captured == 0
        assert source.stats.frames_dropped == 0


class TestFakeVideoSource:
    def test_is_not_running_until_started(self) -> None:
        source = FakeVideoSource(clock=ManualClock())
        assert source.is_running is False
        source.start()
        assert source.is_running is True
        source.stop()
        assert source.is_running is False

    def test_emits_frames_with_monotonic_sequence_numbers(self) -> None:
        source = FakeVideoSource(clock=ManualClock())
        source.start()
        source.emit(5)
        assert [f.seq for f in source.latest(5)] == [0, 1, 2, 3, 4]

    def test_timestamps_come_from_the_injected_clock(self) -> None:
        """One clock. A source that reads the wall clock itself cannot be aligned with
        recorded controller output afterwards."""
        clock = ManualClock()
        source = FakeVideoSource(clock=clock)
        source.start()
        source.emit()
        clock.advance(100.0)
        source.emit()
        assert [f.session_ms for f in source.latest(2)] == [0.0, 100.0]

    def test_ignores_emissions_while_stopped(self) -> None:
        source = FakeVideoSource(clock=ManualClock())
        source.emit(3)
        assert source.latest(3) == ()

    def test_counts_captured_frames(self) -> None:
        source = FakeVideoSource(clock=ManualClock())
        source.start()
        source.emit(4)
        assert source.stats.frames_captured == 4

    def test_frames_do_not_share_a_buffer(self) -> None:
        """The aliasing hazard, stated as a contract every source must meet.

        A capture callback receives MAPPED memory that is unmapped when it returns, and
        backends commonly reuse one buffer for every frame. A source that retains those
        buffers hands out history that silently rewrites itself: ask for the last five
        frames and receive the same image five times, or worse, freed memory. Nothing
        downstream can detect this -- it just quietly poisons the dataset.
        """
        source = FakeVideoSource(clock=ManualClock())
        source.start()
        source.emit(3)
        buffers = [id(f.data) for f in source.latest(3)]
        assert len(set(buffers)) == 3, "frames share a buffer object"

    def test_stop_is_idempotent(self) -> None:
        source = FakeVideoSource(clock=ManualClock())
        source.start()
        source.stop()
        source.stop()
        assert source.is_running is False

    def test_can_be_restarted(self) -> None:
        source = FakeVideoSource(clock=ManualClock())
        source.start()
        source.emit()
        source.stop()
        source.start()
        source.emit()
        assert source.stats.frames_captured == 2


class TestWindowsGraphicsCapture:
    """Construction and validation only. Live capture is covered by the smoke test below."""

    def test_requires_a_target(self) -> None:
        from player2.video.wgc import WindowsGraphicsCapture

        with pytest.raises(ValueError):
            WindowsGraphicsCapture(clock=SessionClock())

    def test_rejects_being_given_two_targets(self) -> None:
        """Ambiguity about WHAT is being recorded is not acceptable in a dataset."""
        from player2.video.wgc import WindowsGraphicsCapture

        with pytest.raises(ValueError):
            WindowsGraphicsCapture(clock=SessionClock(), hwnd=1234, window_name="something")

    def test_satisfies_the_video_source_interface(self) -> None:
        from player2.video.wgc import WindowsGraphicsCapture

        source = WindowsGraphicsCapture(clock=SessionClock(), hwnd=1)
        assert isinstance(source, IVideoSource)
        assert source.is_running is False


@pytest.mark.smoke
class TestLiveCapture:
    """Runs only when a real capturable window exists. Skipped in CI, invaluable locally.

    An earlier version targeted `list_windows()[0]`, which on a real desktop turned out to
    be a phantom "Error Recovery Guide" window that the graphics API refuses with
    0x80070057. Not every titled top-level window owns a capturable surface, so the test
    must go and find one rather than assume the first will do.
    """

    def _capture_frames(self, hwnd: int, want: int = 3) -> tuple[Frame, ...]:
        from player2.video.wgc import WindowsGraphicsCapture

        source = WindowsGraphicsCapture(clock=SessionClock(), hwnd=hwnd)
        source.start()
        try:
            deadline = time.time() + 4.0
            while time.time() < deadline and source.stats.frames_captured < want:
                time.sleep(0.05)
            return source.latest(want)
        finally:
            source.stop()

    def _live_frames(self, want: int = 3) -> tuple[Frame, ...]:
        from player2.video.base import CaptureError
        from player2.window import list_windows

        windows = list_windows()
        if not windows:
            pytest.skip("no visible windows to capture")
        for window in windows[:8]:
            try:
                frames = self._capture_frames(window.hwnd, want)
            except CaptureError:
                continue  # this window has no capturable surface; try the next
            if len(frames) >= 2:
                return frames
        pytest.skip("no capturable window produced frames in this environment")

    def test_captures_real_frames(self) -> None:
        frames = self._live_frames()
        assert all(f.width > 0 and f.height > 0 for f in frames)
        assert [f.seq for f in frames] == sorted(f.seq for f in frames)
        assert all(f.data is not None for f in frames)
        assert all(f.pixel_format == "BGRA8" for f in frames)

    def test_real_frames_do_not_share_a_buffer(self) -> None:
        """The live version of the aliasing test, and the only one that can actually catch
        a backend handing back its own reused mapped buffer."""
        frames = self._live_frames()
        assert len({id(f.data) for f in frames}) == len(frames)

    def test_source_timestamps_advance_with_the_frames(self) -> None:
        """source_ms carries the capture API's own acquisition time, which is steadier than
        arrival time -- that gap is the whole reason for recording it separately."""
        frames = self._live_frames()
        stamps = [f.source_ms for f in frames if f.source_ms is not None]
        if len(stamps) < 2:
            pytest.skip("backend reported no usable source timestamps")
        assert stamps == sorted(stamps)


class TestUncapturableWindow:
    @pytest.mark.smoke
    def test_raises_a_typed_error(self) -> None:
        """A window without a capturable surface must fail as CaptureError, not as a raw
        exception from a graphics thread. Callers need to tell 'pick a different window'
        apart from 'the capture stack is broken'."""
        from player2.video.base import CaptureError
        from player2.video.wgc import WindowsGraphicsCapture

        source = WindowsGraphicsCapture(clock=SessionClock(), hwnd=1)
        with pytest.raises(CaptureError):
            source.start()
        assert source.is_running is False
