"""Failure, clock, and lifecycle behaviour of the real capture backend.

The Unit 4 review's sharpest point was not any single defect, it was that none of them were
testable: every interleaving lived behind a graphics API that could only be smoke-tested.
So `WindowsGraphicsCapture` takes an injectable backend factory, and these tests drive the
callbacks directly -- delivering frames, raising from them, closing spontaneously, and
restarting -- with no graphics stack involved.

The bias throughout: this module feeds a demonstration dataset that will be trained on. A
crash is recoverable. A frame attributed to the wrong time, a gap nobody counted, or pixels
mutated after the fact are not, because nothing downstream can detect them.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from player2.clock import ManualClock
from player2.video.base import CaptureError
from player2.video.wgc import WindowsGraphicsCapture

TICKS_PER_MS = 10_000  # capture timestamps are Windows 100-nanosecond ticks


class FakeFrame:
    """Mimics the backend's frame object, including a reused mutable buffer."""

    def __init__(self, timespan: int, width: int = 4, height: int = 4,
                 buffer: Any | None = None) -> None:
        # max(0, ...) because tests deliberately construct frames with invalid dimensions to
        # exercise the callback's rejection path. Allocating from a negative size would raise
        # inside the fixture instead, testing nothing.
        size = max(0, width * height * 4)
        self.frame_buffer = buffer if buffer is not None else bytearray(size)
        self.width = width
        self.height = height
        self.timespan = timespan


class FakeControl:
    def __init__(self) -> None:
        self.stopped = False
        self.stop_error: Exception | None = None
        self.wait_error: Exception | None = None
        self.wait_blocks = False

    def stop(self) -> None:
        if self.stop_error:
            raise self.stop_error
        self.stopped = True

    def wait(self) -> None:
        if self.wait_error:
            raise self.wait_error
        if self.wait_blocks:
            threading.Event().wait(30.0)

    def is_finished(self) -> bool:
        return self.stopped


class FakeBackend:
    """Stands in for windows_capture.WindowsCapture."""

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.handlers: dict[str, Any] = {}
        self.control = FakeControl()
        self.start_error: Exception | None = None
        self.deliver_on_start: FakeFrame | None = None

    def event(self, fn: Any) -> Any:
        self.handlers[fn.__name__] = fn
        return fn

    def start_free_threaded(self) -> FakeControl:
        if self.deliver_on_start is not None:
            self.deliver(self.deliver_on_start)
        if self.start_error:
            raise self.start_error
        return self.control

    # -- test drivers -------------------------------------------------------
    def deliver(self, frame: FakeFrame) -> None:
        self.handlers["on_frame_arrived"](frame, None)

    def close(self) -> None:
        self.handlers["on_closed"]()


class Rig:
    """Holds a source and every backend it created, so restarts can be inspected.

    `configure` runs against each newly created backend, which lets a test arrange a
    failure mode before start() rather than reaching into the source's internals.
    """

    def __init__(self, configure: Any = None, clock: Any = None, **kwargs: Any) -> None:
        self.backends: list[FakeBackend] = []
        self.clock = clock if clock is not None else ManualClock()

        def factory(**backend_kwargs: Any) -> FakeBackend:
            backend = FakeBackend(**backend_kwargs)
            if configure is not None:
                configure(backend)
            self.backends.append(backend)
            return backend

        self.source = WindowsGraphicsCapture(
            clock=self.clock, hwnd=1, backend_factory=factory, **kwargs
        )

    @property
    def backend(self) -> FakeBackend:
        return self.backends[-1]


@pytest.fixture
def rig() -> Rig:
    return Rig()


class TestHappyPath:
    def test_delivers_frames(self, rig: Rig) -> None:
        rig.source.start()
        rig.backend.deliver(FakeFrame(timespan=0))
        rig.clock.advance(33.0)
        rig.backend.deliver(FakeFrame(timespan=33 * TICKS_PER_MS))
        assert [f.seq for f in rig.source.latest(2)] == [0, 1]
        assert rig.source.stats.frames_captured == 2

    def test_passes_draw_border_false_to_the_backend(self, rig: Rig) -> None:
        """Windows paints a highlight border around a captured window otherwise, and it
        would be baked into every training frame."""
        rig.source.start()
        assert rig.backend.kwargs.get("draw_border") is False

    def test_maps_source_timestamps_from_the_capture_clock(self, rig: Rig) -> None:
        rig.source.start()
        rig.backend.deliver(FakeFrame(timespan=1_000_000))
        rig.clock.advance(33.0)
        rig.backend.deliver(FakeFrame(timespan=1_000_000 + 33 * TICKS_PER_MS))
        stamps = [f.source_ms for f in rig.source.latest(2)]
        assert stamps[0] is not None and stamps[1] is not None
        assert stamps[1] - stamps[0] == pytest.approx(33.0)


class TestPixelsCannotBeMutatedAfterCapture:
    def test_frames_do_not_alias_the_backends_reused_buffer(self, rig: Rig) -> None:
        """Backends reuse one mapped buffer. Retaining it yields history that rewrites
        itself: ask for the last three frames and get the same image three times."""
        shared = bytearray(b"\x01" * 64)
        rig.source.start()
        rig.backend.deliver(FakeFrame(timespan=0, buffer=shared))
        shared[0] = 0xFF
        rig.clock.advance(33.0)
        rig.backend.deliver(FakeFrame(timespan=33 * TICKS_PER_MS, buffer=shared))
        first, second = rig.source.latest(2)
        assert first.data is not second.data
        assert bytes(first.data)[0] != bytes(second.data)[0]  # type: ignore[arg-type]

    def test_delivered_pixels_are_read_only(self, rig: Rig) -> None:
        """Two consumers share every frame: the model deciding what to do, and the recorder
        writing the dataset. One doing an in-place normalisation would silently rewrite what
        the other records. Freezing the dataclass does not freeze the array inside it."""
        rig.source.start()
        rig.backend.deliver(FakeFrame(timespan=0))
        data = rig.source.latest(1)[0].data
        writeable = getattr(getattr(data, "flags", None), "writeable", False)
        assert writeable is False, "captured pixels must not be writable by consumers"


class TestGapsAreNeverSilent:
    def test_a_failed_frame_is_counted(self, rig: Rig) -> None:
        """A frame lost to an exception must appear in the accounting. A gap nobody
        recorded is indistinguishable from a gap that never happened."""
        rig.source.start()
        rig.backend.deliver(FakeFrame(timespan=0, width=0))  # invalid: Frame will reject it
        assert rig.source.stats.frames_errored == 1

    def test_a_failed_frame_leaves_a_visible_sequence_gap(self, rig: Rig) -> None:
        """Without this, the next good frame gets the immediately consecutive seq and the
        recording falsely claims uninterrupted capture."""
        rig.source.start()
        rig.backend.deliver(FakeFrame(timespan=0))
        rig.backend.deliver(FakeFrame(timespan=1, width=-5))
        rig.clock.advance(33.0)
        rig.backend.deliver(FakeFrame(timespan=33 * TICKS_PER_MS))
        seqs = [f.seq for f in rig.source.latest(4)]
        assert seqs == [0, 2], f"expected a gap at seq 1, got {seqs}"

    def test_a_callback_exception_never_escapes(self, rig: Rig) -> None:
        """An exception crossing back into the graphics API is undefined behaviour."""
        rig.source.start()
        rig.backend.deliver(FakeFrame(timespan=0, width=0))  # must not raise


class TestHostileClock:
    def test_a_backwards_clock_does_not_produce_backwards_frames(self) -> None:
        """Alignment with recorded controller output is the entire value of the dataset.
        Frames whose timestamps run backwards break it with no error raised."""

        class BackwardsClock:
            def __init__(self) -> None:
                self.values = [1000.0, 900.0, 950.0]
                self.i = 0

            def now_ms(self) -> float:
                value = self.values[min(self.i, len(self.values) - 1)]
                self.i += 1
                return value

        rig = Rig(clock=BackwardsClock())
        rig.source.start()
        for tick in range(3):
            rig.backend.deliver(FakeFrame(timespan=tick * 33 * TICKS_PER_MS))
        stamps = [f.session_ms for f in rig.source.latest(4)]
        assert stamps == sorted(stamps), f"session time went backwards: {stamps}"

    def test_a_non_finite_clock_does_not_produce_a_frame(self) -> None:
        class NaNClock:
            def now_ms(self) -> float:
                return float("nan")

        rig = Rig(clock=NaNClock())
        rig.source.start()
        rig.backend.deliver(FakeFrame(timespan=0))
        assert rig.source.latest(1) == ()
        assert rig.source.stats.frames_errored >= 1

    def test_a_clock_that_reenters_the_source_does_not_deadlock(self) -> None:
        """A clock implementation is user code. If it consults source.is_running while the
        source holds its state lock during the callback, the capture thread deadlocks and
        the runtime loses its eyes with no error at all."""
        holder: dict[str, Any] = {}

        class ReentrantClock:
            def __init__(self) -> None:
                self.t = 0.0

            def now_ms(self) -> float:
                source = holder.get("source")
                if source is not None:
                    _ = source.is_running
                    _ = source.stats
                self.t += 1.0
                return self.t

        backends: list[FakeBackend] = []

        def factory(**kw: Any) -> FakeBackend:
            backend = FakeBackend(**kw)
            backends.append(backend)
            return backend

        source = WindowsGraphicsCapture(clock=ReentrantClock(), hwnd=1, backend_factory=factory)
        holder["source"] = source
        source.start()

        done = threading.Event()

        def deliver() -> None:
            backends[0].deliver(FakeFrame(timespan=0))
            done.set()

        threading.Thread(target=deliver, daemon=True).start()
        assert done.wait(5.0), "capture callback deadlocked against a reentrant clock"


class TestSourceTimestampDiscontinuity:
    def test_a_rejected_timestamp_does_not_poison_the_next_one(self) -> None:
        """The subtlest defect of the round. The previous derived value was stored BEFORE
        the plausibility check, so after one capture-clock reset the following frame was
        measured against the rejected value, passed, and was recorded as trustworthy while
        being about a second wrong. One reset produced a single None and then a confidently
        misaligned timeline."""
        rig = Rig()
        rig.source.start()
        rig.clock.advance(1000.0)
        rig.backend.deliver(FakeFrame(timespan=10_000_000))   # anchor
        rig.clock.advance(33.0)
        rig.backend.deliver(FakeFrame(timespan=0))            # reset: implausible, rejected
        rig.clock.advance(33.0)
        rig.backend.deliver(FakeFrame(timespan=330_000))      # plausible vs the REJECTED value
        frames = rig.source.latest(3)
        assert frames[1].source_ms is None, "the discontinuous frame must be marked unknown"
        third = frames[2].source_ms
        assert third is None or third >= frames[0].session_ms, (
            f"source time jumped backwards to {third} after a reset; "
            "it was trusted against a value that had already been rejected"
        )


class TestLifecycle:
    def test_a_failed_start_leaves_nothing_behind(self) -> None:
        """A backend may deliver a frame and then fail to start. Reporting CaptureError
        while keeping that frame, its sequence number, and its timestamp anchor means the
        next successful start inherits state from a session that never existed."""

        def sabotage(backend: FakeBackend) -> None:
            backend.deliver_on_start = FakeFrame(timespan=0)
            backend.start_error = RuntimeError("device lost")

        rig = Rig(configure=sabotage)
        with pytest.raises(CaptureError):
            rig.source.start()
        assert rig.source.is_running is False
        assert rig.source.latest(4) == ()
        assert rig.source.stats.frames_captured == 0

    def test_a_stale_callback_cannot_inject_into_a_new_session(self, rig: Rig) -> None:
        """Without a generation token, a straggler callback from a stopped session passes
        the is_running check of the NEW session and is recorded as a new-generation frame --
        and can seed the new timestamp anchor with the old capture clock."""
        rig.source.start()
        old = rig.backend
        rig.source.stop()
        rig.source.start()
        old.deliver(FakeFrame(timespan=999_999_999))
        assert rig.source.latest(4) == (), "a stale callback was accepted into a new session"

    def test_a_stale_close_cannot_stop_a_new_session(self, rig: Rig) -> None:
        rig.source.start()
        old = rig.backend
        rig.source.stop()
        rig.source.start()
        old.close()
        assert rig.source.is_running is True

    def test_spontaneous_close_marks_the_source_stopped(self, rig: Rig) -> None:
        rig.source.start()
        rig.backend.close()
        assert rig.source.is_running is False

    def test_restart_after_a_spontaneous_close_reaps_the_old_control(self, rig: Rig) -> None:
        """Otherwise the old capture session is overwritten and can never be reaped -- it
        keeps a thread alive, copying every delivered frame, for the life of the process."""
        rig.source.start()
        first = rig.backend
        rig.backend.close()
        rig.source.start()
        assert first.control.stopped is True

    def test_stop_does_not_hang_when_the_backend_wait_blocks(self, rig: Rig) -> None:
        """control.wait() has no timeout. Calling it unconditionally means one wedged
        backend hangs shutdown forever."""
        rig.source.start()
        rig.backend.control.wait_blocks = True
        done = threading.Event()
        threading.Thread(target=lambda: (rig.source.stop(), done.set()), daemon=True).start()
        assert done.wait(10.0), "stop() hung waiting on the backend"

    def test_stop_survives_a_backend_that_raises(self, rig: Rig) -> None:
        rig.source.start()
        rig.backend.control.stop_error = RuntimeError("already gone")
        rig.source.stop()
        assert rig.source.is_running is False

    def test_frames_after_stop_are_ignored(self, rig: Rig) -> None:
        rig.source.start()
        backend = rig.backend
        rig.source.stop()
        backend.deliver(FakeFrame(timespan=0))
        assert rig.source.latest(1) == ()
