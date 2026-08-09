"""Shared contracts for bounded, non-blocking video capture sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from player2.contracts import Frame


class CaptureError(RuntimeError):
    """A capture source could not be started or kept running.

    Exists so callers can distinguish "pick a different window" from "the capture stack is
    broken". Not every titled top-level window owns a capturable surface -- shell helpers
    and phantom windows are refused by the graphics API with a bare "the parameter is
    incorrect" -- and a caller enumerating windows to find a game needs to try the next one
    rather than abort. An untyped exception surfacing from a graphics thread makes that
    distinction impossible.
    """


@dataclass(frozen=True)
class CaptureStats:
    """Describe capture progress so missing observations are never mistaken for a record."""

    frames_captured: int
    frames_dropped: int
    frames_errored: int
    last_frame_ms: float | None


@runtime_checkable
class IVideoSource(Protocol):
    """Provide copied frames without making a graphics callback wait for a consumer."""

    def start(self) -> None:
        """Begin accepting frames from the source."""
        ...

    def stop(self) -> None:
        """Stop capture without leaving the source marked as running."""
        ...

    def latest(self, n: int = 1) -> tuple[Frame, ...]:
        """Return a stable chronological snapshot of the most recent frames."""
        ...

    @property
    def is_running(self) -> bool:
        """Return whether the capture source is presently accepting frames."""
        ...

    @property
    def stats(self) -> CaptureStats:
        """Return a stable accounting snapshot, including deliberate buffer drops."""
        ...
