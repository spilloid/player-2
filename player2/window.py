"""Find and focus visible titled windows on Windows.

This module keeps window discovery and focus control in one small boundary so
controller input can be directed to the intended foreground window.
"""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowInfo:
    """The handle and displayed title of a visible window."""

    hwnd: int
    title: str


_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32 = ctypes.WinDLL("user32", use_last_error=True)

_enum_windows = _user32.EnumWindows
_enum_windows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_enum_windows.restype = wintypes.BOOL

_is_window_visible = _user32.IsWindowVisible
_is_window_visible.argtypes = [wintypes.HWND]
_is_window_visible.restype = wintypes.BOOL

_get_window_text_length = _user32.GetWindowTextLengthW
_get_window_text_length.argtypes = [wintypes.HWND]
_get_window_text_length.restype = ctypes.c_int

_get_window_text = _user32.GetWindowTextW
_get_window_text.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_get_window_text.restype = ctypes.c_int

_is_window = _user32.IsWindow
_is_window.argtypes = [wintypes.HWND]
_is_window.restype = wintypes.BOOL

_is_iconic = _user32.IsIconic
_is_iconic.argtypes = [wintypes.HWND]
_is_iconic.restype = wintypes.BOOL

_show_window = _user32.ShowWindow
_show_window.argtypes = [wintypes.HWND, ctypes.c_int]
_show_window.restype = wintypes.BOOL

_set_foreground_window = _user32.SetForegroundWindow
_set_foreground_window.argtypes = [wintypes.HWND]
_set_foreground_window.restype = wintypes.BOOL

_get_foreground_window = _user32.GetForegroundWindow
_get_foreground_window.argtypes = []
_get_foreground_window.restype = wintypes.HWND

_SW_RESTORE = 9


def list_windows() -> list[WindowInfo]:
    """Return visible windows that have a non-empty, stripped title.

    The enumeration callback is retained locally until ``EnumWindows``
    returns, because ctypes may otherwise collect it while Windows is using it.
    """
    windows: list[WindowInfo] = []

    def collect(hwnd: wintypes.HWND, _lparam: wintypes.LPARAM) -> bool:
        """Collect one visible, titled window and continue enumeration."""
        if not _is_window_visible(hwnd):
            return True
        length = _get_window_text_length(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        _get_window_text(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        if title:
            windows.append(WindowInfo(int(hwnd), title))
        return True

    callback = _WNDENUMPROC(collect)
    _enum_windows(callback, 0)
    return windows


def find_window(title_contains: str) -> WindowInfo | None:
    """Find a title by case-insensitive exact match, then substring match."""
    needle = title_contains.strip()
    if not needle:
        raise ValueError("title_contains must not be empty")

    windows = list_windows()
    folded_needle = needle.casefold()
    for window in windows:
        if window.title.casefold() == folded_needle:
            return window
    for window in windows:
        if folded_needle in window.title.casefold():
            return window
    return None


def focus_window(hwnd: int) -> bool:
    """Restore and focus a valid window, returning whether focus really changed.

    ``SetForegroundWindow`` can report success even when Windows refuses the
    foreground change.  Reading the foreground handle back prevents callers
    from sending controller input to a different window based on a false
    success result.
    """
    if hwnd <= 0 or not _is_window(hwnd):
        return False
    if _is_iconic(hwnd):
        _show_window(hwnd, _SW_RESTORE)
    _set_foreground_window(hwnd)
    return current_foreground() == hwnd


def current_foreground() -> int:
    """Return the current foreground window handle, or zero when there is none."""
    return int(_get_foreground_window() or 0)
