"""Windows Graphics Capture backend with copied, bounded callback delivery."""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy  # type: ignore[import-untyped, unused-ignore]

from player2.clock import Clock
from player2.contracts import Frame
from player2.video.base import CaptureError, CaptureStats
from player2.video.ringbuffer import FrameRing

_LOGGER = logging.getLogger(__name__)
_MIN_SOURCE_INTERVAL_MS = 0.05
_MAX_SOURCE_INTERVAL_MS = 10_000.0
_STOP_TIMEOUT_SECONDS = 2.0


class WindowsGraphicsCapture:
    """Capture one explicit window without retaining invalid mapped graphics memory.

    The callback copies the mapped pixels before returning, then freezes that copy. This
    prevents a reused backend buffer or an in-place consumer transform from rewriting a
    historical observation. ``source_ms`` is reliable for relative frame spacing, not exact
    cross-stream alignment: an unknown residual offset remains between capture and session
    clock epochs.
    """

    def __init__(
        self,
        *,
        clock: Clock,
        hwnd: int | None = None,
        window_name: str | None = None,
        capacity: int = 64,
        min_interval_ms: int | None = 33,
        cursor: bool = True,
        backend_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Prepare idle capture; an injected factory makes callback failures testable."""
        if (hwnd is None) == (window_name is None):
            raise ValueError("provide exactly one of hwnd or window_name")
        self._clock = clock
        self._hwnd = hwnd
        self._window_name = window_name
        self._min_interval_ms = min_interval_ms
        self._cursor = cursor
        self._backend_factory = backend_factory
        self._ring = FrameRing(capacity)
        self._state_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._capture: Any | None = None
        self._control: Any | None = None
        self._running = False
        self._generation = 0
        self._next_seq = 0
        self._frames_captured = 0
        self._frames_errored = 0
        self._last_frame_ms: float | None = None
        self._first_timespan: int | None = None
        self._first_session_ms: float | None = None
        self._previous_derived_source_ms: float | None = None
        self._failure_logged_generation: int | None = None

    def start(self) -> None:
        """Start capture, reaping an old control so closed sessions cannot leak workers."""
        with self._lifecycle_lock:
            with self._state_lock:
                if self._running:
                    return
                self._generation += 1
                generation = self._generation
                old_control = self._control
                self._capture = None
                self._control = None
                self._reset_timestamp_anchor_locked()
                self._failure_logged_generation = None

            if old_control is not None:
                self._stop_control(old_control)

            try:
                capture = self._create_backend()

                @capture.event  # type: ignore[untyped-decorator]
                def on_frame_arrived(frame: Any, capture_control: Any) -> None:
                    del capture_control
                    self._on_frame_arrived(frame, generation)

                @capture.event  # type: ignore[untyped-decorator]
                def on_closed() -> None:
                    self._on_closed(generation)
            except Exception as error:
                self._rollback_failed_start(generation)
                raise CaptureError(f"could not open {self._target()}: {error}") from error

            with self._state_lock:
                if generation != self._generation:
                    return
                self._capture = capture
                self._running = True

            try:
                control = capture.start_free_threaded()
            except BaseException as error:
                self._rollback_failed_start(generation)
                if isinstance(error, Exception):
                    raise CaptureError(f"could not capture {self._target()}: {error}") from error
                raise

            with self._state_lock:
                if generation == self._generation:
                    self._control = control

    def stop(self) -> None:
        """Stop idempotently without an unbounded backend wait hanging shutdown forever."""
        with self._lifecycle_lock:
            with self._state_lock:
                self._running = False
                control = self._control

            if control is None:
                return
            finished = self._stop_control(control)
            if finished:
                with self._state_lock:
                    if self._control is control:
                        self._control = None
                        self._capture = None

    def _create_backend(self) -> Any:
        """Build the optional backend lazily so imports work without graphics dependencies."""
        kwargs: dict[str, Any] = {
            "cursor_capture": self._cursor,
            "draw_border": False,
            "minimum_update_interval": self._min_interval_ms,
        }
        if self._hwnd is not None:
            kwargs["window_hwnd"] = self._hwnd
        else:
            kwargs["window_name"] = self._window_name

        factory = self._backend_factory
        if factory is None:
            try:
                from windows_capture import WindowsCapture  # type: ignore[import-untyped]
            except Exception as error:
                raise CaptureError(f"capture backend unavailable: {error}") from error
            factory = WindowsCapture
        return factory(**kwargs)

    def _stop_control(self, control: Any) -> bool:
        """Request shutdown and poll briefly; a wedged backend must not trap the caller."""
        try:
            control.stop()
        except BaseException:
            self._schedule_error_log("failed to stop Windows capture")

        deadline = time.monotonic() + _STOP_TIMEOUT_SECONDS
        while True:
            try:
                if control.is_finished():
                    return True
            except BaseException:
                self._schedule_error_log("failed to poll Windows capture shutdown")
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def _target(self) -> str:
        """Describe the requested target so typed startup errors identify the failed source."""
        if self._hwnd is not None:
            return f"hwnd {self._hwnd}"
        return f"window {self._window_name!r}"

    def latest(self, n: int = 1) -> tuple[Frame, ...]:
        """Return a stable chronological snapshot while callbacks continue arriving."""
        return self._ring.latest(n)

    @property
    def is_running(self) -> bool:
        """Return false when stopped or when the target closes underneath capture."""
        with self._state_lock:
            return self._running

    @property
    def stats(self) -> CaptureStats:
        """Expose captured, dropped, and failed frames so every gap remains attributable."""
        with self._state_lock:
            return CaptureStats(
                frames_captured=self._frames_captured,
                frames_dropped=self._ring.dropped,
                frames_errored=self._frames_errored,
                last_frame_ms=self._last_frame_ms,
            )

    def _on_frame_arrived(self, captured_frame: Any, generation: int) -> None:
        """Copy and commit a frame, consuming a sequence number for every callback failure.

        Session time is deliberately sampled before the pixel copy. Sampling after it would
        bake copy and queue latency into every source timestamp and silently misattribute
        acquisition time. No user clock code runs while the state lock is held.
        """
        try:
            session_ms = self._now_ms()
        except BaseException:
            self._record_callback_failure(generation)
            return

        with self._state_lock:
            if generation != self._generation or not self._running:
                return

        try:
            pixels = numpy.array(captured_frame.frame_buffer, copy=True)
            pixels.flags.writeable = False
            width = captured_frame.width
            height = captured_frame.height
            timespan = captured_frame.timespan
        except BaseException:
            self._record_callback_failure(generation)
            return

        should_log = False
        with self._state_lock:
            if generation != self._generation or not self._running:
                return
            if self._last_frame_ms is not None and session_ms < self._last_frame_ms:
                should_log = self._record_callback_failure_locked(generation)
            else:
                try:
                    frame = Frame(
                        seq=self._next_seq,
                        session_ms=session_ms,
                        source_ms=self._source_ms(timespan, session_ms),
                        width=width,
                        height=height,
                        pixel_format="BGRA8",
                        data=pixels,
                    )
                    self._ring.push(frame)
                except BaseException:
                    should_log = self._record_callback_failure_locked(generation)
                else:
                    self._next_seq += 1
                    self._frames_captured += 1
                    self._last_frame_ms = session_ms
        if should_log:
            self._schedule_error_log("Windows capture frame callback failed")

    def _record_callback_failure(self, generation: int) -> None:
        """Count a failed current callback and reserve its sequence gap for provenance."""
        with self._state_lock:
            if generation != self._generation or not self._running:
                return
            should_log = self._record_callback_failure_locked(generation)
        if should_log:
            self._schedule_error_log("Windows capture frame callback failed")

    def _record_callback_failure_locked(self, generation: int) -> bool:
        """Reserve one sequence while locked so failed callbacks cannot become silent gaps."""
        self._next_seq += 1
        self._frames_errored += 1
        if self._failure_logged_generation == generation:
            return False
        self._failure_logged_generation = generation
        return True

    def _on_closed(self, generation: int) -> None:
        """Ignore stale closure notifications so an old backend cannot stop a new session."""
        with self._state_lock:
            if generation == self._generation:
                self._running = False

    def _source_ms(self, timespan: object, session_ms: float) -> float | None:
        """Map capture ticks to session time and re-anchor after a clock discontinuity.

        A rejected interval is emitted as ``None``. Reusing it as the prior value would make
        the next frame look plausible while being wrongly timed, so this starts a fresh
        relative-time anchor instead.
        """
        if isinstance(timespan, bool) or not isinstance(timespan, int):
            return None
        if self._first_timespan is None:
            self._first_timespan = timespan
            self._first_session_ms = session_ms
        if self._first_session_ms is None:
            return None
        derived_source_ms = self._first_session_ms + (
            (timespan - self._first_timespan) / 10_000.0
        )
        previous = self._previous_derived_source_ms
        if previous is not None:
            interval_ms = derived_source_ms - previous
            if interval_ms < _MIN_SOURCE_INTERVAL_MS or interval_ms > _MAX_SOURCE_INTERVAL_MS:
                self._first_timespan = timespan
                self._first_session_ms = session_ms
                self._previous_derived_source_ms = None
                return None
        self._previous_derived_source_ms = derived_source_ms
        return derived_source_ms

    def _rollback_failed_start(self, generation: int) -> None:
        """Erase a failed start because its frames and anchors never formed a real session."""
        with self._state_lock:
            if generation != self._generation:
                return
            self._running = False
            self._capture = None
            self._control = None
            self._next_seq = 0
            self._frames_captured = 0
            self._frames_errored = 0
            self._last_frame_ms = None
            self._reset_timestamp_anchor_locked()
            self._failure_logged_generation = None
            self._ring.reset()

    def _reset_timestamp_anchor_locked(self) -> None:
        """Forget prior capture-clock epochs so old hardware time cannot time a new session."""
        self._first_timespan = None
        self._first_session_ms = None
        self._previous_derived_source_ms = None

    def _now_ms(self) -> float:
        """Read finite session time before copying so invalid clocks cannot corrupt ordering."""
        value = self._clock.now_ms()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock value must be a finite number")
        now_ms = float(value)
        if not math.isfinite(now_ms):
            raise ValueError("clock value must be a finite number")
        return now_ms

    @staticmethod
    def _schedule_error_log(message: str) -> None:
        """Log off the callback thread so a slow handler cannot stall compositor delivery."""
        try:
            threading.Thread(
                target=WindowsGraphicsCapture._log_callback_error,
                args=(message,),
                daemon=True,
            ).start()
        except BaseException:
            pass

    @staticmethod
    def _log_callback_error(message: str) -> None:
        """Best-effort deferred logging; accounting remains the reliable failure signal."""
        try:
            _LOGGER.error(message)
        except BaseException:
            pass
