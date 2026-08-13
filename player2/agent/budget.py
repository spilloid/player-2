"""Rolling token-budget accounting for model-backed agent cognition.

Scope, deliberately: this assumes one governed call in flight at a time per governor (the
real caller, AgentLoop, only ever has one `propose()` outstanding on its `_run` thread at
once). Sharing a single BudgetGovernor across multiple concurrent callers -- two AgentLoops,
or two threads both driving the same GovernedTransport -- can let more than one call start
between a `decide()` and the matching `record()`, since a decision is a snapshot, not a
reservation. Multi-caller admission control is out of scope here; it would need per-call
reservations, which is a materially different (and heavier) mechanism than what CARRYOVER
asked for. Same assumption applies to a transport's `last_usage` property, which is ambient
mutable state on the transport instance -- fine for one caller at a time, not safe to read
from a transport instance two threads are both calling concurrently.
"""

from __future__ import annotations

import json
import math
import threading
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from player2.agent.model_policy import IModelTransport
from player2.clock import Clock

# All three constants below are deliberately rough ballparks used only when a transport
# cannot report real provider usage -- never a substitute for a real count.
_CHARS_PER_TOKEN = 4
_ESTIMATED_IMAGE_INPUT_TOKENS = 1_200
_ESTIMATED_OUTPUT_TOKENS = 256

_SECONDS_PER_MINUTE = 60.0


def _finite_int(value: object, name: str) -> int:
    """Reject bool/float/NaN/inf so a malformed provider usage object cannot poison a sum.

    Mirrors the bool-before-int and math.isfinite checks used throughout contracts.py --
    one bad token count must raise here, not silently corrupt every deadline downstream
    (a NaN or inf entry would make every `<` comparison in `_decision` false, which reads as
    permanently EXHAUSTED and never recovers, even after it ages out of the window).
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if value < 0:
        raise ValueError(f"{name} must not be negative")
    return value


@dataclass(frozen=True)
class TokenUsage:
    """The provider-reported input and output token counts for one successful call."""

    input_tokens: int
    output_tokens: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_tokens", _finite_int(self.input_tokens, "input_tokens"))
        object.__setattr__(
            self, "output_tokens", _finite_int(self.output_tokens, "output_tokens")
        )

    @property
    def total_tokens(self) -> int:
        """Return all tokens charged to the call."""
        return self.input_tokens + self.output_tokens


class BudgetLevel(StrEnum):
    """The current cognitive cost posture."""

    NORMAL = "normal"
    DEGRADED = "degraded"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True)
class BudgetDecision:
    """The cadence and proposal permission for one loop iteration."""

    level: BudgetLevel
    min_interval_ms: float
    frames_per_observation: int
    should_propose: bool


@dataclass(frozen=True)
class BudgetGovernorStats:
    """A compact snapshot of the rolling budget window."""

    level: BudgetLevel
    tokens_in_window: int


def estimate_tokens(
    *, prompt_chars: int = 0, system_chars: int = 0, schema_chars: int = 0, image_count: int = 0
) -> TokenUsage:
    """Estimate unavailable usage conservatively -- a rough ballpark, not a real count.

    Covers every part of an actual request this package sends: the user prompt, the system
    prompt, and (for a tool call) the serialized schema -- omitting any of those would let a
    large system prompt or tool schema slip through as free -- plus a flat per-image estimate
    and a non-zero output floor, since a response that generated no output tokens at all is
    not the realistic case this fallback exists to cover.
    """
    input_chars = max(0, prompt_chars) + max(0, system_chars) + max(0, schema_chars)
    input_tokens = max(1, input_chars // _CHARS_PER_TOKEN)
    input_tokens += max(0, image_count) * _ESTIMATED_IMAGE_INPUT_TOKENS
    return TokenUsage(input_tokens=input_tokens, output_tokens=_ESTIMATED_OUTPUT_TOKENS)


class BudgetGovernor:
    """Apply a stateless policy to the token total in a trailing time window."""

    def __init__(
        self,
        *,
        tokens_per_minute_limit: float,
        base_interval_ms: float,
        base_frames_per_observation: int,
        clock: Clock,
        max_interval_ms: float | None = None,
        min_frames_per_observation: int = 1,
        window_seconds: float = 60.0,
        degrade_at_fraction: float = 0.8,
    ) -> None:
        self._tokens_per_minute_limit = self._positive_finite(
            tokens_per_minute_limit, "tokens_per_minute_limit"
        )
        self._base_interval_ms = self._positive_finite(base_interval_ms, "base_interval_ms")
        if (isinstance(base_frames_per_observation, bool)
                or not isinstance(base_frames_per_observation, int)
                or base_frames_per_observation <= 0):
            raise ValueError("base_frames_per_observation must be a positive integer")
        self._base_frames_per_observation = base_frames_per_observation
        maximum = 4 * self._base_interval_ms if max_interval_ms is None else self._positive_finite(
            max_interval_ms, "max_interval_ms"
        )
        if maximum < self._base_interval_ms:
            raise ValueError("max_interval_ms must be at least base_interval_ms")
        self._max_interval_ms = maximum
        if (isinstance(min_frames_per_observation, bool)
                or not isinstance(min_frames_per_observation, int)
                or not 1 <= min_frames_per_observation <= base_frames_per_observation):
            raise ValueError("min_frames_per_observation must be from 1 through the base count")
        self._min_frames_per_observation = min_frames_per_observation
        self._window_seconds = self._positive_finite(window_seconds, "window_seconds")
        if isinstance(degrade_at_fraction, bool) or not isinstance(
                degrade_at_fraction, (int, float)):
            raise ValueError("degrade_at_fraction must be strictly between 0 and 1")
        self._degrade_at_fraction = float(degrade_at_fraction)
        if (not math.isfinite(self._degrade_at_fraction)
                or not 0.0 < self._degrade_at_fraction < 1.0):
            raise ValueError("degrade_at_fraction must be strictly between 0 and 1")
        self._clock = clock
        # A per-minute limit compared against a window of a different length must be scaled,
        # or "tokens_per_minute_limit" quietly means something else at any window_seconds != 60
        # (a 10s window would then admit six times the configured per-minute rate). Computed
        # once here since window_seconds and the limit are both immutable after construction.
        self._window_limit = self._tokens_per_minute_limit * (
            self._window_seconds / _SECONDS_PER_MINUTE
        )
        self._entries: deque[tuple[float, int]] = deque()
        self._lock = threading.Lock()

    def record(self, usage: TokenUsage | None) -> None:
        """Record a successful call, retaining only the configured trailing window.

        `usage=None` means the caller has no information at all about what a call cost --
        not even a prompt to estimate from -- and gets `estimate_tokens()`'s bare defaults.
        A caller that DOES have the request available (GovernedTransport) should always
        build its own more specific estimate and pass it here rather than relying on this.
        """
        tokens = usage.total_tokens if usage is not None else estimate_tokens().total_tokens
        with self._lock:
            now_ms = self._clock.now_ms()
            self._prune(now_ms)
            self._entries.append((now_ms, tokens))

    def decide(self) -> BudgetDecision:
        """Return a fresh decision derived only from current rolling-window usage."""
        with self._lock:
            now_ms = self._clock.now_ms()
            self._prune(now_ms)
            return self._decision(sum(tokens for _, tokens in self._entries))

    @property
    def stats(self) -> BudgetGovernorStats:
        """Return current accounting and the corresponding non-sticky budget level."""
        with self._lock:
            now_ms = self._clock.now_ms()
            self._prune(now_ms)
            tokens = sum(tokens for _, tokens in self._entries)
            return BudgetGovernorStats(level=self._decision(tokens).level, tokens_in_window=tokens)

    @staticmethod
    def _positive_finite(value: object, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} must be a positive finite number")
        number = float(value)
        if not math.isfinite(number) or number <= 0.0:
            raise ValueError(f"{name} must be a positive finite number")
        return number

    def _prune(self, now_ms: float) -> None:
        # Assumes entries are appended in non-decreasing time order, which any Clock
        # satisfying this package's Protocol (a monotonic session-relative source) guarantees,
        # and which holds under concurrent callers too because append happens under this same
        # lock. A caller-supplied Clock that goes backwards would violate that contract
        # elsewhere in the runtime as well (e.g. the scheduler's own deadline math), not just
        # here.
        cutoff_ms = now_ms - self._window_seconds * 1_000.0
        while self._entries and self._entries[0][0] < cutoff_ms:
            self._entries.popleft()

    def _decision(self, tokens: int) -> BudgetDecision:
        if tokens < self._window_limit * self._degrade_at_fraction:
            return BudgetDecision(
                BudgetLevel.NORMAL, self._base_interval_ms,
                self._base_frames_per_observation, True,
            )
        if tokens < self._window_limit:
            return BudgetDecision(
                BudgetLevel.DEGRADED,
                min(self._max_interval_ms, self._base_interval_ms * 2),
                max(self._min_frames_per_observation, self._base_frames_per_observation // 2),
                True,
            )
        return BudgetDecision(
            BudgetLevel.EXHAUSTED, self._max_interval_ms,
            self._min_frames_per_observation, False,
        )


@runtime_checkable
class IBudgetGovernor(Protocol):
    """Describe the loop-facing governor seam for deterministic test doubles."""

    def decide(self) -> BudgetDecision:
        """Return the fresh policy for one iteration."""
        ...


class GovernedTransport:
    """Record successful AND failed model calls while otherwise forwarding requests unchanged.

    Recording on failure too (not just success) is deliberate: a response that fails local
    parsing (a model that won't call the required tool, malformed JSON, ...) was still
    generated and billed by the provider. Skipping accounting there would make the governor
    blind to exactly the failure mode a flaky or small local model is most likely to produce
    -- unbounded retries at full cost with the budget cap never once triggering. On failure
    this always uses an estimate (never the inner transport's `last_usage`, which -- per that
    property's own contract -- still holds whatever a PREVIOUS successful call reported and
    would double-count that call rather than describe this one).
    """

    def __init__(self, *, inner: IModelTransport, governor: BudgetGovernor) -> None:
        self._inner = inner
        self._governor = governor

    def complete_tool(
        self,
        *,
        system: str,
        prompt: str,
        images: tuple[bytes, ...],
        tool_name: str,
        tool_schema: dict[str, Any],
        timeout_s: float,
    ) -> dict[str, Any]:
        try:
            result = self._inner.complete_tool(
                system=system, prompt=prompt, images=images, tool_name=tool_name,
                tool_schema=tool_schema, timeout_s=timeout_s,
            )
        except Exception:
            self._record(system=system, prompt=prompt, images=images, tool_schema=tool_schema,
                         prefer_actual=False)
            raise
        self._record(system=system, prompt=prompt, images=images, tool_schema=tool_schema,
                     prefer_actual=True)
        return result

    def complete_text(
        self,
        *,
        system: str,
        prompt: str,
        images: tuple[bytes, ...],
        timeout_s: float,
    ) -> str:
        try:
            result = self._inner.complete_text(
                system=system, prompt=prompt, images=images, timeout_s=timeout_s,
            )
        except Exception:
            self._record(system=system, prompt=prompt, images=images, tool_schema=None,
                         prefer_actual=False)
            raise
        self._record(system=system, prompt=prompt, images=images, tool_schema=None,
                     prefer_actual=True)
        return result

    def _record(
        self, *, system: str, prompt: str, images: tuple[bytes, ...],
        tool_schema: dict[str, Any] | None, prefer_actual: bool,
    ) -> None:
        """Never let accounting turn a successful call into a failure, or mask a real one."""
        try:
            usage = getattr(self._inner, "last_usage", None) if prefer_actual else None
            if usage is None:
                schema_chars = len(json.dumps(tool_schema)) if tool_schema is not None else 0
                usage = estimate_tokens(
                    prompt_chars=len(prompt), system_chars=len(system),
                    schema_chars=schema_chars, image_count=len(images),
                )
            self._governor.record(usage)
        except Exception:
            pass
