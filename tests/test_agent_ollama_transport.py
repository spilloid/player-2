"""Contract for player2.agent.ollama_transport.OllamaTransport -- a local, no-credential path.

Ollama exposes an OpenAI-compatible endpoint, so this is deliberately a thin subclass of
OpenAITransport rather than a fourth independent implementation: identical request building,
identical response parsing, identical retry-disabling and token-cap discipline, identical
last_usage reporting -- all inherited, none re-tested here. What this file actually specs is
the part that IS different: construction defaults (a local base_url, a placeholder credential
Ollama does not check, a genuinely small default model) and that OllamaTransport is a real,
distinct, discoverable class rather than a documented trick callers have to know themselves.

UNVERIFIED, and said so in the module docstring: whether the default small model actually
honors forced tool_choice the way this code assumes. That needs a live smoke run before
propose() is trusted against it, same as every other transport here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from player2.agent.model_policy import ModelTransportError
from player2.agent.ollama_transport import OllamaTransport
from player2.agent.openai_transport import OpenAITransport


@dataclass
class _FunctionCall:
    name: str
    arguments: str


@dataclass
class _ToolCall:
    function: _FunctionCall
    id: str = "call_1"
    type: str = "function"


@dataclass
class _Message:
    content: str | None = None
    tool_calls: list[_ToolCall] | None = None


@dataclass
class _Choice:
    message: _Message


@dataclass
class _ChatCompletion:
    choices: list[_Choice]
    usage: Any = None


class _FakeCompletions:
    def __init__(self, *, result: _ChatCompletion | None = None,
                 error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _ChatCompletion:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


@dataclass
class _FakeChat:
    completions: _FakeCompletions


@dataclass
class _FakeClient:
    chat: _FakeChat


def make_client(*, result: _ChatCompletion | None = None) -> _FakeClient:
    return _FakeClient(chat=_FakeChat(completions=_FakeCompletions(result=result)))


def tool_call_response(name: str, arguments: dict[str, Any]) -> _ChatCompletion:
    call = _ToolCall(function=_FunctionCall(name=name, arguments=json.dumps(arguments)))
    return _ChatCompletion(choices=[_Choice(message=_Message(tool_calls=[call]))])


class TestIsAnOpenAICompatibleTransport:
    def test_is_a_subclass_of_openai_transport(self) -> None:
        """Ollama's OpenAI-compatible endpoint means there is exactly one place that builds
        chat-completions requests and parses their replies; a second parallel implementation
        would only be a place for the two to quietly drift apart."""
        assert issubclass(OllamaTransport, OpenAITransport)

    def test_delegates_complete_tool_to_the_inherited_implementation(self) -> None:
        client = make_client(result=tool_call_response("t", {"keyframes": []}))
        transport = OllamaTransport(client=client)
        result = transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                         tool_schema={"type": "object"}, timeout_s=5.0)
        assert result == {"keyframes": []}

    def test_delegates_complete_text_to_the_inherited_implementation(self) -> None:
        client = make_client(result=_ChatCompletion(
            choices=[_Choice(message=_Message(content="explore the map"))]
        ))
        transport = OllamaTransport(client=client)
        result = transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)
        assert result == "explore the map"

    def test_provider_errors_still_become_model_transport_error(self) -> None:
        client = make_client()
        client.chat.completions._error = RuntimeError("connection refused")
        transport = OllamaTransport(client=client)
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                    tool_schema={"type": "object"}, timeout_s=5.0)


class TestConstructionDefaults:
    def test_default_model_is_distinct_from_openai_transports_default(self) -> None:
        """The whole premise of this unit: try something genuinely small locally and accept
        it will be slower. Silently sharing OpenAITransport's cloud-sized default model would
        defeat that, so each default is pinned by what it actually sends on the wire."""
        ollama_client = make_client(result=tool_call_response("t", {"keyframes": []}))
        OllamaTransport(client=ollama_client).complete_tool(
            system="sys", prompt="go", images=(), tool_name="t",
            tool_schema={"type": "object"}, timeout_s=5.0,
        )
        openai_client = make_client(result=tool_call_response("t", {"keyframes": []}))
        OpenAITransport(client=openai_client).complete_tool(
            system="sys", prompt="go", images=(), tool_name="t",
            tool_schema={"type": "object"}, timeout_s=5.0,
        )
        ollama_model = ollama_client.chat.completions.calls[0]["model"]
        openai_model = openai_client.chat.completions.calls[0]["model"]
        assert ollama_model != openai_model

    def test_accepts_a_model_override(self) -> None:
        client = make_client(result=tool_call_response("t", {"keyframes": []}))
        transport = OllamaTransport(client=client, model="llava")
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        assert client.chat.completions.calls[0]["model"] == "llava"

    def test_uses_an_injected_client_without_requiring_the_openai_package(self) -> None:
        client = make_client(result=tool_call_response("t", {"keyframes": []}))
        OllamaTransport(client=client)

    def test_default_construction_points_at_a_local_server_not_the_cloud(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Regardless of whether the real openai package is installed, constructing without
        an injected client must never reach out to OpenAI's cloud API -- that would silently
        bill the wrong provider for what is supposed to be a free local experiment."""
        import player2.agent.ollama_transport as module

        calls: list[dict[str, Any]] = []

        class _FakeOpenAIModule:
            class OpenAI:
                def __init__(self, **kwargs: Any) -> None:
                    calls.append(kwargs)

        monkeypatch.setattr(module, "openai", _FakeOpenAIModule())
        module.OllamaTransport()
        assert calls, "OllamaTransport() did not construct a client at all"
        assert "localhost" in calls[0]["base_url"] or "127.0.0.1" in calls[0]["base_url"]
        assert calls[0]["max_retries"] == 0

    def test_no_client_and_no_package_raises_a_clear_import_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import player2.agent.ollama_transport as module

        monkeypatch.setattr(module, "openai", None)
        with pytest.raises(ImportError):
            OllamaTransport()
