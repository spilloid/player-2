"""Small deterministic fast policy for integration tests and runtime diagnostics."""

from __future__ import annotations

import threading
from collections.abc import Iterable

from player2.agent.base import Observation
from player2.contracts import ActionChunk


class ScriptedPolicy:
    """Cycle immutable action chunks so seam tests do not need a model dependency."""

    def __init__(self, chunks: Iterable[ActionChunk]) -> None:
        """Snapshot caller input so later list mutation cannot rewrite policy behavior."""
        self._chunks = tuple(chunks)
        self._next_index = 0
        self._lock = threading.Lock()

    def propose(self, observation: Observation) -> ActionChunk | None:
        """Return the next authored chunk, or no intent when the script is empty."""
        del observation
        with self._lock:
            if not self._chunks:
                return None
            chunk = self._chunks[self._next_index]
            self._next_index = (self._next_index + 1) % len(self._chunks)
            return chunk
