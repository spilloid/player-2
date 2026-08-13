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
from player2.contracts import Button, Frame
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
