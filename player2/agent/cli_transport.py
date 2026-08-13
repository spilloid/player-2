"""Run Codex CLI completions as a zero-credential model transport."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from player2.agent.model_policy import ModelTransportError

_DEFAULT_MODEL = "gpt-5.6-terra"
_DEFAULT_EFFORT = "medium"

type _Runner = Callable[[list[str], str, float, str], subprocess.CompletedProcess[str]]

# Retain only the latest successful call's directory for immediate diagnostics, then clean it
# when another call succeeds or the interpreter exits so a long session has bounded temp usage.
_RETAINED_TEMP_DIRECTORY: tempfile.TemporaryDirectory[str] | None = None
_RETENTION_LOCK = threading.Lock()


def _kill_process_tree(pid: int) -> None:
    """Terminate a timed-out call's whole process tree, not just its direct child.

    Verified directly against real Windows process semantics before this was written:
    TerminateProcess (what Popen.kill() sends) does not cascade to descendants, and
    `codex.cmd` is an npm shim that spawns a real node.exe child -- a bare kill leaves that
    child running past the timeout it was supposed to enforce. `taskkill /T /F` does not
    have this gap; confirmed with a synthetic parent/grandchild reproduction.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _subprocess_runner(
    args: list[str], stdin_text: str, timeout_s: float, cwd: str
) -> subprocess.CompletedProcess[str]:
    """Invoke Codex without a shell, confined to a harmless per-call directory.

    `cwd` is always the same disposable temp directory the caller wrote images and a
    schema into -- never the real process working directory -- so a model that decides to
    look around before answering (`-s read-only` still permits reads and read-oriented
    tools) finds nothing sensitive to read.
    """
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
        )
    except OSError as error:
        raise ModelTransportError(f"could not run codex exec: {error}") from error
    try:
        stdout, stderr = process.communicate(input=stdin_text, timeout=timeout_s)
    except subprocess.TimeoutExpired as error:
        _kill_process_tree(process.pid)
        try:
            process.communicate(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass
        raise ModelTransportError(
            f"codex exec timed out after {timeout_s:g} seconds"
        ) from error
    except subprocess.SubprocessError as error:
        raise ModelTransportError(f"could not run codex exec: {error}") from error
    return subprocess.CompletedProcess(args, cast(int, process.returncode), stdout, stderr)


def _retain_temp_directory(directory: tempfile.TemporaryDirectory[str]) -> None:
    """Bound diagnostic retention while preserving the just-completed call for inspection."""
    global _RETAINED_TEMP_DIRECTORY

    with _RETENTION_LOCK:
        previous = _RETAINED_TEMP_DIRECTORY
        _RETAINED_TEMP_DIRECTORY = directory
    if previous is not None:
        previous.cleanup()


class CLITransport:
    """Adapt ``codex exec`` to the provider-independent model transport boundary."""

    def __init__(
        self,
        *,
        runner: _Runner | None = None,
        binary: str = "codex",
        model: str = _DEFAULT_MODEL,
        effort: str = _DEFAULT_EFFORT,
    ) -> None:
        """Inject process execution so tests need neither the real CLI nor its credentials."""
        self._runner = runner or _subprocess_runner
        self._binary = binary
        self._model = model
        self._effort = effort

    @property
    def last_usage(self) -> None:
        """Codex CLI output does not contain provider token metadata."""
        return None

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
        """Return a JSON object constrained by the requested tool's argument schema."""
        del tool_name  # The CLI constrains its final response directly rather than calling a tool.
        try:
            directory = tempfile.TemporaryDirectory(
                prefix="player2-codex-", ignore_cleanup_errors=True
            )
        except OSError as error:
            raise ModelTransportError(f"could not prepare codex exec inputs: {error}") from error
        retained = False
        try:
            root = Path(directory.name)
            try:
                schema_path = root / "tool-schema.json"
                schema_path.write_text(json.dumps(tool_schema), encoding="utf-8")
                image_paths = self._write_images(root, images)
                output_path = root / "output.json"
            except (OSError, TypeError, ValueError) as error:
                raise ModelTransportError(
                    f"could not prepare codex exec inputs: {error}"
                ) from error

            output = self._invoke(
                system=system,
                prompt=prompt,
                image_paths=image_paths,
                output_path=output_path,
                schema_path=schema_path,
                timeout_s=timeout_s,
            )
            try:
                decoded: object = json.loads(output)
            except (json.JSONDecodeError, TypeError) as error:
                raise ModelTransportError("codex exec returned invalid JSON") from error
            if not isinstance(decoded, dict):
                raise ModelTransportError("codex exec returned JSON that was not an object")
            _retain_temp_directory(directory)
            retained = True
            return cast(dict[str, Any], decoded)
        finally:
            if not retained:
                directory.cleanup()

    def complete_text(
        self,
        *,
        system: str,
        prompt: str,
        images: tuple[bytes, ...],
        timeout_s: float,
    ) -> str:
        """Return the CLI's final message without imposing a structured output schema."""
        try:
            directory = tempfile.TemporaryDirectory(
                prefix="player2-codex-", ignore_cleanup_errors=True
            )
        except OSError as error:
            raise ModelTransportError(f"could not prepare codex exec inputs: {error}") from error
        retained = False
        try:
            root = Path(directory.name)
            try:
                image_paths = self._write_images(root, images)
                output_path = root / "output.txt"
            except OSError as error:
                raise ModelTransportError(
                    f"could not prepare codex exec inputs: {error}"
                ) from error
            output = self._invoke(
                system=system,
                prompt=prompt,
                image_paths=image_paths,
                output_path=output_path,
                schema_path=None,
                timeout_s=timeout_s,
            )
            _retain_temp_directory(directory)
            retained = True
            return output
        finally:
            if not retained:
                directory.cleanup()

    @staticmethod
    def _write_images(root: Path, images: tuple[bytes, ...]) -> tuple[Path, ...]:
        paths: list[Path] = []
        for index, image in enumerate(images):
            path = root / f"image-{index}.jpg"
            path.write_bytes(image)
            paths.append(path)
        return tuple(paths)

    def _invoke(
        self,
        *,
        system: str,
        prompt: str,
        image_paths: tuple[Path, ...],
        output_path: Path,
        schema_path: Path | None,
        timeout_s: float,
    ) -> str:
        args = [
            self._binary,
            "exec",
            "-m",
            self._model,
            "-c",
            f'model_reasoning_effort="{self._effort}"',
            "-s",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "-o",
            str(output_path),
        ]
        if schema_path is not None:
            args.extend(("--output-schema", str(schema_path)))
        for image_path in image_paths:
            args.extend(("-i", str(image_path)))

        stdin_text = f"{system}\n\n{prompt}"
        try:
            result = self._runner(args, stdin_text, timeout_s, str(output_path.parent))
        except subprocess.TimeoutExpired as error:
            raise ModelTransportError(
                f"codex exec timed out after {timeout_s:g} seconds"
            ) from error
        except (OSError, subprocess.SubprocessError) as error:
            raise ModelTransportError(f"could not run codex exec: {error}") from error

        if result.returncode != 0:
            stderr = result.stderr if isinstance(result.stderr, str) else ""
            stdout = result.stdout if isinstance(result.stdout, str) else ""
            detail = stderr.strip() or stdout.strip()
            suffix = f": {detail}" if detail else ""
            raise ModelTransportError(
                f"codex exec exited with status {result.returncode}{suffix}"
            )
        try:
            return output_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ModelTransportError(f"could not read codex exec output: {error}") from error
