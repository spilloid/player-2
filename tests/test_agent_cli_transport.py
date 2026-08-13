"""Contract for player2.agent.cli_transport.CLITransport -- the zero-credential fallback.

CARRYOVER.md is explicit that the direct-SDK transport should leave the codex-exec CLI path
in place as a fallback that needs no API key of its own, and that the ~12.5k-token harness
floor measured there came specifically from `codex exec`'s invocation shape (stdin prompt,
`-i` per image, `-o`/`--output-schema` for the reply) -- not from calling a bare HTTP API.
This file pins that shape down as a contract, using a fake process runner instead of actually
spawning `codex`, per CLAUDE.md: reviewers get `-s read-only`, and this transport is used only
to ask a question, never to write to the repo, so it must always pass that flag too.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from player2.agent.cli_transport import CLITransport
from player2.agent.model_policy import ModelTransportError

_Runner = Callable[[list[str], str, float, str], "subprocess.CompletedProcess[str]"]
_Call = tuple[list[str], str, float, str]


def _output_path(args: list[str]) -> Path:
    return Path(args[args.index("-o") + 1])


def fake_runner(*, stdout_json: object = None, stdout_text: str | None = None,
                returncode: int = 0, stderr: str = "",
                raise_timeout: bool = False) -> tuple[_Runner, list[_Call]]:
    calls: list[_Call] = []

    def runner(
        args: list[str], stdin_text: str, timeout_s: float, cwd: str,
    ) -> subprocess.CompletedProcess[str]:
        calls.append((args, stdin_text, timeout_s, cwd))
        if raise_timeout:
            raise subprocess.TimeoutExpired(cmd=args, timeout=timeout_s)
        if returncode == 0:
            text = json.dumps(stdout_json) if stdout_json is not None else (stdout_text or "")
            _output_path(args).write_text(text, encoding="utf-8")
        return subprocess.CompletedProcess(args=args, returncode=returncode, stdout="",
                                            stderr=stderr)

    return runner, calls


class TestCompleteTool:
    def test_returns_the_decoded_json_object_from_the_output_file(self) -> None:
        runner, _ = fake_runner(stdout_json={"keyframes": []})
        transport = CLITransport(runner=runner)
        result = transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                         tool_schema={"type": "object"}, timeout_s=5.0)
        assert result == {"keyframes": []}

    def test_raises_on_a_nonzero_exit_code(self) -> None:
        runner, _ = fake_runner(returncode=1, stderr="codex: auth failed")
        transport = CLITransport(runner=runner)
        with pytest.raises(ModelTransportError, match="auth failed"):
            transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                    tool_schema={"type": "object"}, timeout_s=5.0)

    def test_raises_on_output_that_is_not_valid_json(self) -> None:
        runner, _ = fake_runner(stdout_text="not json at all")
        transport = CLITransport(runner=runner)
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                    tool_schema={"type": "object"}, timeout_s=5.0)

    def test_wraps_a_subprocess_timeout(self) -> None:
        runner, _ = fake_runner(raise_timeout=True)
        transport = CLITransport(runner=runner)
        with pytest.raises(ModelTransportError):
            transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                    tool_schema={"type": "object"}, timeout_s=5.0)

    def test_always_runs_read_only_and_ephemeral(self) -> None:
        """This transport only ever asks a question; it must never be able to write to the
        repo or leave state behind, regardless of what a future caller passes in."""
        runner, calls = fake_runner(stdout_json={"keyframes": []})
        CLITransport(runner=runner).complete_tool(system="sys", prompt="go", images=(),
                                                   tool_name="t", tool_schema={"type": "object"},
                                                   timeout_s=5.0)
        args = calls[0][0]
        assert "-s" in args and args[args.index("-s") + 1] == "read-only"
        assert "--ephemeral" in args
        assert "--skip-git-repo-check" in args

    def test_passes_the_schema_via_output_schema(self) -> None:
        runner, calls = fake_runner(stdout_json={"keyframes": []})
        CLITransport(runner=runner).complete_tool(system="sys", prompt="go", images=(),
                                                   tool_name="t", tool_schema={"type": "object"},
                                                   timeout_s=5.0)
        args = calls[0][0]
        assert "--output-schema" in args
        schema_path = Path(args[args.index("--output-schema") + 1])
        assert json.loads(schema_path.read_text()) == {"type": "object"}

    def test_writes_one_temp_file_per_image_and_passes_dash_i(self) -> None:
        runner, calls = fake_runner(stdout_json={"keyframes": []})
        CLITransport(runner=runner).complete_tool(
            system="sys", prompt="go", images=(b"\xff\xd8one", b"\xff\xd8two"),
            tool_name="t", tool_schema={"type": "object"}, timeout_s=5.0,
        )
        args = calls[0][0]
        image_flags = [i for i, a in enumerate(args) if a == "-i"]
        assert len(image_flags) == 2
        for index in image_flags:
            path = Path(args[index + 1])
            assert path.read_bytes() in (b"\xff\xd8one", b"\xff\xd8two")

    def test_sends_system_and_prompt_on_stdin(self) -> None:
        runner, calls = fake_runner(stdout_json={"keyframes": []})
        CLITransport(runner=runner).complete_tool(system="be careful", prompt="walk forward",
                                                   images=(), tool_name="t",
                                                   tool_schema={"type": "object"}, timeout_s=5.0)
        _, stdin_text, _, _ = calls[0]
        assert "be careful" in stdin_text
        assert "walk forward" in stdin_text

    def test_runs_confined_to_its_own_disposable_directory(self) -> None:
        """codex is never given the real process working directory. `-s read-only` still
        permits reads and read-oriented tools, so a model that decides to look around before
        answering must find nothing but the harmless artifacts this call itself wrote --
        never the repo, credentials, or anything else on the host."""
        runner, calls = fake_runner(stdout_json={"keyframes": []})
        CLITransport(runner=runner).complete_tool(system="sys", prompt="go", images=(),
                                                   tool_name="t", tool_schema={"type": "object"},
                                                   timeout_s=5.0)
        args, _, _, cwd = calls[0]
        assert cwd == str(_output_path(args).parent)

    def test_forwards_the_timeout(self) -> None:
        runner, calls = fake_runner(stdout_json={"keyframes": []})
        CLITransport(runner=runner).complete_tool(system="sys", prompt="go", images=(),
                                                   tool_name="t", tool_schema={"type": "object"},
                                                   timeout_s=42.0)
        assert calls[0][2] == 42.0

    def test_model_and_effort_are_configurable(self) -> None:
        runner, calls = fake_runner(stdout_json={"keyframes": []})
        transport = CLITransport(runner=runner, model="gpt-5.6-sol", effort="high")
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        args = calls[0][0]
        assert args[args.index("-m") + 1] == "gpt-5.6-sol"
        assert any("high" in a for a in args)


class TestCompleteText:
    def test_returns_the_raw_output_file_contents(self) -> None:
        runner, _ = fake_runner(stdout_text="clear the nearest room")
        transport = CLITransport(runner=runner)
        result = transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)
        assert result == "clear the nearest room"

    def test_does_not_pass_output_schema(self) -> None:
        """A free-text goal has no JSON schema to constrain -- passing one here would force
        deliberate() into the same structured shape propose() uses, defeating the point of
        having two different interfaces in base.py."""
        runner, calls = fake_runner(stdout_text="some goal")
        CLITransport(runner=runner).complete_text(system="sys", prompt="go", images=(),
                                                   timeout_s=5.0)
        assert "--output-schema" not in calls[0][0]

    def test_raises_on_a_nonzero_exit_code(self) -> None:
        runner, _ = fake_runner(returncode=1, stderr="boom")
        transport = CLITransport(runner=runner)
        with pytest.raises(ModelTransportError):
            transport.complete_text(system="sys", prompt="go", images=(), timeout_s=5.0)


class TestLastUsage:
    def test_is_always_none(self) -> None:
        """codex exec's -o file contains only the schema-shaped reply, no usage metadata --
        this transport genuinely cannot report real cost. GovernedTransport's estimate
        fallback (player2.agent.budget) exists specifically to cover this gap; it must not be
        papered over here with a fabricated number that looks more precise than it is."""
        runner, _ = fake_runner(stdout_json={"keyframes": []})
        transport = CLITransport(runner=runner)
        transport.complete_tool(system="sys", prompt="go", images=(), tool_name="t",
                                tool_schema={"type": "object"}, timeout_s=5.0)
        assert transport.last_usage is None
