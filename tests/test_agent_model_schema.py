"""Contract for player2.agent.model_schema -- the wire format between Observation and a model.

This module is deliberately the only place that turns an Observation into request content
and turns a reply back into an ActionChunk. It owns no network code and no subprocess code,
so every case here is tested with plain data -- no fake transport, no API key, no process.

The schema must not invent a second definition of "legal action chunk". `chunk_from_dict` in
contracts.py already validates the sparse keyframe shape; parse_action_chunk delegates to it
so the wire format and the runtime's own validation cannot drift apart.
"""

from __future__ import annotations

import base64

import numpy as np
import pytest

from player2.agent.base import Observation
from player2.agent.model_schema import (
    ACTION_CHUNK_TOOL_NAME,
    ModelResponseError,
    action_chunk_tool_schema,
    observation_to_images,
    observation_to_prompt,
    parse_action_chunk,
)
from player2.contracts import MAX_COMMENTARY_CHARS, Button, Frame
from player2.control.scheduler import EventKind, SchedulerEvent


def solid_frame(seq: int, session_ms: float, width: int = 8, height: int = 8) -> Frame:
    buffer = np.zeros((height, width, 4), dtype=np.uint8)
    buffer[:, :] = (10, 20, 30, 255)
    buffer.flags.writeable = False
    return Frame(seq=seq, session_ms=session_ms, source_ms=None, width=width, height=height,
                 pixel_format="BGRA8", data=buffer)


def observation(
    *,
    frames: tuple[Frame, ...] = (),
    goal: str | None = "test goal",
    history: tuple[SchedulerEvent, ...] = (),
    cutoff: float = 100.0,
    deadline: float = 200.0,
) -> Observation:
    return Observation(frames=frames, goal=goal, history=history,
                       observation_cutoff_ms=cutoff, deadline_ms=deadline,
                       epoch=1, decision_seq=3)


class TestActionChunkToolSchema:
    def test_is_a_json_schema_object_requiring_keyframes(self) -> None:
        schema = action_chunk_tool_schema()
        assert schema["type"] == "object"
        assert schema["required"] == ["keyframes"]
        assert "keyframes" in schema["properties"]

    def test_keyframe_items_require_only_t_ms(self) -> None:
        """Every other channel is sparse-optional: chunk_from_dict treats an absent key as
        inherit, so the schema must not force the model to restate the whole pad every frame."""
        schema = action_chunk_tool_schema()
        keyframe_schema = schema["properties"]["keyframes"]["items"]
        assert keyframe_schema["required"] == ["t_ms"]

    def test_buttons_enum_matches_the_real_button_set(self) -> None:
        schema = action_chunk_tool_schema()
        keyframe_schema = schema["properties"]["keyframes"]["items"]
        buttons_enum = set(keyframe_schema["properties"]["buttons"]["items"]["enum"])
        assert buttons_enum == {button.value for button in Button}

    def test_commentary_is_optional_by_default(self) -> None:
        """Default: a scripted or terse model must not be forced to narrate. Confirmed live
        (docs/DEV-PROCESS.md, Unit 14) that this is not merely permissive -- against a local
        grammar-constrained model, "optional" means the field is realistically never used,
        which is exactly why `require_commentary` exists as an opt-in override below."""
        schema = action_chunk_tool_schema()
        assert "commentary" not in schema["required"]

    def test_commentary_schema_is_bounded_regardless_of_required_state(self) -> None:
        """Bounded to the same limit contracts.py enforces so a schema-obeying model can
        never get rejected at the runtime boundary purely for length."""
        schemas = (action_chunk_tool_schema(), action_chunk_tool_schema(require_commentary=True))
        for schema in schemas:
            commentary_schema = schema["properties"]["commentary"]
            assert commentary_schema["type"] == "string"
            assert commentary_schema["maxLength"] == MAX_COMMENTARY_CHARS
            # Guidance only, not an enforcement boundary a provider might ignore: an empty
            # string that gets through anyway is not rejected (it carries no controller-safety
            # weight, so there is no reason to throw away an otherwise-valid movement chunk
            # over it) -- contracts.py and demo.py's formatter just treat it the same as "no
            # commentary given" wherever it would otherwise show up.
            assert commentary_schema["minLength"] == 1

    def test_require_commentary_true_adds_it_to_the_required_list(self) -> None:
        schema = action_chunk_tool_schema(require_commentary=True)
        assert set(schema["required"]) == {"keyframes", "commentary"}

    def test_require_commentary_does_not_change_keyframes_requirement(self) -> None:
        schema = action_chunk_tool_schema(require_commentary=True)
        assert "keyframes" in schema["required"]

    def test_is_stable_json_serialisable_shape(self) -> None:
        """No tuples, no sets, no enums leaking into the schema -- some SDKs json.dumps this
        directly and a non-JSON-native type would fail deep inside a transport instead of here."""
        import json
        json.dumps(action_chunk_tool_schema())


class TestObservationToPrompt:
    def test_includes_the_goal_when_present(self) -> None:
        text = observation_to_prompt(observation(goal="walk an expanding spiral"))
        assert "walk an expanding spiral" in text

    def test_says_something_sane_when_goal_is_absent(self) -> None:
        """goal=None is a legitimate state (see test_agent.py) and must not crash prompting
        or silently render the literal string 'None' into the model's instructions."""
        text = observation_to_prompt(observation(goal=None))
        assert "None" not in text

    def test_includes_recent_executed_history_not_just_proposals(self) -> None:
        """The model must reason from what the pad actually did, not from its own intentions
        -- see the seam docstring in test_agent.py. A prompt with no execution evidence would
        make that distinction invisible to the model even though the runtime preserves it."""
        events = (
            SchedulerEvent(kind=EventKind.PREEMPTED, session_ms=50.0, detail="chunk seq=2"),
            SchedulerEvent(kind=EventKind.DEADMAN, session_ms=90.0, detail="released"),
        )
        text = observation_to_prompt(observation(history=events))
        assert "preempted" in text.lower()
        assert "deadman" in text.lower()

    def test_communicates_the_remaining_time_budget(self) -> None:
        """deadline_ms - observation_cutoff_ms is how long this decision has before it is
        stale. A model with no notion of that budget cannot reason about chunk duration."""
        text = observation_to_prompt(observation(cutoff=100.0, deadline=350.0))
        assert "250" in text

    def test_never_raises_on_an_empty_observation(self) -> None:
        observation_to_prompt(observation(goal=None, history=(), frames=()))

    def test_history_defaults_to_a_bounded_recent_window(self) -> None:
        """Confirmed live (docs/DEV-PROCESS.md): unbounded history text grows ~20 tokens per
        decision and eventually exceeds the model's 4096-token context entirely on its own,
        which corrupts every subsequent tool-call response mid-generation rather than failing
        cleanly. Only the most recent events belong in the prompt -- older ones are dropped,
        not the newest, since "did my last command land" matters far more than history from
        many decisions ago."""
        events = tuple(
            SchedulerEvent(kind=EventKind.PREEMPTED, session_ms=float(i), detail=f"chunk seq={i}")
            for i in range(50)
        )
        text = observation_to_prompt(observation(history=events))
        assert "seq=49" in text  # newest event survives
        assert "seq=0" not in text  # oldest event was dropped

    def test_history_window_is_configurable(self) -> None:
        events = tuple(
            SchedulerEvent(kind=EventKind.PREEMPTED, session_ms=float(i), detail=f"chunk seq={i}")
            for i in range(10)
        )
        text = observation_to_prompt(observation(history=events), history_window=3)
        assert "seq=9" in text
        assert "seq=8" in text
        assert "seq=7" in text
        assert "seq=6" not in text

    def test_history_shorter_than_the_window_is_shown_in_full_unmarked(self) -> None:
        """No spurious "omitted" note when nothing was actually dropped -- a caller reading
        the prompt should not have to wonder whether 0 is a real omission count."""
        events = (SchedulerEvent(kind=EventKind.DEADMAN, session_ms=1.0, detail="released"),)
        text = observation_to_prompt(observation(history=events), history_window=30)
        assert "omitted" not in text.lower()
        assert "released" in text

    def test_history_window_must_be_a_positive_integer(self) -> None:
        for bad in (0, -1, 1.5, True):
            with pytest.raises(ValueError):
                observation_to_prompt(observation(), history_window=bad)  # type: ignore[arg-type]

    def test_labels_each_frame_with_its_time_relative_to_the_cutoff_when_there_are_several(
        self,
    ) -> None:
        """`IVideoSource.latest()` guarantees chronological order (oldest to newest), and
        `observation_to_images()` preserves that order into the images sent to the model --
        this must give the model a relative timestamp per frame in the SAME order, or a
        multi-frame observation is just N unlabeled stills with no way to judge motion between
        them, which is the whole point of ever sending more than one. Checked as exact lines,
        not loose substrings -- a substring match would still pass if a label were attached to
        the wrong frame."""
        frames = (
            solid_frame(0, 100.0), solid_frame(1, 350.0), solid_frame(2, 620.0),
        )
        text = observation_to_prompt(observation(frames=frames, cutoff=620.0))
        lines = text.splitlines()
        assert "  frame 1: -520 ms" in lines
        assert "  frame 2: -270 ms" in lines
        assert "  frame 3: 0 ms" in lines

    def test_a_frame_fractionally_before_the_cutoff_never_renders_as_negative_zero(self) -> None:
        """A frame captured less than 0.5ms before the cutoff rounds to 0ms -- Python's own
        float formatting (`f"{-0.3:.0f}"` == "-0") would render that as the confusing,
        ambiguous "-0 ms" if relative offsets were formatted as floats directly."""
        frames = (solid_frame(0, 619.7), solid_frame(1, 620.0))
        text = observation_to_prompt(observation(frames=frames, cutoff=620.0))
        assert "-0" not in text

    def test_single_frame_observation_gets_no_per_frame_breakdown(self) -> None:
        """The current default is exactly one frame per decision -- this labeling must add
        nothing (no extra tokens, no behavior change) to that baseline. Only worth doing once
        frames_per_observation is deliberately raised above 1."""
        text = observation_to_prompt(observation(frames=(solid_frame(0, 100.0),), cutoff=100.0))
        assert "relative to" not in text.lower()


class TestObservationToImages:
    def test_returns_one_encoded_image_per_frame(self) -> None:
        frames = (solid_frame(0, 0.0), solid_frame(1, 33.0))
        images = observation_to_images(observation(frames=frames))
        assert len(images) == 2

    def test_returned_bytes_are_valid_jpeg(self) -> None:
        images = observation_to_images(observation(frames=(solid_frame(0, 0.0),)))
        # JPEG's magic number; this is the difference between "an image" and "raw garbage
        # a provider will reject with an opaque 400" (Unit 4's channel-order lesson applies
        # here too -- a wrong-but-plausible-looking encode is worse than a crash).
        assert images[0][:2] == b"\xff\xd8"

    def test_empty_frames_yields_empty_images(self) -> None:
        assert observation_to_images(observation(frames=())) == ()

    def test_downscales_large_frames(self) -> None:
        """A full-resolution frame handed to a model wastes tokens for nothing a policy needs
        -- CARRYOVER's own transport numbers were measured on downscaled input."""
        big = solid_frame(0, 0.0, width=2000, height=1000)
        small_bytes = observation_to_images(observation(frames=(big,)), max_dim=256)
        full_bytes = observation_to_images(observation(frames=(big,)), max_dim=4000)
        assert len(small_bytes[0]) < len(full_bytes[0])


class TestParseActionChunk:
    def test_accepts_a_well_formed_payload(self) -> None:
        payload = {"keyframes": [
            {"t_ms": 0.0, "left_stick": [1.0, 0.0]},
            {"t_ms": 300.0},
        ]}
        chunk = parse_action_chunk(payload)
        assert chunk.keyframes[0].left_stick == (1.0, 0.0)

    def test_carries_commentary_through_when_present(self) -> None:
        payload = {"keyframes": [{"t_ms": 0.0}], "commentary": "circling back for ammo"}
        assert parse_action_chunk(payload).commentary == "circling back for ammo"

    def test_commentary_defaults_to_none_when_absent(self) -> None:
        assert parse_action_chunk({"keyframes": [{"t_ms": 0.0}]}).commentary is None

    def test_wraps_an_invalid_chunk_as_a_model_response_error(self) -> None:
        """chunk_from_dict raises InvalidChunk for the runtime's own decode path (recorded
        sessions, MCP). A model reply is untrusted input from a different boundary and must
        surface as this module's own error type, not leak an internal contracts exception."""
        with pytest.raises(ModelResponseError):
            parse_action_chunk({"keyframes": []})

    def test_malformed_button_value_is_a_model_response_error_not_a_crash(self) -> None:
        payload = {"keyframes": [{"t_ms": 0.0, "buttons": ["NOT_A_BUTTON"]}]}
        with pytest.raises(ModelResponseError):
            parse_action_chunk(payload)

    def test_non_dict_payload_is_a_model_response_error(self) -> None:
        with pytest.raises(ModelResponseError):
            parse_action_chunk("not even a dict")

    def test_error_message_carries_the_specific_reason(self) -> None:
        """chunk_from_dict already distinguishes causes (contracts.py's own comment: a model
        reads the rejection reason to correct its next chunk). Collapsing it here would
        repeat a mistake DEV-PROCESS.md already recorded and fixed once in Unit 1."""
        with pytest.raises(ModelResponseError, match="keyframe"):
            parse_action_chunk({"keyframes": []})


class TestToolName:
    def test_is_a_non_empty_stable_identifier(self) -> None:
        assert isinstance(ACTION_CHUNK_TOOL_NAME, str)
        assert ACTION_CHUNK_TOOL_NAME


def test_image_bytes_round_trip_through_base64_cleanly() -> None:
    """Every transport must base64-encode these bytes for a JSON request body. Confirming
    that round-trips here means a transport failure can never be blamed on this module."""
    images = observation_to_images(observation(frames=(solid_frame(0, 0.0),)))
    encoded = base64.b64encode(images[0])
    assert base64.b64decode(encoded) == images[0]
