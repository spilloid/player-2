"""Translate between runtime observations and provider-neutral model wire data."""

from __future__ import annotations

from typing import Any

from player2.agent.base import Observation
from player2.contracts import (
    MAX_CHUNK_MS,
    MAX_KEYFRAMES,
    ActionChunk,
    Button,
    InvalidChunk,
    chunk_from_dict,
)
from player2.video.encode import downscale, encode_jpeg

ACTION_CHUNK_TOOL_NAME = "submit_action_chunk"

_DEFAULT_IMAGE_MAX_DIM = 768
_DEFAULT_JPEG_QUALITY = 85


class ModelResponseError(RuntimeError):
    """Report model-authored data that cannot satisfy the runtime contract."""


def _stick_schema() -> dict[str, Any]:
    """Return a fresh JSON-native schema for one normalized analog stick."""
    return {
        "type": "array",
        "items": {"type": "number", "minimum": -1.0, "maximum": 1.0},
        "minItems": 2,
        "maxItems": 2,
    }


def action_chunk_tool_schema() -> dict[str, Any]:
    """Describe sparse action authoring while leaving final validation to contracts.py."""
    keyframe_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "t_ms": {
                "type": "number",
                "minimum": 0.0,
                "maximum": MAX_CHUNK_MS,
                "description": "Milliseconds from the start of this action chunk.",
            },
            "left_stick": _stick_schema(),
            "right_stick": _stick_schema(),
            "left_trigger": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "right_trigger": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "buttons": {
                "type": "array",
                "items": {"type": "string", "enum": [button.value for button in Button]},
                "uniqueItems": True,
                "maxItems": len(Button),
                "description": "Complete set of buttons held from this keyframe onward.",
            },
        },
        "required": ["t_ms"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "keyframes": {
                "type": "array",
                "items": keyframe_schema,
                "minItems": 1,
                "maxItems": MAX_KEYFRAMES,
                "description": (
                    "Sparse controller states in strictly increasing time order; the first "
                    "keyframe must have t_ms=0. Omitted channels inherit their previous value."
                ),
            }
        },
        "required": ["keyframes"],
        "additionalProperties": False,
    }


def observation_to_prompt(observation: Observation) -> str:
    """Render goal, timing, frame provenance, and executed scheduler evidence as text."""
    goal = observation.goal if observation.goal is not None else "No goal is currently set."
    remaining_ms = observation.deadline_ms - observation.observation_cutoff_ms
    lines = [
        f"Current goal: {goal}",
        f"Observation contains {len(observation.frames)} recent frame(s).",
        f"Observation cutoff: {observation.observation_cutoff_ms:g} ms.",
        f"Remaining decision budget from that cutoff: {remaining_ms:g} ms.",
        f"Controller epoch: {observation.epoch}; decision sequence: {observation.decision_seq}.",
        "Recent scheduler execution history (oldest to newest):",
    ]
    if observation.history:
        lines.extend(
            f"- {event.session_ms:g} ms: {event.kind.value}: {event.detail}"
            for event in observation.history
        )
    else:
        lines.append("- No recent scheduler events.")
    return "\n".join(lines)


def observation_to_images(
    observation: Observation,
    *,
    max_dim: int = _DEFAULT_IMAGE_MAX_DIM,
    quality: int = _DEFAULT_JPEG_QUALITY,
) -> tuple[bytes, ...]:
    """Encode each observation frame as a bounded JPEG in the original evidence order."""
    if isinstance(max_dim, bool) or not isinstance(max_dim, int) or max_dim <= 0:
        raise ValueError("max_dim must be a positive integer")
    if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
        raise ValueError("quality must be an integer from 1 through 100")
    return tuple(
        encode_jpeg(downscale(frame, max_dim), quality=quality)
        for frame in observation.frames
    )


def parse_action_chunk(payload: object) -> ActionChunk:
    """Decode model output through the runtime validator and translate its boundary error."""
    try:
        return chunk_from_dict(payload)
    except InvalidChunk as error:
        raise ModelResponseError(str(error)) from error
