"""Monotonic clocks used to align all of a session's runtime components."""

from __future__ import annotations

import math
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Provide session-relative milliseconds from one monotonic time source."""

    def now_ms(self) -> float:
        """Return elapsed session milliseconds without being affected by wall-clock jumps."""
        ...


class SessionClock:
    """Measure real elapsed time while retaining the wall-clock session start."""

    def __init__(self) -> None:
        """Capture both origins so elapsed time is monotonic and recordings are locatable."""
        self._origin = time.perf_counter()
        self.epoch_wall = time.time()

    def now_ms(self) -> float:
        """Return monotonic milliseconds since this clock was created."""
        return (time.perf_counter() - self._origin) * 1000.0


class ManualClock:
    """A manually advanced clock that makes timing-dependent code deterministic in tests."""

    def __init__(self, start_ms: float = 0.0) -> None:
        """Start at ``start_ms`` and never advance unless ``advance`` is called."""
        self._now = self._finite(start_ms)

    def now_ms(self) -> float:
        """Return the current manually controlled session time."""
        return self._now

    def advance(self, amount_ms: float) -> None:
        """Advance by a non-negative amount so deadlines can never move backwards."""
        amount = self._finite(amount_ms)
        if amount < 0.0:
            raise ValueError("clock cannot move backwards")
        self._now += amount

    @staticmethod
    def _finite(value: object) -> float:
        """Reject non-finite input so one bad update cannot poison every deadline."""
        if isinstance(value, bool):
            raise ValueError("clock value must be numeric")
        if not isinstance(value, (int, float)):
            raise ValueError("clock value must be numeric")
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError("clock value must be numeric") from None
        if not math.isfinite(number):
            raise ValueError("clock value must be finite")
        return number
