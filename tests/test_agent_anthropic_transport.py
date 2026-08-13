"""Contract for player2.agent.anthropic_transport.AnthropicTransport.

This is the thin, largely-untestable-for-real seam: everything here is a fake client
injected in place of `anthropic.Anthropic()`, shaped exactly like the real SDK's response
objects. No network call, no API key, no real model. That is deliberate -- Unit 4's review
found the real cost of skipping this seam: 13 defects that could only be smoke-tested because
nothing stood between the logic and the graphics API. Here the equivalent seam is `client`.

What this file is NOT responsible for proving: that anthropic.Anthropic().messages.create
actually behaves the way these fakes assume. That gap is closed by a manual smoke run against
a real key before this transport is trusted in a live session, not by unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx
import pytest

from player2.agent.budget import TokenUsage
from player2.agent.model_policy import ModelTransportError

pytest.importorskip("anthropic")

from player2.agent.anthropic_transport import AnthropicTransport  # noqa: E402


@dataclass
class _TextBlock:
    text: str
    type: str = "text"


@dataclass
class _ToolUseBlock:
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class _FakeUsage:
    input_tokens: int
    output_tokens: int


@dataclass
class _FakeMessage:
    content: list[Any]
    usage: Any = None


@dataclass
class _RecordedCreate:
    kwargs: dict[str, Any]


class _FakeMessages:
    def __init__(self, *, result: _FakeMessage | None = None,
                 error: Exception | None = None) -> None:
        self._result = result
        self._error = error
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeMessage:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


@dataclass
class _FakeClient:
    messages: _FakeMessages = field(default_factory=_FakeMessages)


def connection_error() -> anthropic.APIConnectionError:
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://example.invalid"))


class TestCompleteTool:
    def test_returns_the_tool_use_input(self) -> None:
        block = _ToolUseBlock(name="submit_action_chunk", input={"keyframes": []})
        client = _FakeClient(messages=_FakeMessages(result=_FakeMessage(content=[block])))
        transport = AnthropicTransport(client=client)
        result = transport.complete_tool(
            system="sys", prompt="go", images=(), tool_name="submit_action_chunk",
            tool_schema={"type": "object"}, timeout_s=5.0,
        )
        assert result == {"keyframes": []}

    def test_ignores_a_tool_use_block_with_a_different_name(self) -> None:
        blocks = [_ToolUseBlock(name="wrong_tool", input={"a": 1}),
                  _ToolUseBlock(name="submit_action_chunk", input={"keyframes": []})]
        client = _FakeClient(messages=_FakeMessages(result=_FakeMessage(content=blocks)))
        transport = AnthropicTransport(client=client)
        result = transport.complete_tool(
            system="sys", prompt="go", images=(), tool_name="submit_action_chunk",
            tool_schema={"type": "object"}, timeout_s=5.0,
        )
        assert result == {"keyframes": []}

    def test_raises_when_no_matching_tool_use_block_is_present(self) -> None:
        client = _FakeClient(messages=_FakeMessages(
            result=_FakeMessage(content=[_TextBlock(text="I refuse to call the tool")])
        ))
        transport = AnthropicTransport(client=client)
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(),
                                    tool_name="submit_action_chunk",
                                    tool_schema={"type": "object"}, timeout_s=5.0)

    def test_wraps_a_provider_api_error(self) -> None:
        client = _FakeClient(messages=_FakeMessages(error=connection_error()))
        transport = AnthropicTransport(client=client)
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(),
                                    tool_name="submit_action_chunk",
                                    tool_schema={"type": "object"}, timeout_s=5.0)

    def test_forces_the_named_tool_via_tool_choice(self) -> None:
        """Without forcing tool_choice, the model can reply with prose instead of a call, and
        the fast-policy loop has an 8.3ms tick budget -- there is no turn for 'try again'."""
        block = _ToolUseBlock(name="submit_action_chunk", input={"keyframes": []})
        client = _FakeClient(messages=_FakeMessages(result=_FakeMessage(content=[block])))
        transport = AnthropicTransport(client=client)
        transport.complete_tool(system="sys", prompt="go", images=(),
                                tool_name="submit_action_chunk",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        call = client.messages.calls[0]
        assert call["tool_choice"] == {"type": "tool", "name": "submit_action_chunk"}
        assert call["tools"][0]["name"] == "submit_action_chunk"
        assert call["tools"][0]["input_schema"] == {"type": "object"}

    def test_sends_images_as_base64_content_blocks(self) -> None:
        block = _ToolUseBlock(name="t", input={"keyframes": []})
        client = _FakeClient(messages=_FakeMessages(result=_FakeMessage(content=[block])))
        transport = AnthropicTransport(client=client)
        transport.complete_tool(system="sys", prompt="go", images=(b"\xff\xd8fake",),
                                tool_name="t", tool_schema={"type": "object"}, timeout_s=5.0)
        content = client.messages.calls[0]["messages"][0]["content"]
        image_blocks = [b for b in content if b.get("type") == "image"]
        assert len(image_blocks) == 1
        assert image_blocks[0]["source"]["media_type"] == "image/jpeg"

    def test_forwards_the_system_prompt_and_timeout(self) -> None:
        block = _ToolUseBlock(name="t", input={"keyframes": []})
        client = _FakeClient(messages=_FakeMessages(result=_FakeMessage(content=[block])))
        transport = AnthropicTransport(client=client)
        transport.complete_tool(system="be careful", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=2.5)
        call = client.messages.calls[0]
        assert call["system"] == "be careful"
        assert call["timeout"] == 2.5


class TestCompleteText:
    def test_joins_all_text_blocks(self) -> None:
        client = _FakeClient(messages=_FakeMessages(
            result=_FakeMessage(content=[_TextBlock(text="hello "), _TextBlock(text="world")])
        ))
        transport = AnthropicTransport(client=client)
        result = transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)
        assert result == "hello world"

    def test_ignores_non_text_blocks(self) -> None:
        blocks = [_ToolUseBlock(name="t", input={}), _TextBlock(text="the goal")]
        client = _FakeClient(messages=_FakeMessages(result=_FakeMessage(content=blocks)))
        transport = AnthropicTransport(client=client)
        result = transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)
        assert result == "the goal"

    def test_wraps_a_provider_api_error(self) -> None:
        client = _FakeClient(messages=_FakeMessages(error=connection_error()))
        transport = AnthropicTransport(client=client)
        with pytest.raises(ModelTransportError):
            transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)


class TestConstruction:
    def test_uses_an_injected_client_without_touching_credentials(self) -> None:
        """The whole point of the seam: constructing a transport for tests must never require
        an API key, network access, or the ANTHROPIC_API_KEY environment variable."""
        client = _FakeClient()
        AnthropicTransport(client=client)

    def test_accepts_a_model_override(self) -> None:
        block = _ToolUseBlock(name="t", input={"keyframes": []})
        client = _FakeClient(messages=_FakeMessages(result=_FakeMessage(content=[block])))
        transport = AnthropicTransport(client=client, model="claude-fable-5")
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        assert client.messages.calls[0]["model"] == "claude-fable-5"


class TestLastUsage:
    """The budget governor reads this after every call (player2.agent.budget.GovernedTransport)
    -- it must reflect the real provider-reported cost, not an estimate, whenever the SDK
    supplies one."""

    def test_reflects_the_providers_reported_usage_after_a_tool_call(self) -> None:
        block = _ToolUseBlock(name="t", input={"keyframes": []})
        usage = _FakeUsage(input_tokens=812, output_tokens=64)
        client = _FakeClient(messages=_FakeMessages(
            result=_FakeMessage(content=[block], usage=usage)
        ))
        transport = AnthropicTransport(client=client)
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        assert transport.last_usage is not None
        assert transport.last_usage.input_tokens == 812
        assert transport.last_usage.output_tokens == 64

    def test_reflects_usage_after_a_text_call_too(self) -> None:
        usage = _FakeUsage(input_tokens=300, output_tokens=12)
        client = _FakeClient(messages=_FakeMessages(
            result=_FakeMessage(content=[_TextBlock(text="a goal")], usage=usage)
        ))
        transport = AnthropicTransport(client=client)
        transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)
        assert transport.last_usage == TokenUsage(input_tokens=300, output_tokens=12)

    def test_is_none_before_any_call_is_made(self) -> None:
        transport = AnthropicTransport(client=_FakeClient())
        assert transport.last_usage is None

    def test_is_none_when_the_response_carries_no_usage_field(self) -> None:
        block = _ToolUseBlock(name="t", input={"keyframes": []})
        client = _FakeClient(messages=_FakeMessages(
            result=_FakeMessage(content=[block], usage=None)
        ))
        transport = AnthropicTransport(client=client)
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        assert transport.last_usage is None

    def test_is_left_unchanged_by_a_failed_call(self) -> None:
        """A stale usage figure from a previous successful call would misreport the cost of
        a call that never actually happened."""
        good = _FakeUsage(input_tokens=100, output_tokens=10)
        block = _ToolUseBlock(name="t", input={"keyframes": []})
        client = _FakeClient(messages=_FakeMessages(
            result=_FakeMessage(content=[block], usage=good)
        ))
        transport = AnthropicTransport(client=client)
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        client.messages._error = connection_error()
        client.messages._result = None
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                    tool_schema={"type": "object"}, timeout_s=5.0)
        assert transport.last_usage == TokenUsage(input_tokens=100, output_tokens=10)
