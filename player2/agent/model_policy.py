"""Provider-independent model policy built around an injectable transport seam."""

from __future__ import annotations

import math
from typing import Any, Protocol, runtime_checkable

from player2.agent.base import Observation
from player2.agent.model_schema import (
    ACTION_CHUNK_TOOL_NAME,
    ModelResponseError,
    action_chunk_tool_schema,
    observation_to_images,
    observation_to_prompt,
    parse_action_chunk,
)
from player2.contracts import ActionChunk

_DEFAULT_TIMEOUT_S = 10.0
_DEFAULT_MAX_IMAGE_DIM = 768
_DEFAULT_JPEG_QUALITY = 85

_DEFAULT_SYSTEM_PROMPT = """You are a visuomotor game-playing policy and planner.
Use only the supplied screenshots, goal, timing budget, and executed scheduler history.
For a tool request, author sparse action keyframes relative to t_ms=0 and keep the action short
enough for correction by the next observation. For a text request, return one concise goal for
the fast controller policy without JSON, controller keyframes, or commentary."""


class ModelTransportError(RuntimeError):
    """Report a provider, network, authentication, or subprocess transport failure."""


@runtime_checkable
class IModelTransport(Protocol):
    """Abstract provider-specific I/O while keeping policy decisions deterministic in tests."""

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
        """Return the arguments from one required structured tool call."""
        ...

    def complete_text(
        self,
        *,
        system: str,
        prompt: str,
        images: tuple[bytes, ...],
        timeout_s: float,
    ) -> str:
        """Return one provider-authored text completion."""
        ...


class SDKPolicy:
    """Use a model transport for both fast action proposals and slower goal formation."""

    def __init__(
        self,
        *,
        transport: IModelTransport,
        system_prompt: str = _DEFAULT_SYSTEM_PROMPT,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_image_dim: int = _DEFAULT_MAX_IMAGE_DIM,
        jpeg_quality: int = _DEFAULT_JPEG_QUALITY,
    ) -> None:
        """Bind a transport and validate the shared request and image configuration."""
        if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)):
            raise ValueError("timeout_s must be a positive finite number")
        try:
            timeout = float(timeout_s)
        except (OverflowError, TypeError, ValueError):
            raise ValueError("timeout_s must be a positive finite number") from None
        if not math.isfinite(timeout) or timeout <= 0.0:
            raise ValueError("timeout_s must be a positive finite number")
        if not isinstance(system_prompt, str):
            raise ValueError("system_prompt must be a string")
        if (isinstance(max_image_dim, bool) or not isinstance(max_image_dim, int)
                or max_image_dim <= 0):
            raise ValueError("max_image_dim must be a positive integer")
        if (isinstance(jpeg_quality, bool) or not isinstance(jpeg_quality, int)
                or not 1 <= jpeg_quality <= 100):
            raise ValueError("jpeg_quality must be an integer from 1 through 100")
        self._transport = transport
        self._timeout_s = timeout
        self._system_prompt = system_prompt
        self._max_image_dim = max_image_dim
        self._jpeg_quality = jpeg_quality

    def propose(self, observation: Observation) -> ActionChunk | None:
        """Request one structured action and validate it at the model-response boundary."""
        payload = self._transport.complete_tool(
            system=self._system_prompt,
            prompt=observation_to_prompt(observation),
            images=observation_to_images(
                observation,
                max_dim=self._max_image_dim,
                quality=self._jpeg_quality,
            ),
            tool_name=ACTION_CHUNK_TOOL_NAME,
            tool_schema=action_chunk_tool_schema(),
            timeout_s=self._timeout_s,
        )
        return parse_action_chunk(payload)

    def deliberate(self, observation: Observation) -> str | None:
        """Request one compact goal, treating provider whitespace as no guidance."""
        response = self._transport.complete_text(
            system=self._system_prompt,
            prompt=observation_to_prompt(observation),
            images=observation_to_images(
                observation,
                max_dim=self._max_image_dim,
                quality=self._jpeg_quality,
            ),
            timeout_s=self._timeout_s,
        )
        if not isinstance(response, str):
            raise ModelResponseError("deliberative model response must be text")
        goal = response.strip()
        return goal or None
