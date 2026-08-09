"""Interfaces for the one component permitted to write controller reports."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from player2.contracts import PadState


@runtime_checkable
class IControllerOutput(Protocol):
    """Write complete controller snapshots and provide a safe neutral reset."""

    def set_state(self, state: PadState) -> None:
        """Send one complete state; partial updates could leave an old input held."""
        ...

    def reset(self) -> None:
        """Release every input, for failure handling and orderly shutdown."""
        ...
