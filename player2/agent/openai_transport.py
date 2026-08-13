"""OpenAI SDK adapter for model-backed agent policies."""

from __future__ import annotations

import base64
import importlib
import json
from types import ModuleType
from typing import Any, cast

from player2.agent.budget import TokenUsage
from player2.agent.model_policy import ModelTransportError

_DEFAULT_MODEL = "gpt-4.1-mini"
_MAX_TOKENS = 2_048


def _optional_openai() -> ModuleType | None:
    """Load the optional SDK without making fake-client tests depend on it."""
    try:
        return importlib.import_module("openai")
    except ImportError:
        return None


openai = _optional_openai()


def _user_content(prompt: str, images: tuple[bytes, ...]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for image in images:
        encoded = base64.b64encode(image).decode("ascii")
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
            }
        )
    content.append({"type": "text", "text": prompt})
    return content


def _messages(system: str, prompt: str, images: tuple[bytes, ...]) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": _user_content(prompt, images)},
    ]


class OpenAITransport:
    """Keep OpenAI's SDK-specific wire format behind the policy transport seam."""

    def __init__(self, *, client: Any | None = None, model: str = _DEFAULT_MODEL) -> None:
        """Permit fake injection while giving missing optional installs an actionable error.

        The default client disables the SDK's own retry loop (``max_retries=0``) for the same
        reason anthropic_transport.py does: the fast policy thread has one ``timeout_s``
        budget for a whole decision, and a silent retry on a transient error can multiply the
        worst-case stall to several times what the caller configured.
        """
        if client is None:
            if openai is None:
                raise ImportError(
                    "OpenAITransport requires the optional 'openai' package; "
                    "install player2[models] or inject a client"
                )
            client = openai.OpenAI(max_retries=0)
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
        """Force one function call because the fast policy cannot retry a prose response."""
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=_messages(system, prompt, images),
                tools=[
                    {
                        "type": "function",
                        "function": {"name": tool_name, "parameters": tool_schema},
                    }
                ],
                tool_choice={"type": "function", "function": {"name": tool_name}},
                max_completion_tokens=_MAX_TOKENS,
                timeout=timeout_s,
            )
        except Exception as exc:
            raise ModelTransportError(f"OpenAI request failed: {exc}") from exc

        try:
            calls = response.choices[0].message.tool_calls
            if not calls:
                raise ModelTransportError(f"OpenAI did not call the required tool {tool_name!r}")
            call = calls[0]
            called_name = call.function.name
            arguments = call.function.arguments
        except ModelTransportError:
            raise
        except (AttributeError, IndexError, TypeError) as exc:
            raise ModelTransportError("OpenAI returned malformed completion content") from exc

        if called_name != tool_name:
            raise ModelTransportError(
                f"OpenAI called {called_name!r}, not the required tool {tool_name!r}"
            )
        try:
            payload = json.loads(arguments)
        except (json.JSONDecodeError, TypeError, UnicodeDecodeError) as exc:
            raise ModelTransportError("OpenAI returned malformed tool arguments") from exc
        if not isinstance(payload, dict):
            raise ModelTransportError("OpenAI returned non-object tool arguments")
        self._set_last_usage(response)
        return cast(dict[str, Any], payload)

    def complete_text(
        self,
        *,
        system: str,
        prompt: str,
        images: tuple[bytes, ...],
        timeout_s: float,
    ) -> str:
        """Normalize absent text to an empty string so the policy boundary stays typed."""
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=_messages(system, prompt, images),
                max_completion_tokens=_MAX_TOKENS,
                timeout=timeout_s,
            )
        except Exception as exc:
            raise ModelTransportError(f"OpenAI request failed: {exc}") from exc

        try:
            content = response.choices[0].message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise ModelTransportError("OpenAI returned no completion choice") from exc
        if content is None:
            self._set_last_usage(response)
            return ""
        if not isinstance(content, str):
            raise ModelTransportError("OpenAI returned non-text completion content")
        self._set_last_usage(response)
        return content

    def _set_last_usage(self, response: Any) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            self._last_usage = None
            return
        input_tokens = getattr(usage, "prompt_tokens", None)
        output_tokens = getattr(usage, "completion_tokens", None)
        if input_tokens is None or output_tokens is None:
            self._last_usage = None
            return
        self._last_usage = TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens)
