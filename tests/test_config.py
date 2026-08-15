"""Contract for player2.config -- resolving runtime knobs from CLI, env vars, and .env files.

This is the spec Codex/Terra's design consult was implemented against (docs/DEV-PROCESS.md,
the config-layer unit). The design constraint that shapes every test here: resolution is a pure
function over explicit mappings (cli / environ / dotenv), never a reach into real os.environ or
the filesystem. That's what lets `player2.demo.agent()` and friends keep working unchanged when
called directly with explicit kwargs (existing scripts and tests), and what keeps these tests
from being order-dependent on whatever happens to be in the real environment.

Precedence, everywhere: explicit CLI value > non-empty PLAYER2_* env var > non-empty .env value >
hardcoded default. Same "absent is not empty" split this project already uses for pad state
(contracts.py) applies here: a field missing from the `cli` mapping means "not passed on the
command line," not "passed as empty" -- argparse.SUPPRESS is what makes that distinction possible
upstream, so these tests simulate it by simply omitting keys rather than passing None/"".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from player2.config import RuntimeConfig, load_dotenv_file, resolve_runtime_config


class TestDefaults:
    def test_no_cli_no_env_no_dotenv_uses_hardcoded_defaults(self) -> None:
        config = resolve_runtime_config(cli={}, environ={}, dotenv={})
        assert config == RuntimeConfig(
            provider="anthropic",
            model=None,
            budget_tpm=60_000.0,
            ollama_base_url=None,
            ollama_keep_alive=None,
            history_window=None,
            recordings="recordings",
            profile=None,
            commentary_required=False,
        )

    def test_result_is_a_frozen_dataclass(self) -> None:
        config = resolve_runtime_config(cli={}, environ={}, dotenv={})
        with pytest.raises(AttributeError):
            config.provider = "openai"  # type: ignore[misc]


class TestPrecedence:
    """Each field independently obeys CLI > env > dotenv > default -- proven per-field so a
    field that accidentally shares resolution logic with its neighbour cannot hide a bug."""

    def test_env_overrides_dotenv(self) -> None:
        config = resolve_runtime_config(
            cli={}, environ={"PLAYER2_PROVIDER": "openai"}, dotenv={"PLAYER2_PROVIDER": "cli"},
        )
        assert config.provider == "openai"

    def test_dotenv_overrides_hardcoded_default(self) -> None:
        config = resolve_runtime_config(cli={}, environ={}, dotenv={"PLAYER2_PROVIDER": "ollama"})
        assert config.provider == "ollama"

    def test_cli_overrides_env_and_dotenv(self) -> None:
        config = resolve_runtime_config(
            cli={"provider": "cli"},
            environ={"PLAYER2_PROVIDER": "openai"},
            dotenv={"PLAYER2_PROVIDER": "ollama"},
        )
        assert config.provider == "cli"

    def test_precedence_holds_for_ollama_base_url(self) -> None:
        config = resolve_runtime_config(
            cli={},
            environ={"PLAYER2_OLLAMA_BASE_URL": "http://192.168.68.3:11434/v1"},
            dotenv={"PLAYER2_OLLAMA_BASE_URL": "http://ignored:11434/v1"},
        )
        assert config.ollama_base_url == "http://192.168.68.3:11434/v1"

    def test_precedence_holds_for_ollama_keep_alive(self) -> None:
        config = resolve_runtime_config(
            cli={},
            environ={"PLAYER2_OLLAMA_KEEP_ALIVE": "30m"},
            dotenv={"PLAYER2_OLLAMA_KEEP_ALIVE": "5m"},
        )
        assert config.ollama_keep_alive == "30m"

    def test_precedence_holds_for_budget_tpm_with_type_coercion(self) -> None:
        config = resolve_runtime_config(
            cli={}, environ={"PLAYER2_BUDGET_TPM": "12000"}, dotenv={},
        )
        assert config.budget_tpm == 12_000.0

    def test_precedence_holds_for_history_window_with_type_coercion(self) -> None:
        config = resolve_runtime_config(
            cli={},
            environ={"PLAYER2_HISTORY_WINDOW": "10"},
            dotenv={"PLAYER2_HISTORY_WINDOW": "5"},
        )
        assert config.history_window == 10

    @pytest.mark.parametrize("raw,expected", [
        ("1", True), ("true", True), ("True", True), ("yes", True), ("on", True),
        ("0", False), ("false", False), ("False", False), ("no", False), ("off", False),
    ])
    def test_precedence_holds_for_commentary_required_with_type_coercion(
        self, raw: str, expected: bool,
    ) -> None:
        config = resolve_runtime_config(cli={}, environ={"PLAYER2_COMMENTARY": raw}, dotenv={})
        assert config.commentary_required is expected

    def test_cli_flag_overrides_env_for_commentary_required(self) -> None:
        config = resolve_runtime_config(
            cli={"commentary_required": True},
            environ={"PLAYER2_COMMENTARY": "false"},
            dotenv={},
        )
        assert config.commentary_required is True

    def test_precedence_holds_for_model_recordings_and_profile(self) -> None:
        config = resolve_runtime_config(
            cli={},
            environ={
                "PLAYER2_MODEL": "llava",
                "PLAYER2_RECORDINGS": "/mnt/recordings",
                "PLAYER2_PROFILE": "profiles/factorio.toml",
            },
            dotenv={},
        )
        assert config.model == "llava"
        assert config.recordings == "/mnt/recordings"
        assert config.profile == "profiles/factorio.toml"


class TestAbsentIsNotEmpty:
    """The defect class this project has hit before (contracts.py's buttons=None vs
    buttons=frozenset()): a field explicitly set to the empty string must not silently beat a
    populated value from a lower-precedence source, and must not silently become a live but
    broken config (e.g. an empty ollama_base_url that no client would ever construct from)."""

    def test_empty_env_var_does_not_override_populated_dotenv(self) -> None:
        config = resolve_runtime_config(
            cli={}, environ={"PLAYER2_PROVIDER": ""}, dotenv={"PLAYER2_PROVIDER": "ollama"},
        )
        assert config.provider == "ollama"

    def test_empty_dotenv_value_does_not_override_hardcoded_default(self) -> None:
        config = resolve_runtime_config(cli={}, environ={}, dotenv={"PLAYER2_MODEL": ""})
        assert config.model is None

    def test_empty_env_var_falls_through_to_hardcoded_default(self) -> None:
        config = resolve_runtime_config(cli={}, environ={"PLAYER2_OLLAMA_BASE_URL": ""}, dotenv={})
        assert config.ollama_base_url is None

    def test_empty_keep_alive_env_var_falls_through_to_hardcoded_default(self) -> None:
        config = resolve_runtime_config(
            cli={}, environ={"PLAYER2_OLLAMA_KEEP_ALIVE": ""}, dotenv={},
        )
        assert config.ollama_keep_alive is None

    def test_empty_history_window_env_var_falls_through_to_hardcoded_default(self) -> None:
        config = resolve_runtime_config(
            cli={}, environ={"PLAYER2_HISTORY_WINDOW": ""}, dotenv={},
        )
        assert config.history_window is None

    def test_cli_key_present_with_falsy_value_still_wins(self) -> None:
        """Presence in the cli mapping decides, not truthiness -- a real --budget-tpm 0 must
        not be mistaken for "not passed" just because 0 is falsy in Python."""
        config = resolve_runtime_config(
            cli={"budget_tpm": 0.0}, environ={"PLAYER2_BUDGET_TPM": "60000"}, dotenv={},
        )
        assert config.budget_tpm == 0.0

    def test_cli_flag_present_as_false_still_wins_over_a_true_env_var(self) -> None:
        """The same presence-not-truthiness rule for a boolean CLI flag: argparse's
        `default=argparse.SUPPRESS` is what makes an explicit False distinguishable from
        "the flag was never passed" upstream, and this layer must honor that distinction
        rather than treating False as though the key were absent."""
        config = resolve_runtime_config(
            cli={"commentary_required": False},
            environ={"PLAYER2_COMMENTARY": "true"},
            dotenv={},
        )
        assert config.commentary_required is False

    def test_empty_commentary_env_var_falls_through_to_hardcoded_default(self) -> None:
        config = resolve_runtime_config(cli={}, environ={"PLAYER2_COMMENTARY": ""}, dotenv={})
        assert config.commentary_required is False


class TestValidation:
    def test_unknown_provider_from_env_raises_a_clear_error(self) -> None:
        with pytest.raises(ValueError, match="provider"):
            resolve_runtime_config(cli={}, environ={"PLAYER2_PROVIDER": "not-a-provider"},
                                    dotenv={})

    def test_non_numeric_budget_tpm_from_dotenv_raises_a_clear_error(self) -> None:
        with pytest.raises(ValueError, match="budget_tpm|BUDGET_TPM"):
            resolve_runtime_config(cli={}, environ={}, dotenv={"PLAYER2_BUDGET_TPM": "lots"})

    def test_non_numeric_history_window_from_dotenv_raises_a_clear_error(self) -> None:
        with pytest.raises(ValueError, match="history_window|HISTORY_WINDOW"):
            resolve_runtime_config(cli={}, environ={}, dotenv={"PLAYER2_HISTORY_WINDOW": "lots"})

    def test_unrecognized_commentary_required_value_raises_a_clear_error(self) -> None:
        with pytest.raises(ValueError, match="commentary|COMMENTARY"):
            resolve_runtime_config(cli={}, environ={"PLAYER2_COMMENTARY": "maybe"}, dotenv={})


class TestLoadDotenvFile:
    def test_missing_file_returns_empty_mapping(self, tmp_path: Path) -> None:
        assert load_dotenv_file(tmp_path / "does-not-exist.env") == {}

    def test_parses_key_value_pairs(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("PLAYER2_PROVIDER=ollama\nPLAYER2_MODEL=llava\n")
        assert load_dotenv_file(path) == {
            "PLAYER2_PROVIDER": "ollama", "PLAYER2_MODEL": "llava",
        }

    def test_ignores_blank_lines_and_whole_line_comments(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("\n# a comment\nPLAYER2_PROVIDER=ollama\n\n# trailing\n")
        assert load_dotenv_file(path) == {"PLAYER2_PROVIDER": "ollama"}

    def test_splits_only_on_the_first_equals_sign(self, tmp_path: Path) -> None:
        """A URL value legitimately contains '=' (query strings); a naive split(1) elsewhere
        in a hand-rolled parser is the easy way to corrupt exactly the kind of value this
        config layer exists to carry."""
        path = tmp_path / ".env"
        path.write_text("PLAYER2_OLLAMA_BASE_URL=http://host:11434/v1?x=1\n")
        assert load_dotenv_file(path) == {
            "PLAYER2_OLLAMA_BASE_URL": "http://host:11434/v1?x=1",
        }

    def test_trims_surrounding_whitespace_around_key_and_value(self, tmp_path: Path) -> None:
        path = tmp_path / ".env"
        path.write_text("  PLAYER2_PROVIDER = ollama  \n")
        assert load_dotenv_file(path) == {"PLAYER2_PROVIDER": "ollama"}

    def test_a_line_with_no_equals_sign_raises_a_clear_error(self, tmp_path: Path) -> None:
        """Unsupported dotenv syntax (export foo, quoting, multiline) must fail loudly rather
        than silently drop or mis-parse a line -- this parser deliberately does not implement
        full dotenv semantics, and pretending otherwise would be worse than refusing it."""
        path = tmp_path / ".env"
        path.write_text("PLAYER2_PROVIDER=ollama\nnot-a-valid-line\n")
        with pytest.raises(ValueError, match="line 2"):
            load_dotenv_file(path)
