"""A small, thread-safe frame history that drops instead of stalling capture."""

from __future__ import annotations

import threading
from collections import deque
from itertools import islice

from player2.contracts import Frame


class FrameRing:
    """Keep bounded frame history without blocking a capture callback on a slow reader.

    Overflow evicts the oldest frame. Waiting for a consumer here would stall the compositor
    callback itself, while retaining every frame would silently turn a brief lag into an
    unbounded memory failure.
    """

    def __init__(self, capacity: int) -> None:
        """Create a useful bounded history; a zero-sized one cannot retain observations."""
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._frames: deque[Frame] = deque(maxlen=capacity)
        self._dropped = 0
        self._lock = threading.Lock()

    def push(self, frame: Frame) -> None:
        """Store a frame, counting an evicted oldest frame rather than blocking delivery."""
        with self._lock:
            if len(self._frames) == self._capacity:
                self._dropped += 1
            self._frames.append(frame)

    def latest(self, n: int = 1) -> tuple[Frame, ...]:
        """Return up to ``n`` frames oldest first as a snapshot immune to later mutation."""
        if isinstance(n, bool) or not isinstance(n, int) or n <= 0:
            raise ValueError("n must be a positive integer")
        with self._lock:
            start = max(0, len(self._frames) - n)
            return tuple(islice(self._frames, start, None))

    def newest(self) -> Frame | None:
        """Return the newest frame, if any, without exposing the live deque."""
        with self._lock:
            return self._frames[-1] if self._frames else None

    def clear(self) -> None:
        """Discard retained history while preserving drop accounting for recording provenance."""
        with self._lock:
            self._frames.clear()

    def reset(self) -> None:
        """Clear frames and accounting after a failed session cannot be part of a recording."""
        with self._lock:
            self._frames.clear()
            self._dropped = 0

    @property
    def dropped(self) -> int:
        """Return the number of oldest frames discarded because the bounded ring was full."""
        with self._lock:
            return self._dropped

    @property
    def capacity(self) -> int:
        """Return the fixed retention capacity."""
        return self._capacity

    def __len__(self) -> int:
        """Return the number of retained frames under the same lock as mutation."""
        with self._lock:
            return len(self._frames)
