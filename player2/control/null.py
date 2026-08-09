"""In-memory controller output used as the scheduler's deterministic test oracle."""

from __future__ import annotations

from player2.contracts import NEUTRAL, PadState


class NullControllerAdapter:
    """Record each complete report so tests can inspect exactly what reached the pad."""

    def __init__(self) -> None:
        """Start with no reports; a scheduler tick records even an idle neutral state."""
        self.states: list[PadState] = []
        self.reset_calls = 0

    def set_state(self, state: PadState) -> None:
        """Record the supplied complete state without deduplicating physical reports."""
        self.states.append(state)

    def reset(self) -> None:
        """Record neutral because reset must be observable just like a hardware release."""
        self.reset_calls += 1
        self.set_state(NEUTRAL)
