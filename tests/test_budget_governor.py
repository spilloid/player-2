"""Contract for player2.agent.budget -- degrading cognition cost before it becomes a bill.

CARRYOVER.md's own framing: "a tokens-per-minute cap that *degrades* rather than stopping...
Belongs conceptually next to the deadman; both are 'fail safe when a resource runs out.'" This
file specs the pure accounting/decision logic: a rolling token-rate window that lengthens the
decision interval and shrinks the frame count before it ever tells the loop to stop proposing
outright, and recovers on its own once the rate drops back down -- degradation must not be a
one-way trip, or a single burst would silence the agent for the rest of the session.
"""

from __future__ import annotations

import threading

import pytest

from player2.agent.budget import (
    BudgetGovernor,
    BudgetLevel,
    TokenUsage,
    estimate_tokens,
)
from player2.clock import ManualClock


def governor(
    *,
    clock: ManualClock,
    limit: float = 6000.0,
    base_interval_ms: float = 100.0,
    base_frames: int = 4,
    max_interval_ms: float = 400.0,
    min_frames: int = 1,
    window_seconds: float = 60.0,
    degrade_at_fraction: float = 0.8,
) -> BudgetGovernor:
    return BudgetGovernor(
        tokens_per_minute_limit=limit,
        base_interval_ms=base_interval_ms,
        base_frames_per_observation=base_frames,
        clock=clock,
        max_interval_ms=max_interval_ms,
        min_frames_per_observation=min_frames,
        window_seconds=window_seconds,
        degrade_at_fraction=degrade_at_fraction,
    )


class TestTokenUsage:
    def test_total_is_input_plus_output(self) -> None:
        assert TokenUsage(input_tokens=100, output_tokens=40).total_tokens == 140

    def test_rejects_negative_counts(self) -> None:
        with pytest.raises(ValueError):
            TokenUsage(input_tokens=-1, output_tokens=0)
        with pytest.raises(ValueError):
            TokenUsage(input_tokens=0, output_tokens=-1)

    def test_rejects_bool_even_though_bool_is_an_int_subclass(self) -> None:
        with pytest.raises(ValueError):
            TokenUsage(input_tokens=True, output_tokens=0)  # type: ignore[arg-type]

    def test_rejects_a_float_count(self) -> None:
        with pytest.raises(ValueError):
            TokenUsage(input_tokens=1.5, output_tokens=0)  # type: ignore[arg-type]

    def test_rejects_nan_and_infinity(self) -> None:
        """A NaN or infinite entry would make every `<` comparison in _decision() false, which
        reads as permanently EXHAUSTED and never recovers even once it ages out of the window --
        this must be caught at construction, not discovered later as a stuck governor."""
        with pytest.raises(ValueError):
            TokenUsage(input_tokens=float("nan"), output_tokens=0)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            TokenUsage(input_tokens=float("inf"), output_tokens=0)  # type: ignore[arg-type]


class TestEstimateTokens:
    def test_grows_with_prompt_length(self) -> None:
        small = estimate_tokens(prompt_chars=40, image_count=0)
        large = estimate_tokens(prompt_chars=4000, image_count=0)
        assert large.total_tokens > small.total_tokens

    def test_grows_with_image_count(self) -> None:
        none = estimate_tokens(prompt_chars=100, image_count=0)
        two = estimate_tokens(prompt_chars=100, image_count=2)
        assert two.total_tokens > none.total_tokens

    def test_grows_with_system_prompt_length(self) -> None:
        """A large system prompt is a real, often-dominant cost -- omitting it from the
        fallback would let a call with a ten-character user prompt and a 20,000-character
        system prompt look almost free to the governor."""
        small = estimate_tokens(prompt_chars=10, system_chars=10, image_count=0)
        large = estimate_tokens(prompt_chars=10, system_chars=20_000, image_count=0)
        assert large.total_tokens > small.total_tokens

    def test_grows_with_schema_length(self) -> None:
        small = estimate_tokens(prompt_chars=10, schema_chars=10, image_count=0)
        large = estimate_tokens(prompt_chars=10, schema_chars=5_000, image_count=0)
        assert large.total_tokens > small.total_tokens

    def test_output_floor_is_never_zero(self) -> None:
        """A response that generated no output tokens at all is not the realistic case this
        fallback exists to cover -- treating output as free would understate every call."""
        usage = estimate_tokens(prompt_chars=1, image_count=0)
        assert usage.output_tokens > 0

    def test_never_negative_for_empty_input(self) -> None:
        usage = estimate_tokens(prompt_chars=0, image_count=0)
        assert usage.total_tokens >= 0


class TestBudgetGovernorConstruction:
    def test_rejects_a_non_positive_limit(self) -> None:
        clock = ManualClock()
        with pytest.raises(ValueError):
            governor(clock=clock, limit=0.0)

    def test_rejects_max_interval_below_base_interval(self) -> None:
        clock = ManualClock()
        with pytest.raises(ValueError):
            governor(clock=clock, base_interval_ms=200.0, max_interval_ms=100.0)

    def test_rejects_min_frames_above_base_frames(self) -> None:
        clock = ManualClock()
        with pytest.raises(ValueError):
            governor(clock=clock, base_frames=2, min_frames=3)

    def test_rejects_a_degrade_fraction_outside_zero_to_one(self) -> None:
        clock = ManualClock()
        with pytest.raises(ValueError):
            governor(clock=clock, degrade_at_fraction=1.5)
        with pytest.raises(ValueError):
            governor(clock=clock, degrade_at_fraction=0.0)


class TestBudgetGovernorDecide:
    def test_starts_normal_with_no_recorded_usage(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, base_interval_ms=100.0, base_frames=4)
        decision = gov.decide()
        assert decision.level == BudgetLevel.NORMAL
        assert decision.min_interval_ms == 100.0
        assert decision.frames_per_observation == 4
        assert decision.should_propose is True

    def test_stays_normal_below_the_degrade_threshold(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0, degrade_at_fraction=0.8)
        gov.record(TokenUsage(input_tokens=4000, output_tokens=0))  # 4000 < 0.8*6000
        assert gov.decide().level == BudgetLevel.NORMAL

    def test_degrades_once_the_rate_crosses_the_threshold(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0, degrade_at_fraction=0.8,
                       base_interval_ms=100.0, base_frames=4, max_interval_ms=400.0,
                       min_frames=1)
        gov.record(TokenUsage(input_tokens=5000, output_tokens=0))  # 5000 >= 0.8*6000
        decision = gov.decide()
        assert decision.level == BudgetLevel.DEGRADED
        assert decision.min_interval_ms > 100.0
        assert decision.frames_per_observation < 4
        assert decision.should_propose is True

    def test_degraded_interval_never_exceeds_the_configured_ceiling(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0, degrade_at_fraction=0.8,
                       base_interval_ms=300.0, max_interval_ms=400.0)
        gov.record(TokenUsage(input_tokens=5000, output_tokens=0))
        assert gov.decide().min_interval_ms <= 400.0

    def test_degraded_frames_never_drop_below_the_configured_floor(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0, degrade_at_fraction=0.8,
                       base_frames=2, min_frames=2)
        gov.record(TokenUsage(input_tokens=5000, output_tokens=0))
        assert gov.decide().frames_per_observation >= 2

    def test_exhausts_once_the_rate_reaches_the_hard_limit(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0)
        gov.record(TokenUsage(input_tokens=6500, output_tokens=0))
        decision = gov.decide()
        assert decision.level == BudgetLevel.EXHAUSTED
        assert decision.should_propose is False

    def test_exhausted_still_returns_a_finite_bounded_interval(self) -> None:
        """A caller that stops proposing must still know how long to wait before checking
        again -- an infinite or zero interval here would either busy-loop or never recover."""
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0, max_interval_ms=400.0)
        gov.record(TokenUsage(input_tokens=6500, output_tokens=0))
        decision = gov.decide()
        assert 0.0 < decision.min_interval_ms <= 400.0

    def test_recovers_to_normal_once_old_usage_ages_out_of_the_window(self) -> None:
        """Degradation must not be a one-way trip: a single burst that then goes quiet has
        to let the agent return to full cadence, not silence it for the rest of the session."""
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0, window_seconds=60.0)
        gov.record(TokenUsage(input_tokens=6500, output_tokens=0))
        assert gov.decide().level == BudgetLevel.EXHAUSTED
        clock.advance(61_000.0)  # past the trailing window
        assert gov.decide().level == BudgetLevel.NORMAL

    def test_the_limit_scales_with_a_non_default_window_length(self) -> None:
        """tokens_per_minute_limit names a per-MINUTE rate. Comparing it directly against a
        window of a different length silently redefines what the number means: a 10s window
        compared raw against a 6000-token 'per minute' limit would actually admit 36000
        tokens/minute, six times the configured rate. A 30s window must therefore exhaust at
        HALF the per-minute figure, not at the same raw number a 60s window would."""
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0, window_seconds=30.0,
                       base_interval_ms=100.0, max_interval_ms=400.0)
        gov.record(TokenUsage(input_tokens=3100, output_tokens=0))  # > half of 6000
        assert gov.decide().level == BudgetLevel.EXHAUSTED

    def test_a_longer_window_does_not_exhaust_at_the_bare_per_minute_figure(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0, window_seconds=120.0,
                       base_interval_ms=100.0, max_interval_ms=400.0)
        gov.record(TokenUsage(input_tokens=6500, output_tokens=0))  # > 6000, but < 2x6000
        assert gov.decide().level != BudgetLevel.EXHAUSTED

    def test_an_unrecorded_call_beyond_the_window_does_not_still_count(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0, window_seconds=60.0)
        gov.record(TokenUsage(input_tokens=3000, output_tokens=0))
        clock.advance(30_000.0)
        gov.record(TokenUsage(input_tokens=3000, output_tokens=0))
        # Both calls are still inside a 60s window from "now" at t=30s.
        assert gov.decide().level != BudgetLevel.NORMAL
        clock.advance(31_000.0)  # first call now outside the window, second still just inside
        stats = gov.stats
        assert stats.tokens_in_window == 3000

    def test_record_none_still_accounts_for_an_unknown_call(self) -> None:
        """A transport that cannot report real usage (the CLI fallback) must not be invisible
        to the governor -- an untracked call is a hole a real session could fall through."""
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0)
        gov.record(None)
        assert gov.stats.tokens_in_window > 0


class TestBudgetGovernorStats:
    def test_reports_the_current_level_and_rate(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, limit=6000.0)
        gov.record(TokenUsage(input_tokens=1000, output_tokens=0))
        stats = gov.stats
        assert stats.level == BudgetLevel.NORMAL
        assert stats.tokens_in_window == 1000


class TestBudgetGovernorConcurrency:
    def test_concurrent_record_calls_are_not_lost(self) -> None:
        clock = ManualClock()
        gov = governor(clock=clock, limit=10_000_000.0)

        def record_many() -> None:
            for _ in range(200):
                gov.record(TokenUsage(input_tokens=1, output_tokens=0))

        threads = [threading.Thread(target=record_many) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert gov.stats.tokens_in_window == 800
