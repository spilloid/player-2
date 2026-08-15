"""Contract for player2.contracts -- the data types the whole runtime agrees on.

This module is deliberately pure: no threads, no I/O, no clock. All of the subtle
semantics of "what should the controller be doing right now" live here as testable
functions, so that the scheduler in control/ is reduced to timing glue.

Three rules from the architecture review drive most of this file:

  * A Keyframe is authored *sparsely* but resolves to a *complete* pad snapshot.
    The scheduler must never have to reason about partial state.
  * Absent is not empty. `buttons=None` inherits; `buttons=frozenset()` releases.
    Collapsing these two causes silent stuck-button bugs.
  * A chunk resolves against NEUTRAL, never against whatever the pad is doing now.
    Anything a chunk does not mention is released. This is what stops a chunk that
    only says "press A" from silently inheriting a held stick and walking the
    character into the void forever.
"""

import math
from dataclasses import FrozenInstanceError

import pytest

from player2.contracts import (
    MAX_CHUNK_MS,
    MAX_COMMENTARY_CHARS,
    MAX_KEYFRAMES,
    MIN_PULSE_MS,
    NEUTRAL,
    TICK_HZ,
    TICK_MS,
    ActionChunk,
    Button,
    Frame,
    InvalidChunk,
    InvalidFrame,
    InvalidPadState,
    Keyframe,
    PadState,
    ResolvedKeyframe,
    chunk_from_dict,
    chunk_to_dict,
    resolve,
    sample,
)


def kc(*keyframes: Keyframe, **kw: object) -> ActionChunk:
    """Shorthand: build a chunk from keyframes."""
    return ActionChunk(keyframes=tuple(keyframes), **kw)  # type: ignore[arg-type]


class TestTickConstants:
    def test_tick_rate_is_120hz(self) -> None:
        assert TICK_HZ == 120.0
        assert TICK_MS == pytest.approx(1000.0 / 120.0)

    def test_min_pulse_is_two_ticks(self) -> None:
        """A button edge shorter than two ticks can fall between samples and never
        reach the game at all. Such chunks are rejected rather than silently dropped."""
        assert MIN_PULSE_MS == pytest.approx(2.0 * TICK_MS)


class TestPadState:
    def test_neutral_is_all_zero_and_no_buttons(self) -> None:
        assert NEUTRAL.left_stick == (0.0, 0.0)
        assert NEUTRAL.right_stick == (0.0, 0.0)
        assert NEUTRAL.left_trigger == 0.0
        assert NEUTRAL.right_trigger == 0.0
        assert NEUTRAL.buttons == frozenset()

    def test_is_frozen(self) -> None:
        with pytest.raises(FrozenInstanceError):
            NEUTRAL.left_trigger = 1.0  # type: ignore[misc]

    def test_is_hashable_and_value_comparable(self) -> None:
        assert PadState(left_trigger=0.5) == PadState(left_trigger=0.5)
        assert len({PadState(left_trigger=0.5), PadState(left_trigger=0.5)}) == 1

    @pytest.mark.parametrize("value", [1.0, -1.0, 0.0, 0.5])
    def test_accepts_sticks_in_range(self, value: float) -> None:
        PadState(left_stick=(value, value), right_stick=(value, value))

    @pytest.mark.parametrize("value", [1.001, -1.001, 2.0, math.nan, math.inf, -math.inf])
    def test_rejects_out_of_range_or_non_finite_sticks(self, value: float) -> None:
        """Rejected, never clamped. Clamping hides model errors; rejection surfaces them."""
        with pytest.raises(InvalidPadState):
            PadState(left_stick=(value, 0.0))
        with pytest.raises(InvalidPadState):
            PadState(right_stick=(0.0, value))

    @pytest.mark.parametrize("value", [0.0, 0.5, 1.0])
    def test_accepts_triggers_in_range(self, value: float) -> None:
        PadState(left_trigger=value, right_trigger=value)

    @pytest.mark.parametrize("value", [-0.001, 1.001, math.nan, math.inf])
    def test_rejects_out_of_range_or_non_finite_triggers(self, value: float) -> None:
        with pytest.raises(InvalidPadState):
            PadState(left_trigger=value)
        with pytest.raises(InvalidPadState):
            PadState(right_trigger=value)

    def test_rejects_unknown_button(self) -> None:
        with pytest.raises(InvalidPadState):
            PadState(buttons=frozenset({"NOT_A_BUTTON"}))  # type: ignore[arg-type]


class TestButton:
    def test_covers_the_xbox_pad(self) -> None:
        expected = {
            "A", "B", "X", "Y",
            "LB", "RB",
            "BACK", "START", "GUIDE",
            "LS", "RS",
            "DPAD_UP", "DPAD_DOWN", "DPAD_LEFT", "DPAD_RIGHT",
        }
        assert {b.value for b in Button} == expected

    def test_is_string_valued_for_serialization(self) -> None:
        assert Button.A == "A"
        assert Button("A") is Button.A


class TestKeyframeAuthoring:
    def test_fields_default_to_none_meaning_inherit(self) -> None:
        kf = Keyframe(t_ms=0.0)
        assert kf.left_stick is None
        assert kf.right_stick is None
        assert kf.left_trigger is None
        assert kf.right_trigger is None
        assert kf.buttons is None

    def test_empty_buttons_is_distinct_from_none(self) -> None:
        assert Keyframe(t_ms=0.0, buttons=frozenset()).buttons == frozenset()
        assert Keyframe(t_ms=0.0, buttons=frozenset()) != Keyframe(t_ms=0.0)

    def test_does_not_validate_at_construction(self) -> None:
        """Keyframe is an authoring/wire type. Validation is a whole-chunk concern,
        so that a malformed chunk produces one InvalidChunk rather than an arbitrary
        failure at whichever field happened to be built first."""
        Keyframe(t_ms=-5.0, left_trigger=99.0)


class TestValidateChunk:
    def test_accepts_a_minimal_chunk(self) -> None:
        resolve(kc(Keyframe(t_ms=0.0)))

    def test_rejects_empty_chunk(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc())

    def test_rejects_negative_time(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=-1.0)))

    def test_rejects_duplicate_times(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0), Keyframe(t_ms=0.0)))

    def test_rejects_non_monotonic_times(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0), Keyframe(t_ms=50.0), Keyframe(t_ms=25.0)))

    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
    def test_rejects_non_finite_time(self, value: float) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=value)))

    @pytest.mark.parametrize("value", [1.5, -1.5, math.nan, math.inf])
    def test_rejects_out_of_range_analog(self, value: float) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0, left_stick=(value, 0.0))))

    def test_rejects_trigger_out_of_range(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0, left_trigger=-0.5)))

    def test_rejects_unknown_button(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0, buttons=frozenset({"NOPE"}))))  # type: ignore[arg-type]

    def test_rejects_negative_decision_seq(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0), decision_seq=-1))


class TestMinimumPulse:
    def test_rejects_button_press_shorter_than_two_ticks(self) -> None:
        """Pressed at 0, released at 5ms: a 120Hz sampler can miss it entirely."""
        chunk = kc(
            Keyframe(t_ms=0.0, buttons=frozenset({Button.A})),
            Keyframe(t_ms=5.0, buttons=frozenset()),
        )
        with pytest.raises(InvalidChunk):
            resolve(chunk)

    def test_accepts_button_press_of_exactly_two_ticks(self) -> None:
        chunk = kc(
            Keyframe(t_ms=0.0, buttons=frozenset({Button.A})),
            Keyframe(t_ms=MIN_PULSE_MS, buttons=frozenset()),
        )
        resolve(chunk)

    def test_rejects_release_gap_shorter_than_two_ticks(self) -> None:
        """A blink-length *release* between two presses is equally invisible."""
        chunk = kc(
            Keyframe(t_ms=0.0, buttons=frozenset({Button.A})),
            Keyframe(t_ms=100.0, buttons=frozenset()),
            Keyframe(t_ms=103.0, buttons=frozenset({Button.A})),
            Keyframe(t_ms=200.0, buttons=frozenset()),
        )
        with pytest.raises(InvalidChunk):
            resolve(chunk)

    def test_button_still_held_at_chunk_end_is_not_a_short_pulse(self) -> None:
        """The final state persists until the deadman fires, so a press that is never
        released inside the chunk is held for at least max_hold_ms -- not a blink."""
        chunk = kc(
            Keyframe(t_ms=0.0),
            Keyframe(t_ms=100.0, buttons=frozenset({Button.A})),
        )
        resolve(chunk)

    def test_measures_the_whole_held_interval_not_keyframe_spacing(self) -> None:
        """A is held 0..25ms across an intermediate keyframe that only moves a stick.
        Naively comparing adjacent button-differing keyframes (20 -> 25) would reject
        this valid chunk."""
        chunk = kc(
            Keyframe(t_ms=0.0, buttons=frozenset({Button.A})),
            Keyframe(t_ms=20.0, left_stick=(1.0, 0.0)),
            Keyframe(t_ms=25.0, buttons=frozenset()),
        )
        resolve(chunk)


class TestResolve:
    def test_returns_resolved_keyframes_with_complete_states(self) -> None:
        out = resolve(kc(Keyframe(t_ms=0.0, left_trigger=0.5)))
        assert isinstance(out[0], ResolvedKeyframe)
        assert out[0].t_ms == 0.0
        assert isinstance(out[0].state, PadState)

    def test_first_keyframe_inherits_from_neutral_not_from_anywhere_else(self) -> None:
        """The single most important rule in the runtime: unspecified means released."""
        out = resolve(kc(Keyframe(t_ms=0.0, buttons=frozenset({Button.A}))))
        assert out[0].state == PadState(buttons=frozenset({Button.A}))
        assert out[0].state.left_stick == (0.0, 0.0)
        assert out[0].state.right_trigger == 0.0

    def test_later_keyframes_inherit_unspecified_fields_from_previous(self) -> None:
        out = resolve(
            kc(
                Keyframe(t_ms=0.0, left_stick=(1.0, 0.0), left_trigger=0.25),
                Keyframe(t_ms=100.0, right_trigger=1.0),
            )
        )
        assert out[1].state.left_stick == (1.0, 0.0)
        assert out[1].state.left_trigger == 0.25
        assert out[1].state.right_trigger == 1.0

    def test_none_buttons_inherits_held_buttons(self) -> None:
        out = resolve(
            kc(
                Keyframe(t_ms=0.0, buttons=frozenset({Button.A, Button.B})),
                Keyframe(t_ms=100.0, left_stick=(0.5, 0.0)),
            )
        )
        assert out[1].state.buttons == frozenset({Button.A, Button.B})

    def test_empty_buttons_releases_everything(self) -> None:
        out = resolve(
            kc(
                Keyframe(t_ms=0.0, buttons=frozenset({Button.A, Button.B})),
                Keyframe(t_ms=100.0, buttons=frozenset()),
            )
        )
        assert out[1].state.buttons == frozenset()

    def test_button_set_replaces_rather_than_merges(self) -> None:
        out = resolve(
            kc(
                Keyframe(t_ms=0.0, buttons=frozenset({Button.A})),
                Keyframe(t_ms=100.0, buttons=frozenset({Button.B})),
            )
        )
        assert out[1].state.buttons == frozenset({Button.B})

    def test_is_pure_and_repeatable(self) -> None:
        chunk = kc(Keyframe(t_ms=0.0, left_stick=(0.3, 0.4)), Keyframe(t_ms=50.0))
        assert resolve(chunk) == resolve(chunk)

    def test_does_not_depend_on_any_live_pad_state(self) -> None:
        """resolve() takes only the chunk. There is deliberately no way to pass in
        'what the pad is doing right now' -- that is what makes invariant #2 structural
        rather than a convention someone has to remember."""
        import inspect

        assert list(inspect.signature(resolve).parameters) == ["chunk"]


class TestSample:
    def test_chunk_must_begin_at_zero(self) -> None:
        """Review finding: if a chunk's first keyframe is at t=50, it is ambiguous what the
        pad should do during 0..50 -- hold the upcoming state (acting early) or stay neutral.
        Rather than pick, the ambiguous region is legislated out of existence. "Wait, then
        act" stays expressible as an explicit neutral keyframe at t=0."""
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=50.0, left_trigger=1.0)))

    def test_wait_then_act_is_expressible_explicitly(self) -> None:
        r = resolve(kc(Keyframe(t_ms=0.0), Keyframe(t_ms=50.0, left_trigger=1.0)))
        assert sample(r, 0.0).left_trigger == 0.0
        assert sample(r, 50.0).left_trigger == 1.0

    def test_holds_last_state_after_the_final_keyframe(self) -> None:
        """Holding is correct here; releasing is the deadman's job, not the sampler's."""
        r = resolve(kc(Keyframe(t_ms=0.0), Keyframe(t_ms=100.0, left_stick=(1.0, 0.0))))
        assert sample(r, 5000.0).left_stick == (1.0, 0.0)

    def test_interpolates_sticks_linearly(self) -> None:
        r = resolve(
            kc(
                Keyframe(t_ms=0.0, left_stick=(0.0, 0.0)),
                Keyframe(t_ms=100.0, left_stick=(1.0, -1.0)),
            )
        )
        assert sample(r, 50.0).left_stick == pytest.approx((0.5, -0.5))
        assert sample(r, 25.0).left_stick == pytest.approx((0.25, -0.25))

    def test_interpolates_triggers_linearly(self) -> None:
        r = resolve(
            kc(Keyframe(t_ms=0.0, right_trigger=0.0), Keyframe(t_ms=200.0, right_trigger=1.0))
        )
        assert sample(r, 100.0).right_trigger == pytest.approx(0.5)

    def test_hits_keyframe_values_exactly_at_keyframe_times(self) -> None:
        r = resolve(
            kc(
                Keyframe(t_ms=0.0, left_stick=(0.0, 0.0)),
                Keyframe(t_ms=100.0, left_stick=(1.0, 0.0)),
            )
        )
        assert sample(r, 0.0).left_stick == pytest.approx((0.0, 0.0))
        assert sample(r, 100.0).left_stick == pytest.approx((1.0, 0.0))

    def test_buttons_step_and_do_not_interpolate(self) -> None:
        r = resolve(
            kc(
                Keyframe(t_ms=0.0, buttons=frozenset({Button.A})),
                Keyframe(t_ms=100.0, buttons=frozenset()),
            )
        )
        assert sample(r, 0.0).buttons == frozenset({Button.A})
        assert sample(r, 99.9).buttons == frozenset({Button.A})
        assert sample(r, 100.0).buttons == frozenset()
        assert sample(r, 150.0).buttons == frozenset()

    def test_interpolation_spans_keyframes_that_omit_the_channel(self) -> None:
        """The middle keyframe says nothing about the stick, so it resolves to the
        inherited value -- which means the stick holds 1.0 through it, then ramps down.
        Interpolation is always between *resolved* neighbours, never across them."""
        r = resolve(
            kc(
                Keyframe(t_ms=0.0, left_stick=(1.0, 0.0)),
                Keyframe(t_ms=100.0, buttons=frozenset({Button.A})),
                Keyframe(t_ms=200.0, left_stick=(0.0, 0.0)),
            )
        )
        assert sample(r, 50.0).left_stick == pytest.approx((1.0, 0.0))
        assert sample(r, 100.0).left_stick == pytest.approx((1.0, 0.0))
        assert sample(r, 150.0).left_stick == pytest.approx((0.5, 0.0))

    def test_rejects_negative_sample_time(self) -> None:
        r = resolve(kc(Keyframe(t_ms=0.0)))
        with pytest.raises(ValueError):
            sample(r, -1.0)

    def test_never_produces_an_invalid_state(self) -> None:
        r = resolve(
            kc(
                Keyframe(t_ms=0.0, left_stick=(-1.0, 1.0), left_trigger=1.0),
                Keyframe(t_ms=100.0, left_stick=(1.0, -1.0), left_trigger=0.0),
            )
        )
        for t in range(0, 120):
            s = sample(r, float(t))
            assert -1.0 <= s.left_stick[0] <= 1.0
            assert 0.0 <= s.left_trigger <= 1.0


class TestChunkMetadata:
    def test_defaults(self) -> None:
        c = kc(Keyframe(t_ms=0.0))
        assert c.decision_seq == 0
        assert c.epoch == 0
        assert c.observation_cutoff_ms is None

    def test_carries_provenance(self) -> None:
        """decision_seq lets the scheduler drop a slow proposal that lost its race;
        epoch lets it drop anything authored before a human takeover."""
        c = kc(Keyframe(t_ms=0.0), decision_seq=7, epoch=3, observation_cutoff_ms=1234.5)
        assert (c.decision_seq, c.epoch, c.observation_cutoff_ms) == (7, 3, 1234.5)

    def test_duration_ms_is_the_final_keyframe_time(self) -> None:
        assert kc(Keyframe(t_ms=0.0), Keyframe(t_ms=250.0)).duration_ms == 250.0


class TestCommentary:
    """A short, model-authored line of "what I'm doing and why" -- purely for a human
    watching live (e.g. demo.py --verbose). The runtime never interprets it, so it carries
    none of the safety weight a controller channel does; it only needs a type and a length
    bound so an unbounded model response cannot blow up a prompt, a log line, or a recording.
    """

    def test_defaults_to_none(self) -> None:
        assert kc(Keyframe(t_ms=0.0)).commentary is None

    def test_accepts_a_short_string(self) -> None:
        chunk = kc(Keyframe(t_ms=0.0), commentary="heading toward the ore patch")
        resolve(chunk)
        assert chunk.commentary == "heading toward the ore patch"

    def test_rejects_non_string(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0), commentary=5))  # type: ignore[arg-type]

    def test_accepts_exactly_the_length_limit(self) -> None:
        resolve(kc(Keyframe(t_ms=0.0), commentary="x" * MAX_COMMENTARY_CHARS))

    def test_rejects_over_the_length_limit(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0), commentary="x" * (MAX_COMMENTARY_CHARS + 1)))

    @pytest.mark.parametrize("bad", ["line one\nline two", "line\rone", "with\ttab", "\x1b[31mred"])
    def test_rejects_control_characters(self, bad: str) -> None:
        """A model-authored newline or ANSI escape here could forge a fake --verbose log
        line (e.g. embed something that reads as a second, fabricated decision) once
        demo.py interpolates this string directly into a one-line live record. Rejecting
        at the boundary, rather than trusting the formatter to sanitize, keeps every
        consumer of `commentary` -- --verbose today, a future one tomorrow -- safe by
        construction instead of by remembering to escape."""
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0), commentary=bad))

    def test_accepts_ordinary_punctuation(self) -> None:
        resolve(kc(Keyframe(t_ms=0.0), commentary="mining iron -- careful, biters nearby!"))


class TestSerialization:
    def test_round_trips(self) -> None:
        c = kc(
            Keyframe(t_ms=0.0, left_stick=(0.5, -0.25), buttons=frozenset({Button.A})),
            Keyframe(t_ms=200.0, right_trigger=1.0, buttons=frozenset()),
            decision_seq=4,
            epoch=2,
            observation_cutoff_ms=99.5,
            commentary="mining toward the visible ore patch",
        )
        assert chunk_from_dict(chunk_to_dict(c)) == c

    def test_absent_commentary_serializes_as_a_missing_key(self) -> None:
        d = chunk_to_dict(kc(Keyframe(t_ms=0.0)))
        assert "commentary" not in d

    def test_empty_commentary_serializes_as_a_missing_key(self) -> None:
        """An explicit "" carries the same "nothing to say" meaning as never setting the
        field at all -- unlike buttons, there is no controller-state distinction between
        absent and empty here, so collapsing them on the wire avoids storing a meaningless
        empty key in every recording and keeps demo.py's truthy display check honest."""
        d = chunk_to_dict(kc(Keyframe(t_ms=0.0), commentary=""))
        assert "commentary" not in d

    def test_commentary_round_trips_when_present(self) -> None:
        c = kc(Keyframe(t_ms=0.0), commentary="dodging the spitter")
        assert chunk_from_dict(chunk_to_dict(c)).commentary == "dodging the spitter"

    def test_absent_buttons_serializes_as_a_missing_key(self) -> None:
        d = chunk_to_dict(kc(Keyframe(t_ms=0.0)))
        assert "buttons" not in d["keyframes"][0]

    def test_empty_buttons_serializes_as_an_empty_list(self) -> None:
        d = chunk_to_dict(kc(Keyframe(t_ms=0.0, buttons=frozenset())))
        assert d["keyframes"][0]["buttons"] == []

    def test_absent_and_empty_survive_a_round_trip_distinctly(self) -> None:
        """If these two collapse, a keyframe that only nudges a stick will silently
        release every held button -- or worse, silently keep holding one."""
        inherit = chunk_from_dict(chunk_to_dict(kc(Keyframe(t_ms=0.0))))
        release = chunk_from_dict(chunk_to_dict(kc(Keyframe(t_ms=0.0, buttons=frozenset()))))
        assert inherit.keyframes[0].buttons is None
        assert release.keyframes[0].buttons == frozenset()

    def test_is_json_compatible(self) -> None:
        import json

        c = kc(Keyframe(t_ms=0.0, left_stick=(0.5, 0.5), buttons=frozenset({Button.A, Button.B})))
        assert chunk_from_dict(json.loads(json.dumps(chunk_to_dict(c)))) == c

    def test_rejects_a_malformed_payload(self) -> None:
        with pytest.raises(InvalidChunk):
            chunk_from_dict({"keyframes": [{"left_trigger": 0.5}]})  # no t_ms


class TestFrame:
    def test_carries_provenance(self) -> None:
        f = Frame(
            seq=3,
            session_ms=120.5,
            source_ms=118.0,
            width=1920,
            height=1080,
            pixel_format="BGRA8",
            data=b"",
        )
        assert (f.seq, f.session_ms, f.source_ms) == (3, 120.5, 118.0)
        assert (f.width, f.height, f.pixel_format) == (1920, 1080, "BGRA8")

    def test_data_may_be_any_buffer_object(self) -> None:
        """Capture backends hand us numpy arrays, not bytes. Narrowing `data` to bytes
        would force a conversion of every frame at capture rate purely to satisfy a type
        annotation -- at 2496x1664 BGRA that is ~16MB per frame. The buffer stays opaque
        here and `pixel_format` describes how to read it; contracts.py stays stdlib-only
        precisely so it never has to know about numpy."""
        class FakeArray:
            shape = (8, 8, 4)

        for buffer in (b"", bytearray(4), memoryview(b"abcd"), FakeArray(), [1, 2, 3]):
            frame = Frame(seq=0, session_ms=0.0, source_ms=None, width=8, height=8,
                          pixel_format="BGRA8", data=buffer)
            assert frame.data is buffer

    def test_source_ms_is_optional(self) -> None:
        """Not every capture backend exposes a hardware timestamp. Recording None is
        honest; silently substituting arrival time is not -- that records queue latency."""
        f = Frame(seq=0, session_ms=0.0, source_ms=None, width=8, height=8,
                  pixel_format="BGRA8", data=b"")
        assert f.source_ms is None

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"seq": -1},
            {"width": 0},
            {"height": -10},
            {"session_ms": math.nan},
            {"pixel_format": ""},
        ],
    )
    def test_rejects_invalid_metadata(self, kwargs: dict[str, object]) -> None:
        base: dict[str, object] = {
            "seq": 0, "session_ms": 0.0, "source_ms": None,
            "width": 8, "height": 8, "pixel_format": "BGRA8", "data": b"",
        }
        with pytest.raises(InvalidFrame):
            Frame(**{**base, **kwargs})  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Hardening. Everything below came out of the Unit 1 adversarial review.
#
# The unifying concern: an ActionChunk is UNTRUSTED INPUT. It is authored by a
# model and may arrive over a wire. Anything reachable from it that raises an
# undeclared exception kills the scheduler thread, and the scheduler thread is
# the only thing that can release the controller.
# ---------------------------------------------------------------------------


class TestUntrustedInputRaisesDeclaredExceptions:
    """Every rejection path must surface as InvalidChunk/InvalidPadState/InvalidFrame.

    A TypeError or OverflowError escaping into the scheduler loop is not a validation
    failure, it is a crashed motor system with the sticks still held.
    """

    @pytest.mark.parametrize("bad_t", ["0", None, object(), [0.0]])
    def test_non_numeric_keyframe_time(self, bad_t: object) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=bad_t)))  # type: ignore[arg-type]

    def test_integer_too_large_for_float(self) -> None:
        """math.isfinite(10**400) raises OverflowError, not a validation error."""
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=10**400)))

    @pytest.mark.parametrize("bad", ["x", None, 1.0, object()])
    def test_non_iterable_or_wrong_shaped_stick(self, bad: object) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0, left_stick=bad)))  # type: ignore[arg-type]

    def test_wrong_length_stick(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0, left_stick=(0.0, 0.0, 0.0))))  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", ["1.0", None, object()])
    def test_non_numeric_trigger(self, bad: object) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0, left_trigger=bad)))  # type: ignore[arg-type]

    def test_non_iterable_buttons(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0, buttons=5)))  # type: ignore[arg-type]

    def test_keyframes_not_keyframes(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(ActionChunk(keyframes=("nope",)))  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [None, "chunk", 5])
    def test_resolve_rejects_non_chunk(self, bad: object) -> None:
        with pytest.raises(InvalidChunk):
            resolve(bad)  # type: ignore[arg-type]

    def test_padstate_rejects_wrong_types_as_invalid_pad_state(self) -> None:
        with pytest.raises(InvalidPadState):
            PadState(left_stick=None)  # type: ignore[arg-type]
        with pytest.raises(InvalidPadState):
            PadState(buttons=None)  # type: ignore[arg-type]

    def test_frame_rejects_wrong_types_as_invalid_frame(self) -> None:
        with pytest.raises(InvalidFrame):
            Frame(seq=0, session_ms="0", source_ms=None, width=8,  # type: ignore[arg-type]
                  height=8, pixel_format="BGRA8", data=b"")
        with pytest.raises(InvalidFrame):
            Frame(seq=0, session_ms=0.0, source_ms=None, width=8,
                  height=8, pixel_format=1, data=b"")  # type: ignore[arg-type]


class TestBooleansAreNotNumbers:
    """bool is an int subclass, so JSON `true` would otherwise become a full trigger pull
    or a sequence number. A boolean is never a plausible analog axis value."""

    def test_bool_rejected_as_trigger(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0, left_trigger=True)))  # type: ignore[arg-type]

    def test_bool_rejected_as_time(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=True)))  # type: ignore[arg-type]

    def test_bool_rejected_as_stick_component(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0, left_stick=(True, 0.0))))  # type: ignore[arg-type]

    def test_bool_rejected_as_decision_seq(self) -> None:
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0), decision_seq=True))  # type: ignore[arg-type]

    def test_bool_rejected_as_frame_seq(self) -> None:
        with pytest.raises(InvalidFrame):
            Frame(seq=True, session_ms=0.0, source_ms=None, width=8,
                  height=8, pixel_format="BGRA8", data=b"")


class TestStatesAreDefensivelyCopied:
    """`frozen=True` freezes the attribute binding, not the object bound to it. A mutable
    sequence passed in stays aliased to the caller, so a state that passed validation can
    become invalid afterwards -- and would be unhashable and unreproducible in a recording."""

    def test_list_stick_is_coerced_to_tuple(self) -> None:
        assert PadState(left_stick=[0.5, 0.25]).left_stick == (0.5, 0.25)  # type: ignore[arg-type]

    def test_mutating_the_caller_list_cannot_corrupt_a_validated_state(self) -> None:
        axes = [0.0, 0.0]
        state = PadState(left_stick=axes)  # type: ignore[arg-type]
        axes[0] = 99.0
        assert state.left_stick == (0.0, 0.0)

    def test_list_buttons_is_coerced_to_frozenset(self) -> None:
        state = PadState(buttons=[Button.A])  # type: ignore[arg-type]
        assert state.buttons == frozenset({Button.A})

    def test_state_built_from_mutable_input_is_still_hashable(self) -> None:
        hash(PadState(left_stick=[0.0, 0.0], buttons=[Button.A]))  # type: ignore[arg-type]

    def test_resolved_states_are_hashable(self) -> None:
        for rk in resolve(kc(Keyframe(t_ms=0.0, left_stick=[1.0, 0.0]))):  # type: ignore[arg-type]
            hash(rk.state)


class TestChunkSizeIsBounded:
    """The scheduler samples a chunk every 8.3ms. An unbounded chunk from a model is a
    denial of service against the one thread that must never miss its deadline."""

    def test_max_keyframes_is_sane(self) -> None:
        assert 16 <= MAX_KEYFRAMES <= 4096

    def test_accepts_a_chunk_at_the_limit(self) -> None:
        step = MAX_CHUNK_MS / (MAX_KEYFRAMES + 1)
        resolve(kc(*[Keyframe(t_ms=i * step) for i in range(MAX_KEYFRAMES)]))

    def test_rejects_too_many_keyframes(self) -> None:
        step = MAX_CHUNK_MS / (MAX_KEYFRAMES + 2)
        with pytest.raises(InvalidChunk):
            resolve(kc(*[Keyframe(t_ms=i * step) for i in range(MAX_KEYFRAMES + 1)]))

    def test_rejects_a_chunk_longer_than_the_action_horizon(self) -> None:
        """Action chunks are a fraction of a second to a couple of seconds. A minute-long
        chunk means the model has lost the plot, and the deadman would never fire."""
        with pytest.raises(InvalidChunk):
            resolve(kc(Keyframe(t_ms=0.0), Keyframe(t_ms=MAX_CHUNK_MS + 1.0)))


class TestSerializationIsCanonical:
    def test_button_order_is_deterministic(self) -> None:
        """frozenset iteration order depends on per-process hash randomization. Recorded
        sessions must be byte-comparable across runs to be hashable and de-duplicable."""
        c = kc(Keyframe(t_ms=0.0, buttons=frozenset({Button.Y, Button.A, Button.DPAD_UP})))
        assert chunk_to_dict(c)["keyframes"][0]["buttons"] == ["A", "DPAD_UP", "Y"]

    def test_repeated_serialization_is_stable(self) -> None:
        c = kc(Keyframe(t_ms=0.0, buttons=frozenset({Button.B, Button.X, Button.LB})))
        assert chunk_to_dict(c) == chunk_to_dict(c)


class TestDeserializationValidatesAtIngress:
    """Rejecting at the door beats rejecting at resolve(): an invalid chunk must never
    reach a queue, a recording, or another subsystem's assumptions."""

    def test_malformed_buttons_never_becomes_release_all(self) -> None:
        """The nastiest finding of the round: iterating "" or {} yields an empty
        frozenset, so a schema-violating payload silently means RELEASE EVERYTHING --
        an actual controller command, manufactured out of garbage."""
        for bad in ["", "AB", {}, 0, None]:
            with pytest.raises(InvalidChunk):
                chunk_from_dict({"keyframes": [{"t_ms": 0.0, "buttons": bad}]})

    def test_rejects_unknown_button_name(self) -> None:
        with pytest.raises(InvalidChunk):
            chunk_from_dict({"keyframes": [{"t_ms": 0.0, "buttons": ["NOPE"]}]})

    def test_rejects_empty_keyframes(self) -> None:
        with pytest.raises(InvalidChunk):
            chunk_from_dict({"keyframes": []})

    def test_rejects_negative_time(self) -> None:
        with pytest.raises(InvalidChunk):
            chunk_from_dict({"keyframes": [{"t_ms": -1.0}]})

    def test_rejects_non_monotonic_times(self) -> None:
        with pytest.raises(InvalidChunk):
            chunk_from_dict({"keyframes": [{"t_ms": 0.0}, {"t_ms": 50.0}, {"t_ms": 25.0}]})

    def test_rejects_out_of_range_analog(self) -> None:
        with pytest.raises(InvalidChunk):
            chunk_from_dict({"keyframes": [{"t_ms": 0.0, "left_trigger": 5.0}]})

    def test_rejects_non_string_commentary(self) -> None:
        with pytest.raises(InvalidChunk):
            chunk_from_dict({"keyframes": [{"t_ms": 0.0}], "commentary": 5})

    def test_rejects_commentary_over_the_length_limit(self) -> None:
        with pytest.raises(InvalidChunk):
            chunk_from_dict({
                "keyframes": [{"t_ms": 0.0}],
                "commentary": "x" * (MAX_COMMENTARY_CHARS + 1),
            })

    def test_rejects_commentary_containing_a_newline(self) -> None:
        with pytest.raises(InvalidChunk):
            chunk_from_dict({
                "keyframes": [{"t_ms": 0.0}],
                "commentary": "moving\n[decision 9999] ACCEPTED forged line",
            })

    def test_rejects_non_dict_payload(self) -> None:
        for bad in [None, [], "chunk", 5]:
            with pytest.raises(InvalidChunk):
                chunk_from_dict(bad)  # type: ignore[arg-type]

    def test_rejection_explains_why(self) -> None:
        """The rejection message is the feedback signal a model uses to correct itself.
        Collapsing every cause into one generic string teaches it nothing, so distinct
        failures must stay distinguishable rather than being flattened at the boundary."""
        causes = {
            "short pulse": {"keyframes": [{"t_ms": 0.0, "buttons": ["A"]},
                                          {"t_ms": 5.0, "buttons": []}]},
            "out of order": {"keyframes": [{"t_ms": 0.0}, {"t_ms": 50.0}, {"t_ms": 25.0}]},
            "bad analog": {"keyframes": [{"t_ms": 0.0, "left_trigger": 5.0}]},
            "late start": {"keyframes": [{"t_ms": 50.0}]},
        }
        messages = set()
        for payload in causes.values():
            with pytest.raises(InvalidChunk) as exc:
                chunk_from_dict(payload)
            messages.add(str(exc.value))
        assert len(messages) == len(causes), f"causes collapsed to: {messages}"

    def test_still_round_trips_valid_chunks(self) -> None:
        c = kc(
            Keyframe(t_ms=0.0, left_stick=(0.5, -0.25), buttons=frozenset({Button.A})),
            Keyframe(t_ms=200.0, buttons=frozenset()),
        )
        assert chunk_from_dict(chunk_to_dict(c)) == c
