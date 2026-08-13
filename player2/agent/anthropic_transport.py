"""Anthropic SDK adapter for model-backed agent policies."""

from __future__ import annotations

import base64
import importlib
from types import ModuleType
from typing import Any, cast

from player2.agent.budget import TokenUsage
from player2.agent.model_policy import ModelTransportError

_DEFAULT_MODEL = "claude-sonnet-5"
_MAX_TOKENS = 2_048


def _optional_anthropic() -> ModuleType | None:
    """Load the optional SDK without making fake-client tests depend on it.

    Mirrors openai_transport.py's guarded import -- a base or CLI-only install must not pay
    an import-time ModuleNotFoundError for a provider it never uses, and injecting a fake
    client must be enough on its own to construct and exercise this transport.
    """
    try:
        return importlib.import_module("anthropic")
    except ImportError:
        return None


anthropic = _optional_anthropic()


def _user_content(prompt: str, images: tuple[bytes, ...]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for image in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.b64encode(image).decode("ascii"),
                },
            }
        )
    content.append({"type": "text", "text": prompt})
    return content


class AnthropicTransport:
    """Keep Anthropic's SDK-specific wire format behind the policy transport seam."""

    def __init__(self, *, client: Any | None = None, model: str = _DEFAULT_MODEL) -> None:
        """Accept a fake client so unit tests never need credentials, network, or the SDK.

        The default client disables the SDK's own retry loop (``max_retries=0``): the fast
        policy thread has one ``timeout_s`` budget for a whole decision, and a transient-error
        retry that silently reruns the request would let three attempts each cost close to
        that budget, multiplying the worst-case stall well past what the caller configured.
        """
        if client is None:
            if anthropic is None:
                raise ImportError(
                    "AnthropicTransport requires the optional 'anthropic' package; "
                    "install player2[models] or inject a client"
                )
            client = anthropic.Anthropic(max_retries=0)
        self._client: Any = client
        self._model = model
        self._last_usage: TokenUsage | None = None

    @property
    def last_usage(self) -> TokenUsage | None:
        """Return usage from the most recent successful, parseable completion."""
        return self._last_usage

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
        """Force one tool call because the fast policy has no spare retry turn for prose."""
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": _user_content(prompt, images)}],
                tools=[{"name": tool_name, "input_schema": tool_schema}],
                tool_choice={"type": "tool", "name": tool_name},
                timeout=timeout_s,
            )
        except Exception as exc:
            raise ModelTransportError(f"Anthropic request failed: {exc}") from exc

        try:
            for block in response.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                if getattr(block, "name", None) != tool_name:
                    continue
                tool_input = getattr(block, "input", None)
                if not isinstance(tool_input, dict):
                    raise ModelTransportError("Anthropic returned a non-object tool input")
                self._set_last_usage(response)
                return cast(dict[str, Any], tool_input)
        except ModelTransportError:
            raise
        except (AttributeError, TypeError) as exc:
            raise ModelTransportError("Anthropic returned malformed message content") from exc
        raise ModelTransportError(f"Anthropic did not call the required tool {tool_name!r}")

    def complete_text(
        self,
        *,
        system: str,
        prompt: str,
        images: tuple[bytes, ...],
        timeout_s: float,
    ) -> str:
        """Join only text blocks so incidental provider content cannot leak into a goal."""
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=_MAX_TOKENS,
                system=system,
                messages=[{"role": "user", "content": _user_content(prompt, images)}],
                timeout=timeout_s,
            )
        except Exception as exc:
            raise ModelTransportError(f"Anthropic request failed: {exc}") from exc

        parts: list[str] = []
        try:
            for block in response.content:
                if getattr(block, "type", None) != "text":
                    continue
                text = getattr(block, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        except (AttributeError, TypeError) as exc:
            raise ModelTransportError("Anthropic returned malformed message content") from exc
        self._set_last_usage(response)
        return "".join(parts)

    def _set_last_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            self._last_usage = None
            return
        input_tokens = getattr(usage, "input_tokens", None)
        output_tokens = getattr(usage, "output_tokens", None)
        if input_tokens is None or output_tokens is None:
            self._last_usage = None
            return
        self._last_usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
