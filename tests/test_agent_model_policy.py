"""Contract for player2.agent.model_policy.SDKPolicy -- the provider-agnostic decision seam.

SDKPolicy is the part of "direct SDK transport" that actually decides what to ask a model for
and how to interpret the answer. Every real network or subprocess call lives behind
IModelTransport in a separate, thin, largely-untestable adapter (Unit 4's lesson: put the
untestable part behind an injectable seam, then test everything on this side of it).

These tests use a FakeTransport and never construct a real anthropic/openai client or spawn
codex -- that is deliberate. If a test here needs a network call to pass, it belongs in one
of the transport test files instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pytest

from player2.agent.base import IDeliberativeModel, IFastPolicy, Observation
from player2.agent.model_policy import ModelTransportError, SDKPolicy
from player2.agent.model_schema import ACTION_CHUNK_TOOL_NAME, ModelResponseError
from player2.contracts import Frame
from player2.control.scheduler import EventKind, SchedulerEvent


def solid_frame(seq: int = 0, session_ms: float = 0.0) -> Frame:
    buffer = np.zeros((4, 4, 4), dtype=np.uint8)
    buffer.flags.writeable = False
    return Frame(seq=seq, session_ms=session_ms, source_ms=None, width=4, height=4,
                 pixel_format="BGRA8", data=buffer)


def observation(frames: tuple[Frame, ...] = (), goal: str | None = "test goal",
                 history: tuple[SchedulerEvent, ...] = ()) -> Observation:
    return Observation(frames=frames or (solid_frame(),), goal=goal, history=history,
                       observation_cutoff_ms=0.0, deadline_ms=100.0, epoch=0, decision_seq=0)


VALID_CHUNK_PAYLOAD: dict[str, Any] = {"keyframes": [{"t_ms": 0.0, "left_stick": [1.0, 0.0]},
                                                       {"t_ms": 300.0}]}


@dataclass
class _ToolCall:
    system: str
    prompt: str
    images: tuple[bytes, ...]
    tool_name: str
    tool_schema: dict[str, Any]
    timeout_s: float


@dataclass
class _TextCall:
    system: str
    prompt: str
    images: tuple[bytes, ...]
    timeout_s: float


class FakeTransport:
    """Record every call it receives and return caller-scripted results or errors."""

    def __init__(self, *, tool_result: dict[str, Any] | None = None,
                 tool_error: Exception | None = None,
                 text_result: str | None = None,
                 text_error: Exception | None = None) -> None:
        self.tool_result = tool_result
        self.tool_error = tool_error
        self.text_result = text_result
        self.text_error = text_error
        self.tool_calls: list[_ToolCall] = []
        self.text_calls: list[_TextCall] = []

    def complete_tool(self, *, system: str, prompt: str, images: tuple[bytes, ...],
                       tool_name: str, tool_schema: dict[str, Any],
                       timeout_s: float) -> dict[str, Any]:
        self.tool_calls.append(_ToolCall(system, prompt, images, tool_name, tool_schema,
                                          timeout_s))
        if self.tool_error is not None:
            raise self.tool_error
        assert self.tool_result is not None
        return self.tool_result

    def complete_text(self, *, system: str, prompt: str, images: tuple[bytes, ...],
                       timeout_s: float) -> str:
        self.text_calls.append(_TextCall(system, prompt, images, timeout_s))
        if self.text_error is not None:
            raise self.text_error
        assert self.text_result is not None
        return self.text_result


class TestSatisfiesBothPolicyInterfaces:
    def test_is_a_fast_policy(self) -> None:
        assert isinstance(SDKPolicy(transport=FakeTransport()), IFastPolicy)

    def test_is_a_deliberative_model(self) -> None:
        assert isinstance(SDKPolicy(transport=FakeTransport()), IDeliberativeModel)


class TestPropose:
    def test_returns_a_parsed_action_chunk_from_the_transport_reply(self) -> None:
        transport = FakeTransport(tool_result=VALID_CHUNK_PAYLOAD)
        policy = SDKPolicy(transport=transport)
        chunk = policy.propose(observation())
        assert chunk is not None
        assert chunk.keyframes[0].left_stick == (1.0, 0.0)

    def test_sends_the_observation_as_images_and_a_prompt(self) -> None:
        transport = FakeTransport(tool_result=VALID_CHUNK_PAYLOAD)
        policy = SDKPolicy(transport=transport)
        policy.propose(observation(goal="walk a spiral"))
        call = transport.tool_calls[0]
        assert len(call.images) == 1
        assert "walk a spiral" in call.prompt

    def test_requests_the_action_chunk_tool_by_name(self) -> None:
        transport = FakeTransport(tool_result=VALID_CHUNK_PAYLOAD)
        SDKPolicy(transport=transport).propose(observation())
        assert transport.tool_calls[0].tool_name == ACTION_CHUNK_TOOL_NAME

    def test_a_malformed_reply_raises_rather_than_returning_a_bad_chunk(self) -> None:
        """AgentLoop already wraps policy.propose() in a broad except and counts the error
        (see loop.py's _decide) -- SDKPolicy must not swallow this itself and return a chunk
        assembled from garbage, which would be far worse than no chunk this tick."""
        transport = FakeTransport(tool_result={"keyframes": []})
        policy = SDKPolicy(transport=transport)
        with pytest.raises(ModelResponseError):
            policy.propose(observation())

    def test_a_transport_failure_propagates(self) -> None:
        transport = FakeTransport(tool_error=ModelTransportError("network gone"))
        policy = SDKPolicy(transport=transport)
        with pytest.raises(ModelTransportError):
            policy.propose(observation())

    def test_passes_the_configured_timeout_through(self) -> None:
        transport = FakeTransport(tool_result=VALID_CHUNK_PAYLOAD)
        policy = SDKPolicy(transport=transport, timeout_s=3.5)
        policy.propose(observation())
        assert transport.tool_calls[0].timeout_s == 3.5

    def test_commentary_is_optional_in_the_tool_schema_by_default(self) -> None:
        transport = FakeTransport(tool_result=VALID_CHUNK_PAYLOAD)
        SDKPolicy(transport=transport).propose(observation())
        assert "commentary" not in transport.tool_calls[0].tool_schema["required"]

    def test_require_commentary_makes_it_required_in_the_tool_schema(self) -> None:
        """Confirmed live (docs/DEV-PROCESS.md, Unit 14) that the default optional field goes
        unused against a local grammar-constrained model regardless of prompt wording --
        `require_commentary` is the opt-in override that actually changes that behavior, by
        changing the schema's own `required` list rather than asking harder in English."""
        transport = FakeTransport(tool_result=VALID_CHUNK_PAYLOAD)
        policy = SDKPolicy(transport=transport, require_commentary=True)
        policy.propose(observation())
        assert "commentary" in transport.tool_calls[0].tool_schema["required"]

    def test_passes_the_configured_history_window_through(self) -> None:
        """A live 30-minute session (docs/DEV-PROCESS.md) grew its prompt past the model's
        4096-token context ceiling because history was rendered in full -- SDKPolicy must
        actually apply its configured window, not just accept the constructor argument."""
        events = tuple(
            SchedulerEvent(kind=EventKind.PREEMPTED, session_ms=float(i), detail=f"seq={i}")
            for i in range(10)
        )
        transport = FakeTransport(tool_result=VALID_CHUNK_PAYLOAD)
        policy = SDKPolicy(transport=transport, history_window=3)
        policy.propose(observation(history=events))
        prompt = transport.tool_calls[0].prompt
        assert "seq=9" in prompt
        assert "seq=6" not in prompt


class TestDeliberate:
    def test_returns_the_transport_text_stripped(self) -> None:
        transport = FakeTransport(text_result="  clear the nearest room  ")
        policy = SDKPolicy(transport=transport)
        assert policy.deliberate(observation()) == "clear the nearest room"

    def test_blank_reply_becomes_none_not_an_empty_string(self) -> None:
        """base.py's IDeliberativeModel contract is str | None; an empty-but-truthy '' would
        be silently treated as a real goal by any caller that only checks `is not None`."""
        transport = FakeTransport(text_result="   ")
        policy = SDKPolicy(transport=transport)
        assert policy.deliberate(observation()) is None

    def test_sends_images_and_prompt_like_propose_does(self) -> None:
        transport = FakeTransport(text_result="goal")
        SDKPolicy(transport=transport).deliberate(observation(goal="scout the area"))
        assert "scout the area" in transport.text_calls[0].prompt
        assert len(transport.text_calls[0].images) == 1

    def test_passes_the_configured_history_window_through(self) -> None:
        """propose() and deliberate() both build a prompt from the same observation --
        deliberate() must get the same history_window protection, not just propose()."""
        events = tuple(
            SchedulerEvent(kind=EventKind.PREEMPTED, session_ms=float(i), detail=f"seq={i}")
            for i in range(10)
        )
        transport = FakeTransport(text_result="goal")
        policy = SDKPolicy(transport=transport, history_window=3)
        policy.deliberate(observation(history=events))
        prompt = transport.text_calls[0].prompt
        assert "seq=9" in prompt
        assert "seq=6" not in prompt

    def test_a_transport_failure_propagates(self) -> None:
        transport = FakeTransport(text_error=ModelTransportError("timed out"))
        policy = SDKPolicy(transport=transport)
        with pytest.raises(ModelTransportError):
            policy.deliberate(observation())


class TestConstruction:
    def test_rejects_a_non_positive_timeout(self) -> None:
        with pytest.raises(ValueError):
            SDKPolicy(transport=FakeTransport(), timeout_s=0.0)

    def test_rejects_a_non_finite_timeout(self) -> None:
        with pytest.raises(ValueError):
            SDKPolicy(transport=FakeTransport(), timeout_s=float("nan"))

    def test_rejects_a_non_positive_history_window(self) -> None:
        with pytest.raises(ValueError):
            SDKPolicy(transport=FakeTransport(), history_window=0)

    def test_rejects_a_non_bool_require_commentary(self) -> None:
        with pytest.raises(ValueError):
            SDKPolicy(transport=FakeTransport(), require_commentary="yes")  # type: ignore[arg-type]

    def test_default_construction_needs_only_a_transport(self) -> None:
        SDKPolicy(transport=FakeTransport())
