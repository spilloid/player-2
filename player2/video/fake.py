"""A deterministic video source for tests that exercises capture ownership rules."""

from __future__ import annotations

import math
import threading

from player2.clock import Clock
from player2.contracts import Frame
from player2.video.base import CaptureStats
from player2.video.ringbuffer import FrameRing


class FakeVideoSource:
    """Produce predictable blank BGRA frames while modelling copied capture buffers.

    Every emitted image owns a new ``bytearray``. Sharing a reusable backend buffer would make
    retained history silently rewrite itself when a later capture overwrites that memory.
    """

    def __init__(
        self,
        clock: Clock,
        width: int = 16,
        height: int = 16,
        capacity: int = 64,
    ) -> None:
        """Prepare a stopped source using the supplied session clock as its sole time source."""
        self._validate_dimension(width, "width")
        self._validate_dimension(height, "height")
        self._clock = clock
        self._width = width
        self._height = height
        self._ring = FrameRing(capacity)
        self._lock = threading.Lock()
        self._running = False
        self._next_seq = 0
        self._frames_captured = 0
        self._last_frame_ms: float | None = None

    def start(self) -> None:
        """Begin accepting explicit emissions; repeated starts do not reset sequence history."""
        with self._lock:
            self._running = True

    def stop(self) -> None:
        """Stop emissions idempotently so shutdown cannot accidentally add a final frame."""
        with self._lock:
            self._running = False

    def emit(self, n: int = 1) -> None:
        """Emit ``n`` copied frames only while running, keeping tests independent of wall time."""
        if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
            raise ValueError("n must be a positive integer")
        for _ in range(n):
            with self._lock:
                if not self._running:
                    return
                session_ms = self._now_ms()
                frame = Frame(
                    seq=self._next_seq,
                    session_ms=session_ms,
                    source_ms=None,
                    width=self._width,
                    height=self._height,
                    pixel_format="BGRA8",
                    data=bytearray(self._width * self._height * 4),
                )
                self._next_seq += 1
                self._frames_captured += 1
                self._last_frame_ms = session_ms
            self._ring.push(frame)

    def latest(self, n: int = 1) -> tuple[Frame, ...]:
        """Return chronological copied-frame history without exposing the mutable ring."""
        return self._ring.latest(n)

    @property
    def is_running(self) -> bool:
        """Return whether calls to ``emit`` currently produce observations."""
        with self._lock:
            return self._running

    @property
    def stats(self) -> CaptureStats:
        """Return a stable snapshot including every overflow deliberately dropped by the ring."""
        with self._lock:
            return CaptureStats(
                frames_captured=self._frames_captured,
                frames_dropped=self._ring.dropped,
                frames_errored=0,
                last_frame_ms=self._last_frame_ms,
            )

    def _now_ms(self) -> float:
        """Read finite test time so invalid clocks cannot create impossible frame metadata."""
        value = self._clock.now_ms()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("clock value must be a finite number")
        now_ms = float(value)
        if not math.isfinite(now_ms):
            raise ValueError("clock value must be a finite number")
        return now_ms

    @staticmethod
    def _validate_dimension(value: object, name: str) -> None:
        """Reject impossible image sizes before allocating a buffer for every test frame."""
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
