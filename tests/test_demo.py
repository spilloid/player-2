"""Contract for demo.py's live --verbose formatting and --overlay wiring.

Only the pure formatting/composition helpers are covered here -- everything else in demo.py is
CLI glue that drives real hardware (a virtual pad, a capture backend) or a real Tkinter window
and is exercised live instead.
"""

from __future__ import annotations

from player2.agent.loop import DecisionEvent, DecisionOutcome
from player2.contracts import ActionChunk, Keyframe
from player2.demo import _combine_on_decision, _format_decision, _overlay_text


def accepted(*, commentary: str | None = None, decision_seq: int = 1) -> DecisionEvent:
    chunk = ActionChunk(
        keyframes=(Keyframe(t_ms=0.0, left_stick=(1.0, 0.0)), Keyframe(t_ms=300.0)),
        commentary=commentary,
    )
    return DecisionEvent(decision_seq=decision_seq, elapsed_ms=1234.0,
                         outcome=DecisionOutcome.ACCEPTED, chunk=chunk)


class TestFormatDecision:
    def test_shows_commentary_when_the_model_provided_one(self) -> None:
        """The whole point: a human watching live should see what the model says it's
        doing, not have to parse stick vectors and button sets to guess."""
        line = _format_decision(accepted(commentary="circling the ore patch for a refill"))
        assert "circling the ore patch for a refill" in line

    def test_omits_raw_controller_state_when_commentary_is_present(self) -> None:
        line = _format_decision(accepted(commentary="circling the ore patch for a refill"))
        assert "left_stick" not in line
        assert "buttons" not in line

    def test_falls_back_to_controller_state_when_commentary_is_absent(self) -> None:
        """Scripted policies and terse models never set commentary -- --verbose must still
        show something useful rather than going blank."""
        line = _format_decision(accepted(commentary=None))
        assert "left_stick" in line
        assert "buttons" in line

    def test_includes_the_decision_sequence_and_outcome_either_way(self) -> None:
        for line in (_format_decision(accepted(commentary="on my way")),
                    _format_decision(accepted(commentary=None))):
            assert "1" in line
            assert "ACCEPTED" in line


class TestOverlayText:
    """Unlike _format_decision's terminal line, the overlay has no room for controller state
    or timing -- it exists purely to answer "what does the model say it's doing right now," so
    a missing commentary is reported explicitly rather than falling back to a stick/button
    dump the way the terminal line does."""

    def test_shows_commentary_alone_when_present(self) -> None:
        text = _overlay_text(accepted(commentary="circling back for ammo"))
        assert text == "circling back for ammo"

    def test_says_so_explicitly_when_commentary_is_absent(self) -> None:
        text = _overlay_text(accepted(commentary=None))
        assert "no commentary" in text.lower()

    def test_reports_a_policy_error(self) -> None:
        event = DecisionEvent(decision_seq=2, elapsed_ms=10.0, outcome=DecisionOutcome.ERROR,
                              detail="boom")
        assert "boom" in _overlay_text(event)

    def test_reports_no_proposal(self) -> None:
        event = DecisionEvent(decision_seq=3, elapsed_ms=10.0, outcome=DecisionOutcome.NONE)
        assert "nothing" in _overlay_text(event).lower()


class TestCombineOnDecision:
    def test_returns_none_for_no_callbacks(self) -> None:
        assert _combine_on_decision([]) is None

    def test_returns_the_callback_itself_when_there_is_only_one(self) -> None:
        seen = []
        callback = seen.append
        assert _combine_on_decision([callback]) is callback

    def test_fans_one_event_out_to_every_callback(self) -> None:
        first: list[DecisionEvent] = []
        second: list[DecisionEvent] = []
        combined = _combine_on_decision([first.append, second.append])
        assert combined is not None
        event = accepted()
        combined(event)
        assert first == [event]
        assert second == [event]

    def test_one_callback_raising_does_not_stop_the_others(self) -> None:
        """Matches the isolation AgentLoop._report_decision already gives a single observer
        (a broken callback must never affect cognition or another, unrelated observer) --
        --verbose and --overlay running together must not let a bug in one silence the other."""
        def boom(event: DecisionEvent) -> None:
            raise RuntimeError("broken observer")

        seen: list[DecisionEvent] = []
        combined = _combine_on_decision([boom, seen.append])
        assert combined is not None
        event = accepted()
        combined(event)  # must not raise
        assert seen == [event]
