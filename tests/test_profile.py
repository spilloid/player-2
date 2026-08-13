"""Contract for player2.profile -- where game-specific facts are allowed to live.

The runtime may not know that Factorio pauses on RT+START, that its window is called
"Factorio", or that `/c` in its chat box executes arbitrary Lua. All of that is real,
necessary knowledge and all of it belongs in a text file the runtime merely transports.

A profile is deliberately not a new mechanism. A macro is just a serialized ActionChunk,
so `chunk_from_dict` already parses and validates it -- which means a malformed profile is
rejected at load with the same rules that protect the scheduler at runtime.

The test that matters most is the last one: nothing in player2/ outside the profile loader
may mention a game by name.
"""

from pathlib import Path

import pytest

from player2.contracts import ActionChunk, Button
from player2.profile import InvalidProfile, Profile, load_profile

FACTORIO = Path(__file__).resolve().parents[1] / "profiles" / "factorio.toml"


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "p.toml"
    path.write_text(text, encoding="utf-8")
    return path


MINIMAL = """
name = "demo"

[macros.wave]
keyframes = [
  { t_ms = 0, buttons = [] },
  { t_ms = 100, buttons = ["A"] },
]
"""


class TestLoading:
    def test_loads_a_minimal_profile(self, tmp_path: Path) -> None:
        p = load_profile(write(tmp_path, MINIMAL))
        assert isinstance(p, Profile)
        assert p.name == "demo"

    def test_macros_are_action_chunks(self, tmp_path: Path) -> None:
        p = load_profile(write(tmp_path, MINIMAL))
        assert isinstance(p.macros["wave"], ActionChunk)
        assert p.macros["wave"].keyframes[-1].buttons == frozenset({Button.A})

    def test_optional_fields_default_to_none_or_empty(self, tmp_path: Path) -> None:
        p = load_profile(write(tmp_path, MINIMAL))
        assert p.wrap_macro is None
        assert p.window_title_contains is None
        assert p.text_deny_patterns == ()
        assert p.agent_notes is None

    def test_reads_optional_fields(self, tmp_path: Path) -> None:
        text = MINIMAL + """
wrap_macro = "wave"
window_title_contains = "Demo Game"
text_deny_patterns = ["^/", "^!"]
"""
        p = load_profile(write(tmp_path, text))
        assert p.wrap_macro == "wave"
        assert p.window_title_contains == "Demo Game"
        assert p.text_deny_patterns == ("^/", "^!")

    def test_reads_agent_notes(self, tmp_path: Path) -> None:
        """agent_notes is where control-binding facts belong -- the runtime forwards this
        string into a model's prompt unread, exactly like every other profile field; it must
        never be interpreted, only transported. See TestRuntimeStaysGameAgnostic below."""
        text = MINIMAL + '\nagent_notes = "controller-x mines the targeted resource"\n'
        p = load_profile(write(tmp_path, text))
        assert p.agent_notes == "controller-x mines the targeted resource"

    def test_agent_notes_must_be_a_string(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidProfile):
            load_profile(write(tmp_path, MINIMAL + "\nagent_notes = 5\n"))

    def test_missing_file_is_an_invalid_profile(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidProfile):
            load_profile(tmp_path / "nope.toml")

    def test_malformed_toml_is_an_invalid_profile(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidProfile):
            load_profile(write(tmp_path, "name = = ="))


class TestValidation:
    """A profile is authored by hand, so it is exactly the kind of file that rots quietly.
    Every failure must be loud at load time, not at 3am mid-session."""

    def test_requires_a_name(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidProfile):
            load_profile(write(tmp_path, '[macros.wave]\nkeyframes = [{ t_ms = 0 }]\n'))

    def test_rejects_a_macro_that_is_not_a_valid_chunk(self, tmp_path: Path) -> None:
        """Reuses the runtime's own chunk validation, so a macro that would be rejected by
        the scheduler is rejected by the file that defines it."""
        bad = """
name = "demo"
[macros.oops]
keyframes = [
  { t_ms = 0, buttons = ["A"] },
  { t_ms = 5, buttons = [] },
]
"""
        with pytest.raises(InvalidProfile):
            load_profile(write(tmp_path, bad))

    def test_rejects_an_unknown_button(self, tmp_path: Path) -> None:
        bad = 'name = "demo"\n[macros.oops]\nkeyframes = [{ t_ms = 0, buttons = ["NOPE"] }]\n'
        with pytest.raises(InvalidProfile):
            load_profile(write(tmp_path, bad))

    def test_rejects_a_wrap_macro_that_does_not_exist(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidProfile):
            load_profile(write(tmp_path, MINIMAL + '\nwrap_macro = "nonexistent"\n'))

    def test_rejects_a_non_string_deny_pattern(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidProfile):
            load_profile(write(tmp_path, MINIMAL + "\ntext_deny_patterns = [5]\n"))

    def test_rejects_an_uncompilable_deny_pattern(self, tmp_path: Path) -> None:
        """A broken regex must fail at load, not silently deny nothing at runtime -- these
        patterns are a safety control, and one that quietly stops working is worse than
        one that was never there."""
        with pytest.raises(InvalidProfile):
            load_profile(write(tmp_path, MINIMAL + '\ntext_deny_patterns = ["([unclosed"]\n'))

    def test_allows_a_profile_with_no_macros(self, tmp_path: Path) -> None:
        assert load_profile(write(tmp_path, 'name = "bare"\n')).macros == {}


class TestMacroLookup:
    def test_get_macro_returns_the_chunk(self, tmp_path: Path) -> None:
        p = load_profile(write(tmp_path, MINIMAL))
        assert p.get_macro("wave") is p.macros["wave"]

    def test_get_macro_raises_a_clear_error_for_an_unknown_name(self, tmp_path: Path) -> None:
        p = load_profile(write(tmp_path, MINIMAL))
        with pytest.raises(KeyError) as exc:
            p.get_macro("nope")
        assert "wave" in str(exc.value), "the error should list what IS available"


class TestFactorioProfile:
    """The shipped profile is real config, so it is worth testing like config."""

    def test_exists_and_loads(self) -> None:
        assert FACTORIO.exists(), f"missing {FACTORIO}"
        load_profile(FACTORIO)

    def test_defines_a_wrap_macro(self) -> None:
        p = load_profile(FACTORIO)
        assert p.wrap_macro is not None
        assert p.wrap_macro in p.macros

    def test_wrap_macro_matches_factorios_own_binding(self) -> None:
        """Factorio's config.ini says pause-game-controller = righttrigger + start.
        That fact lives here and nowhere else in the project."""
        p = load_profile(FACTORIO)
        chunk = p.get_macro(p.wrap_macro or "")
        held = [kf for kf in chunk.keyframes
                if kf.buttons and Button.START in kf.buttons]
        assert held, "expected START to be held"
        assert any(kf.right_trigger == 1.0 for kf in held), "expected RT held with START"

    def test_denies_the_console_prefix(self) -> None:
        """Factorio's chat box is also its Lua console."""
        assert "^/" in load_profile(FACTORIO).text_deny_patterns

    def test_agent_notes_document_the_real_default_bindings(self) -> None:
        """These strings are read from config.ini's DEFAULT controller bindings (Factorio ships
        them commented-out but active), not guessed -- see PLATFORM-NOTES.md #4. A model with
        no notion of which button mines has no way to act on any goal beyond walking around."""
        notes = load_profile(FACTORIO).agent_notes
        assert notes is not None
        assert "mine" in notes.lower() and "X" in notes
        assert "left stick" in notes.lower() or "left_stick" in notes.lower()


class TestRuntimeStaysGameAgnostic:
    def test_no_game_names_appear_in_the_runtime(self) -> None:
        """The load-bearing test of the whole architecture. If a game's name shows up in
        player2/ outside the profile loader, some piece of game knowledge has leaked out
        of config and into code, and the runtime has quietly become a collection of bots."""
        root = Path(__file__).resolve().parents[1] / "player2"
        banned = ("factorio", "baldur", "minecraft", "skyrim", "call of duty")
        offenders = []
        for path in root.rglob("*.py"):
            lowered = path.read_text(encoding="utf-8").lower()
            for word in banned:
                if word in lowered:
                    offenders.append(f"{path.name}: {word}")
        assert not offenders, f"game-specific knowledge leaked into the runtime: {offenders}"
