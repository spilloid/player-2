"""Runtime configuration with a deliberately minimal ``KEY=VALUE`` dotenv parser.

The parser supports neither quoting, ``export``, multiline values, nor interpolation.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

_PROVIDERS = frozenset({"anthropic", "openai", "cli", "ollama"})


@dataclass(frozen=True)
class RuntimeConfig:
    """Resolved runtime settings."""

    provider: str
    model: str | None
    budget_tpm: float
    ollama_base_url: str | None
    ollama_keep_alive: str | None
    history_window: int | None
    recordings: str
    profile: str | None


def _resolve_value(
    cli: Mapping[str, object],
    environ: Mapping[str, str],
    dotenv: Mapping[str, str],
    *,
    cli_key: str,
    env_key: str,
    default: object,
) -> tuple[object, bool]:
    """Return a value and whether its source was the CLI mapping."""
    if cli_key in cli:
        return cli[cli_key], True
    if (value := environ.get(env_key)) != "" and value is not None:
        return value, False
    if (value := dotenv.get(env_key)) != "" and value is not None:
        return value, False
    return default, False


def _parse_budget(value: object) -> float:
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError as exc:
            raise ValueError(f"invalid budget_tpm value: {value!r}") from exc
    return cast(float, value)


def _parse_history_window(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"invalid history_window value: {value!r}") from exc
    return cast(int, value)


def resolve_runtime_config(
    cli: Mapping[str, object], *, environ: Mapping[str, str], dotenv: Mapping[str, str]
) -> RuntimeConfig:
    """Resolve config from CLI, environment, dotenv, and defaults in that order."""
    provider, provider_from_cli = _resolve_value(
        cli, environ, dotenv, cli_key="provider", env_key="PLAYER2_PROVIDER", default="anthropic"
    )
    if not provider_from_cli and provider not in _PROVIDERS:
        raise ValueError(f"invalid provider: {provider!r}")

    model, _ = _resolve_value(
        cli, environ, dotenv, cli_key="model", env_key="PLAYER2_MODEL", default=None
    )
    budget_tpm, _ = _resolve_value(
        cli,
        environ,
        dotenv,
        cli_key="budget_tpm",
        env_key="PLAYER2_BUDGET_TPM",
        default=60_000.0,
    )
    ollama_base_url, _ = _resolve_value(
        cli,
        environ,
        dotenv,
        cli_key="ollama_base_url",
        env_key="PLAYER2_OLLAMA_BASE_URL",
        default=None,
    )
    ollama_keep_alive, _ = _resolve_value(
        cli,
        environ,
        dotenv,
        cli_key="ollama_keep_alive",
        env_key="PLAYER2_OLLAMA_KEEP_ALIVE",
        default=None,
    )
    history_window, _ = _resolve_value(
        cli,
        environ,
        dotenv,
        cli_key="history_window",
        env_key="PLAYER2_HISTORY_WINDOW",
        default=None,
    )
    recordings, _ = _resolve_value(
        cli,
        environ,
        dotenv,
        cli_key="recordings",
        env_key="PLAYER2_RECORDINGS",
        default="recordings",
    )
    profile, _ = _resolve_value(
        cli, environ, dotenv, cli_key="profile", env_key="PLAYER2_PROFILE", default=None
    )

    return RuntimeConfig(
        provider=cast(str, provider),
        model=cast(str | None, model),
        budget_tpm=_parse_budget(budget_tpm),
        ollama_base_url=cast(str | None, ollama_base_url),
        ollama_keep_alive=cast(str | None, ollama_keep_alive),
        history_window=_parse_history_window(history_window),
        recordings=cast(str, recordings),
        profile=cast(str | None, profile),
    )


def load_dotenv_file(path: Path) -> dict[str, str]:
    """Load the supported subset of dotenv syntax from ``path``."""
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid dotenv syntax on line {line_number}")
        key, value = line.split("=", maxsplit=1)
        values[key.strip()] = value.strip()
    return values
