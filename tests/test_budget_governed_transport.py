"""Contract for player2.agent.budget.GovernedTransport -- feeding the governor for free.

The whole point of this wrapper: SDKPolicy and the three Unit 7 transports need ZERO code
changes to become budget-aware. GovernedTransport implements IModelTransport itself, delegates
to any inner transport unchanged, and records usage into a BudgetGovernor as a side effect --
so wiring budgeting in is "wrap the transport", not "modify the policy".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from player2.agent.budget import BudgetGovernor, TokenUsage
from player2.clock import ManualClock


@dataclass
class FakeTransportWithUsage:
    """A transport that can report real usage, like the real SDK adapters after this unit."""

    tool_result: dict[str, Any]
    text_result: str = "a goal"
    last_usage: TokenUsage | None = None

    def complete_tool(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return self.tool_result

    def complete_text(self, **kwargs: Any) -> str:
        del kwargs
        return self.text_result


@dataclass
class FakeTransportWithoutUsage:
    """A transport with no usage telemetry at all, like the CLI fallback."""

    tool_result: dict[str, Any]
    text_result: str = "a goal"

    def complete_tool(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        return self.tool_result

    def complete_text(self, **kwargs: Any) -> str:
        del kwargs
        return self.text_result


@dataclass
class FailingTransport:
    error: Exception

    def complete_tool(self, **kwargs: Any) -> dict[str, Any]:
        del kwargs
        raise self.error

    def complete_text(self, **kwargs: Any) -> str:
        del kwargs
        raise self.error


def make_governor(limit: float = 1_000_000.0) -> BudgetGovernor:
    return BudgetGovernor(
        tokens_per_minute_limit=limit,
        base_interval_ms=100.0,
        base_frames_per_observation=4,
        clock=ManualClock(),
    )


class TestDelegation:
    def test_complete_tool_returns_the_inner_result_unchanged(self) -> None:
        from player2.agent.budget import GovernedTransport

        inner = FakeTransportWithUsage(tool_result={"keyframes": []})
        transport = GovernedTransport(inner=inner, governor=make_governor())
        result = transport.complete_tool(system="s", prompt="p", images=(), tool_name="t",
                                         tool_schema={}, timeout_s=5.0)
        assert result == {"keyframes": []}

    def test_complete_text_returns_the_inner_result_unchanged(self) -> None:
        from player2.agent.budget import GovernedTransport

        inner = FakeTransportWithUsage(tool_result={}, text_result="clear the room")
        transport = GovernedTransport(inner=inner, governor=make_governor())
        result = transport.complete_text(system="s", prompt="p", images=(), timeout_s=5.0)
        assert result == "clear the room"

    def test_a_failing_inner_call_propagates_unchanged(self) -> None:
        from player2.agent.budget import GovernedTransport

        error = RuntimeError("provider down")
        transport = GovernedTransport(inner=FailingTransport(error=error), governor=make_governor())
        with pytest.raises(RuntimeError, match="provider down"):
            transport.complete_tool(system="s", prompt="p", images=(), tool_name="t",
                                    tool_schema={}, timeout_s=5.0)


class TestUsageRecording:
    def test_records_the_inner_transports_real_usage(self) -> None:
        from player2.agent.budget import GovernedTransport

        inner = FakeTransportWithUsage(
            tool_result={"keyframes": []},
            last_usage=TokenUsage(input_tokens=500, output_tokens=20),
        )
        gov = make_governor()
        transport = GovernedTransport(inner=inner, governor=gov)
        transport.complete_tool(system="s", prompt="p", images=(), tool_name="t",
                                tool_schema={}, timeout_s=5.0)
        assert gov.stats.tokens_in_window == 520

    def test_falls_back_to_an_estimate_when_the_inner_transport_has_no_usage(self) -> None:
        """The CLI transport cannot report real token counts. Falling back to an estimate
        keeps sustained CLI use visible to the governor instead of silently free."""
        from player2.agent.budget import GovernedTransport

        inner = FakeTransportWithoutUsage(tool_result={"keyframes": []})
        gov = make_governor()
        transport = GovernedTransport(inner=inner, governor=gov)
        transport.complete_tool(system="s", prompt="some prompt text", images=(),
                                tool_name="t", tool_schema={}, timeout_s=5.0)
        assert gov.stats.tokens_in_window > 0

    def test_a_failing_call_still_records_a_conservative_estimate(self) -> None:
        """A response that fails local parsing (the model wouldn't call the tool, malformed
        JSON, ...) was still generated and billed by the provider. Recording nothing here
        would make the governor blind to exactly the failure mode a flaky or small local
        model is most likely to produce -- unbounded retries at real cost, budget cap never
        once triggering because it never saw any of them."""
        from player2.agent.budget import GovernedTransport

        error = RuntimeError("boom")
        gov = make_governor()
        transport = GovernedTransport(inner=FailingTransport(error=error), governor=gov)
        with pytest.raises(RuntimeError):
            transport.complete_tool(system="s", prompt="p" * 400, images=(), tool_name="t",
                                    tool_schema={}, timeout_s=5.0)
        assert gov.stats.tokens_in_window > 0

    def test_a_failing_call_does_not_charge_a_stale_usage_from_a_prior_success(self) -> None:
        """last_usage retains whatever a PREVIOUS successful call reported (see the
        transports' own last_usage contract) -- reading it after a failure would double-count
        that earlier call as if it were this one instead of estimating this call fresh."""
        from player2.agent.budget import GovernedTransport

        inner = FakeTransportWithUsage(
            tool_result={"keyframes": []},
            last_usage=TokenUsage(input_tokens=999_000, output_tokens=0),
        )
        gov = make_governor(limit=100.0)
        transport = GovernedTransport(inner=inner, governor=gov)
        transport.complete_tool(system="s", prompt="p", images=(), tool_name="t",
                                tool_schema={}, timeout_s=5.0)  # succeeds, records 999,000+

        failing_inner = FakeTransportWithUsage(
            tool_result={"keyframes": []},
            last_usage=TokenUsage(input_tokens=999_000, output_tokens=0),
        )

        def now_failing(**kwargs: object) -> dict[str, object]:
            raise RuntimeError("boom")

        failing_inner.complete_tool = now_failing  # type: ignore[method-assign]
        transport = GovernedTransport(inner=failing_inner, governor=gov)
        before = gov.stats.tokens_in_window
        with pytest.raises(RuntimeError):
            transport.complete_tool(system="s", prompt="p", images=(), tool_name="t",
                                    tool_schema={}, timeout_s=5.0)
        added = gov.stats.tokens_in_window - before
        assert added < 999_000, (
            f"recorded {added} tokens for a failed call whose transport's last_usage still "
            "held a previous successful call's 999,000+ -- looks like the stale value leaked"
        )

    def test_a_broken_governor_does_not_turn_a_successful_call_into_a_failure(self) -> None:
        from player2.agent.budget import GovernedTransport

        class BrokenGovernor:
            def record(self, usage: object) -> None:
                raise RuntimeError("governor is broken")

            def decide(self) -> object:
                raise AssertionError("not used by GovernedTransport")

        inner = FakeTransportWithUsage(tool_result={"keyframes": []})
        transport = GovernedTransport(inner=inner, governor=BrokenGovernor())  # type: ignore[arg-type]
        result = transport.complete_tool(system="s", prompt="p", images=(), tool_name="t",
                                         tool_schema={}, timeout_s=5.0)
        assert result == {"keyframes": []}

    def test_a_broken_governor_does_not_mask_a_real_call_failure(self) -> None:
        from player2.agent.budget import GovernedTransport

        class BrokenGovernor:
            def record(self, usage: object) -> None:
                raise RuntimeError("governor is broken")

            def decide(self) -> object:
                raise AssertionError("not used by GovernedTransport")

        error = ValueError("the real failure")
        transport = GovernedTransport(inner=FailingTransport(error=error),
                                      governor=BrokenGovernor())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="the real failure"):
            transport.complete_tool(system="s", prompt="p", images=(), tool_name="t",
                                    tool_schema={}, timeout_s=5.0)

    def test_records_before_returning_so_the_next_decide_sees_it(self) -> None:
        from player2.agent.budget import GovernedTransport

        inner = FakeTransportWithUsage(
            tool_result={"keyframes": []},
            last_usage=TokenUsage(input_tokens=999_999, output_tokens=0),
        )
        gov = make_governor(limit=100.0)
        transport = GovernedTransport(inner=inner, governor=gov)
        transport.complete_tool(system="s", prompt="p", images=(), tool_name="t",
                                tool_schema={}, timeout_s=5.0)
        from player2.agent.budget import BudgetLevel
        assert gov.decide().level == BudgetLevel.EXHAUSTED
