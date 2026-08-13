"""Load immutable, game-agnostic facts and action macros from TOML profiles."""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from player2.contracts import ActionChunk, InvalidChunk, chunk_from_dict


class InvalidProfile(ValueError):
    """Report a profile that cannot be trusted as runtime configuration."""


@dataclass(frozen=True)
class Profile:
    """Represent validated facts and immutable macros for one configured target.

    A copied, read-only macro mapping prevents a caller from changing actions after
    validation, which would otherwise turn load-time safety checks into a false promise.
    `agent_notes` is free-text game facts (control bindings, terminology) forwarded into a
    model's prompt verbatim -- the runtime never parses or acts on it, only transports it,
    same as every other field here.
    """

    name: str
    macros: Mapping[str, ActionChunk]
    wrap_macro: str | None = None
    window_title_contains: str | None = None
    text_deny_patterns: tuple[str, ...] = ()
    agent_notes: str | None = None

    def __post_init__(self) -> None:
        """Detach the profile from caller-owned mappings while retaining value equality."""
        object.__setattr__(self, "macros", MappingProxyType(dict(self.macros)))

    def get_macro(self, name: str) -> ActionChunk:
        """Return a named macro, listing available names when lookup fails."""
        try:
            return self.macros[name]
        except KeyError:
            available = ", ".join(sorted(self.macros)) or "(none)"
            raise KeyError(
                f"unknown macro {name!r}; available macros: {available}"
            ) from None


def _string_field(value: Any, key: str) -> str | None:
    """Read an optional string field so malformed configuration fails immediately."""
    if value is not None and not isinstance(value, str):
        raise InvalidProfile(f"{key} must be a string")
    return value


def _read_patterns(value: Any) -> tuple[str, ...]:
    """Validate and compile deny patterns before they can become an inert safety control."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InvalidProfile("text_deny_patterns must be a list of strings")
    patterns = tuple(value)
    try:
        for pattern in patterns:
            re.compile(pattern)
    except re.error as error:
        raise InvalidProfile(f"invalid deny pattern: {error}") from error
    return patterns


def _profile_option(
    data: dict[str, Any], macros: dict[str, Any], key: str, default: Any = None
) -> Any:
    """Accept profile options after a macro table, as used by the authored TOML contract."""
    if key in data:
        return data[key]
    for payload in macros.values():
        if isinstance(payload, dict) and key in payload:
            return payload[key]
    return default


def load_profile(path: str | os.PathLike[str]) -> Profile:
    """Load and strictly validate a TOML profile before exposing it to the runtime.

    Strict loading is intentional: a malformed hand-authored safety rule or macro
    must stop startup rather than silently disabling protection or failing mid-session.
    Macros go directly through the runtime's own chunk codec so file-defined actions
    obey exactly the same validation and hardening as scheduled actions.
    """
    try:
        with open(path, "rb") as profile_file:
            data = tomllib.load(profile_file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise InvalidProfile(f"could not load profile: {error}") from error

    if not isinstance(data, dict):
        raise InvalidProfile("profile must be a TOML table")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InvalidProfile("name must be a non-blank string")

    raw_macros = data.get("macros", {})
    if not isinstance(raw_macros, dict):
        raise InvalidProfile("macros must be a table")
    macros: dict[str, ActionChunk] = {}
    for macro_name, payload in raw_macros.items():
        try:
            macros[macro_name] = chunk_from_dict(payload)
        except InvalidChunk as error:
            raise InvalidProfile(str(error)) from error

    wrap_macro = _string_field(_profile_option(data, raw_macros, "wrap_macro"), "wrap_macro")
    if wrap_macro is not None and wrap_macro not in macros:
        raise InvalidProfile(f"wrap_macro {wrap_macro!r} does not name a macro")
    window_title = _string_field(
        _profile_option(data, raw_macros, "window_title_contains"),
        "window_title_contains",
    )
    patterns = _read_patterns(_profile_option(data, raw_macros, "text_deny_patterns", []))
    agent_notes = _string_field(_profile_option(data, raw_macros, "agent_notes"), "agent_notes")
    return Profile(name, macros, wrap_macro, window_title, patterns, agent_notes)
