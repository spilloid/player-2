"""Typed boundary between captured evidence and model-authored controller intent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from player2.contracts import ActionChunk, Frame
from player2.control.scheduler import SchedulerEvent


@dataclass(frozen=True)
class Observation:
    """Freeze the evidence and provenance used to author one action proposal.

    The cutoff prevents stale pixels from being mistaken for the current world, while the
    epoch and sequence let the scheduler reject an answer invalidated by a later takeover.
    """

    frames: tuple[Frame, ...]
    goal: str | None
    history: tuple[SchedulerEvent, ...]
    observation_cutoff_ms: float
    deadline_ms: float
    epoch: int
    decision_seq: int


@runtime_checkable
class IFastPolicy(Protocol):
    """Turn recent evidence into bounded intent without owning controller output."""

    def propose(self, observation: Observation) -> ActionChunk | None:
        """Return intent only; direct device writes would bypass scheduler safety rules."""
        ...


@runtime_checkable
class IDeliberativeModel(Protocol):
    """Produce an opaque goal without putting slow reasoning on the motor thread."""

    def deliberate(self, observation: Observation) -> str | None:
        """Return model-owned guidance that the game-independent runtime never interprets."""
        ...
