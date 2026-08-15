"""Translate between runtime observations and provider-neutral model wire data."""

from __future__ import annotations

from typing import Any

from player2.agent.base import Observation
from player2.contracts import (
    MAX_CHUNK_MS,
    MAX_COMMENTARY_CHARS,
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
# Confirmed live (docs/DEV-PROCESS.md): unbounded history text grows ~20 tokens per decision
# and eventually exceeds this project's 4096-token context ceiling on its own, corrupting
# every subsequent tool-call response mid-generation. 30 events costs roughly 400-600 tokens
# at the observed ~15-20 tokens/event, leaving comfortable margin alongside a ~1700-token
# image and ~700-900 tokens of agent_notes/goal. Tied to today's known ceiling, not a law of
# nature -- raise it freely on hardware with more context room.
_DEFAULT_HISTORY_WINDOW = 30


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


def action_chunk_tool_schema(*, require_commentary: bool = False) -> dict[str, Any]:
    """Describe sparse action authoring while leaving final validation to contracts.py.

    `require_commentary` exists because "optional" turned out not to mean what it sounds like
    for local, grammar-constrained tool-calling: confirmed live (docs/DEV-PROCESS.md, Unit 14)
    that a model satisfies `required` and stops, so an optional field can go unused on every
    single decision regardless of what the system prompt asks for. Runtime validation of
    `commentary` in contracts.py is unaffected either way -- it stays optional there
    (`None` is always a legal value) since this flag is purely a prompting lever, not a
    controller-safety one.
    """
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
            },
            "commentary": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_COMMENTARY_CHARS,
                "description": (
                    "One short, plain-English line on what you're doing right now and why -- "
                    "shown live to the human watching this session, never interpreted by the "
                    "game or the runtime. Say it like you're narrating your own play, not "
                    "documenting a function. A single line: no newlines or other control "
                    "characters."
                ),
            },
        },
        "required": ["keyframes", "commentary"] if require_commentary else ["keyframes"],
        "additionalProperties": False,
    }


def observation_to_prompt(
    observation: Observation, *, history_window: int = _DEFAULT_HISTORY_WINDOW,
) -> str:
    """Render goal, timing, frame provenance, and executed scheduler evidence as text."""
    if (isinstance(history_window, bool) or not isinstance(history_window, int)
            or history_window <= 0):
        raise ValueError("history_window must be a positive integer")
    goal = observation.goal if observation.goal is not None else "No goal is currently set."
    remaining_ms = observation.deadline_ms - observation.observation_cutoff_ms
    lines = [
        f"Current goal: {goal}",
        f"Observation contains {len(observation.frames)} recent frame(s).",
        f"Observation cutoff: {observation.observation_cutoff_ms:g} ms.",
        f"Remaining decision budget from that cutoff: {remaining_ms:g} ms.",
        f"Controller epoch: {observation.epoch}; decision sequence: {observation.decision_seq}.",
    ]
    # Only worth the extra tokens once there is more than one frame to relate to another --
    # at the current default (one frame per decision) this adds nothing to the prompt, so
    # raising frames_per_observation is the only thing that turns this section on.
    # IVideoSource.latest()'s documented contract guarantees ascending session_ms (this
    # project's only real implementation, WGC, rejects backwards session times outright), and
    # observation_to_images() preserves that same tuple order into the images actually sent, so
    # frame N in this list is image N in the request, oldest first -- unlabeled, a multi-frame
    # observation is just N stills with no way to judge motion between them. This function
    # trusts that ordering rather than re-deriving it; it is not re-validated here because
    # AgentLoop._decide() is the only real constructor of an Observation in this codebase, and
    # it derives observation_cutoff_ms from the same max() that ordering guarantees is the
    # tuple's last element.
    if len(observation.frames) > 1:
        lines.append(
            "Frames are supplied oldest to newest, in this order, each labeled with its "
            "capture time relative to the observation cutoff above (0 ms = the newest frame) "
            "so motion between them can be judged accurately:"
        )
        lines.extend(
            # round() to a plain int, not float :g formatting -- int 0 never prints "-0" the
            # way f"{-0.3:.0f}" would for a frame captured a fraction of a millisecond early,
            # and sub-millisecond precision has no motor-timing meaning here anyway.
            f"  frame {index + 1}: "
            f"{round(frame.session_ms - observation.observation_cutoff_ms)} ms"
            for index, frame in enumerate(observation.frames)
        )
    lines.append("Recent scheduler execution history (oldest to newest):")
    history = observation.history[-history_window:]
    omitted = len(observation.history) - len(history)
    if history:
        if omitted > 0:
            lines.append(
                f"  ({omitted} older event(s) omitted to fit the model's context budget)"
            )
        lines.extend(
            f"- {event.session_ms:g} ms: {event.kind.value}: {event.detail}"
            for event in history
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
