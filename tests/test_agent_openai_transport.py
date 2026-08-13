"""Contract for player2.agent.openai_transport.OpenAITransport.

Like the anthropic transport tests, everything here runs against a fake client shaped like
the real openai SDK's chat.completions.create response -- no network, no API key.

Unlike anthropic, the `openai` package is not installed in every environment (models is an
opt-in extra, matching how CLAUDE.md requires voice to stay opt-in: nothing in the core
imports it, and a machine without the extra pays nothing). These tests inject a fake client
directly and must therefore pass whether or not the real `openai` package is present -- that
is itself part of the contract, so there is no importorskip here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from player2.agent.budget import TokenUsage
from player2.agent.model_policy import ModelTransportError
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
class _Usage:
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int = 0


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


def make_client(*, result: _ChatCompletion | None = None,
                error: Exception | None = None) -> _FakeClient:
    return _FakeClient(chat=_FakeChat(completions=_FakeCompletions(result=result, error=error)))


def tool_call_response(name: str, arguments: dict[str, Any]) -> _ChatCompletion:
    call = _ToolCall(function=_FunctionCall(name=name, arguments=json.dumps(arguments)))
    return _ChatCompletion(choices=[_Choice(message=_Message(tool_calls=[call]))])


class TestCompleteTool:
    def test_returns_the_parsed_tool_call_arguments(self) -> None:
        client = make_client(result=tool_call_response("submit_action_chunk", {"keyframes": []}))
        transport = OpenAITransport(client=client)
        result = transport.complete_tool(system="sys", prompt="go", images=(),
                                         tool_name="submit_action_chunk",
                                         tool_schema={"type": "object"}, timeout_s=5.0)
        assert result == {"keyframes": []}

    def test_raises_when_no_tool_call_is_present(self) -> None:
        client = make_client(result=_ChatCompletion(
            choices=[_Choice(message=_Message(content="I won't call the tool"))]
        ))
        transport = OpenAITransport(client=client)
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                    tool_schema={"type": "object"}, timeout_s=5.0)

    def test_raises_on_a_tool_call_with_the_wrong_name(self) -> None:
        client = make_client(result=tool_call_response("some_other_tool", {"a": 1}))
        transport = OpenAITransport(client=client)
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(),
                                    tool_name="submit_action_chunk",
                                    tool_schema={"type": "object"}, timeout_s=5.0)

    def test_raises_on_malformed_json_arguments_rather_than_crashing(self) -> None:
        call = _ToolCall(function=_FunctionCall(name="t", arguments="{not json"))
        client = make_client(result=_ChatCompletion(
            choices=[_Choice(message=_Message(tool_calls=[call]))]
        ))
        transport = OpenAITransport(client=client)
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                    tool_schema={"type": "object"}, timeout_s=5.0)

    def test_wraps_a_client_exception(self) -> None:
        client = make_client(error=RuntimeError("connection refused"))
        transport = OpenAITransport(client=client)
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                    tool_schema={"type": "object"}, timeout_s=5.0)

    def test_forces_the_named_tool_via_tool_choice(self) -> None:
        client = make_client(result=tool_call_response("t", {"keyframes": []}))
        transport = OpenAITransport(client=client)
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        call = client.chat.completions.calls[0]
        assert call["tool_choice"] == {"type": "function", "function": {"name": "t"}}
        assert call["tools"][0]["function"]["name"] == "t"
        assert call["tools"][0]["function"]["parameters"] == {"type": "object"}

    def test_sends_images_as_data_urls(self) -> None:
        client = make_client(result=tool_call_response("t", {"keyframes": []}))
        transport = OpenAITransport(client=client)
        transport.complete_tool(system="sys", prompt="go", images=(b"\xff\xd8fake",),
                                tool_name="t", tool_schema={"type": "object"}, timeout_s=5.0)
        content = client.chat.completions.calls[0]["messages"][-1]["content"]
        image_blocks = [b for b in content if b.get("type") == "image_url"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_forwards_system_as_its_own_message_and_timeout(self) -> None:
        client = make_client(result=tool_call_response("t", {"keyframes": []}))
        transport = OpenAITransport(client=client)
        transport.complete_tool(system="be careful", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=2.5)
        call = client.chat.completions.calls[0]
        assert call["messages"][0] == {"role": "system", "content": "be careful"}
        assert call["timeout"] == 2.5


class TestCompleteText:
    def test_returns_the_message_content(self) -> None:
        client = make_client(result=_ChatCompletion(
            choices=[_Choice(message=_Message(content="the goal"))]
        ))
        transport = OpenAITransport(client=client)
        result = transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)
        assert result == "the goal"

    def test_missing_content_becomes_empty_string_not_none(self) -> None:
        """complete_text's declared return type is str; SDKPolicy.deliberate strips and
        blank-checks the result itself, so this boundary must never hand back None."""
        client = make_client(result=_ChatCompletion(
            choices=[_Choice(message=_Message(content=None))]
        ))
        transport = OpenAITransport(client=client)
        result = transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)
        assert result == ""

    def test_wraps_a_client_exception(self) -> None:
        client = make_client(error=RuntimeError("connection refused"))
        transport = OpenAITransport(client=client)
        with pytest.raises(ModelTransportError):
            transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)


class TestConstruction:
    def test_uses_an_injected_client_without_requiring_the_openai_package(self) -> None:
        """Whether or not `openai` is importable in this environment, injecting a client
        must be enough to construct and use a transport. This is what keeps the extra
        genuinely optional rather than merely undocumented."""
        client = make_client(result=tool_call_response("t", {"keyframes": []}))
        OpenAITransport(client=client)

    def test_accepts_a_model_override(self) -> None:
        client = make_client(result=tool_call_response("t", {"keyframes": []}))
        transport = OpenAITransport(client=client, model="gpt-5.6-terra")
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        assert client.chat.completions.calls[0]["model"] == "gpt-5.6-terra"

    def test_no_client_and_no_package_raises_a_clear_import_error(self) -> None:
        """Constructing without an injected client falls through to `openai.OpenAI()`. When
        the optional extra is not installed this must fail with an actionable message, not a
        bare ModuleNotFoundError pointing at an internal import line."""
        import player2.agent.openai_transport as module
        if module.openai is not None:
            pytest.skip("openai package is installed in this environment")
        with pytest.raises(ImportError):
            OpenAITransport()


class TestLastUsage:
    """The budget governor reads this after every call (player2.agent.budget.GovernedTransport)
    -- OllamaTransport inherits this behavior unchanged, so these cases cover it too."""

    def test_reflects_the_providers_reported_usage_after_a_tool_call(self) -> None:
        response = tool_call_response("t", {"keyframes": []})
        response.usage = _Usage(prompt_tokens=700, completion_tokens=50)
        transport = OpenAITransport(client=make_client(result=response))
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        assert transport.last_usage == TokenUsage(input_tokens=700, output_tokens=50)

    def test_reflects_usage_after_a_text_call_too(self) -> None:
        response = _ChatCompletion(choices=[_Choice(message=_Message(content="a goal"))],
                                   usage=_Usage(prompt_tokens=200, completion_tokens=8))
        transport = OpenAITransport(client=make_client(result=response))
        transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)
        assert transport.last_usage == TokenUsage(input_tokens=200, output_tokens=8)

    def test_is_none_before_any_call_is_made(self) -> None:
        transport = OpenAITransport(client=make_client())
        assert transport.last_usage is None

    def test_is_none_when_the_response_carries_no_usage_field(self) -> None:
        response = tool_call_response("t", {"keyframes": []})
        transport = OpenAITransport(client=make_client(result=response))
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        assert transport.last_usage is None

    def test_is_left_unchanged_by_a_failed_call(self) -> None:
        response = tool_call_response("t", {"keyframes": []})
        response.usage = _Usage(prompt_tokens=50, completion_tokens=5)
        client = make_client(result=response)
        transport = OpenAITransport(client=client)
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        client.chat.completions._error = RuntimeError("boom")
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                    tool_schema={"type": "object"}, timeout_s=5.0)
        assert transport.last_usage == TokenUsage(input_tokens=50, output_tokens=5)
