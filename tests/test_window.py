"""Contract for player2.window -- finding and focusing the window the game lives in.

Input from a virtual controller reaches whichever window currently has focus, so driving a
game from a terminal means the terminal must not be the focused window. Making the human
alt-tab fast enough is not a design.

This is also groundwork rather than a demo convenience: capture needs a window handle to
record the right surface, and the MCP control plane's attach_game() needs to resolve a
human-supplied name to one. The matching substring always comes from a profile, so this
module never learns any game's name.

These tests touch the real desktop, so they assert invariants rather than specific windows.
"""

import pytest

from player2.window import WindowInfo, current_foreground, find_window, focus_window, list_windows


class TestListWindows:
    def test_returns_windows(self) -> None:
        assert list_windows(), "expected at least one visible titled window"

    def test_entries_are_window_info(self) -> None:
        assert all(isinstance(w, WindowInfo) for w in list_windows())

    def test_titles_are_non_empty(self) -> None:
        """Untitled windows are tool windows and tray hosts, never a game surface."""
        assert all(w.title.strip() for w in list_windows())

    def test_handles_are_positive(self) -> None:
        assert all(w.hwnd > 0 for w in list_windows())

    def test_handles_are_unique(self) -> None:
        handles = [w.hwnd for w in list_windows()]
        assert len(handles) == len(set(handles))


class TestFindWindow:
    def test_returns_none_for_no_match(self) -> None:
        assert find_window("no-window-is-called-this-zzqqxx") is None

    def test_finds_a_window_by_substring_of_its_title(self) -> None:
        target = list_windows()[0]
        found = find_window(target.title[: max(4, len(target.title) // 2)])
        assert found is not None

    def test_matching_is_case_insensitive(self) -> None:
        """Profiles are hand-written, so 'factorio' must match a window titled 'Factorio'."""
        target = list_windows()[0]
        fragment = target.title[: max(4, len(target.title) // 2)]
        assert find_window(fragment.lower()) is not None
        assert find_window(fragment.upper()) is not None

    def test_empty_substring_is_rejected(self) -> None:
        """An empty needle would match the first arbitrary window on the desktop and
        silently drive input into it."""
        with pytest.raises(ValueError):
            find_window("")

    def test_whitespace_only_substring_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            find_window("   ")


class TestFocus:
    def test_current_foreground_is_a_handle(self) -> None:
        assert current_foreground() >= 0

    def test_focusing_an_invalid_handle_fails_without_raising(self) -> None:
        """Called on every command in the interactive loop; a stale handle after the game
        closes must degrade to 'could not focus', never take the process down."""
        assert focus_window(0) is False
        assert focus_window(-1) is False

    def test_focusing_a_real_window_reports_a_bool(self) -> None:
        result = focus_window(list_windows()[0].hwnd)
        assert isinstance(result, bool)
