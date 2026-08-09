"""Find and focus visible titled windows on Windows.

This module keeps window discovery and focus control in one small boundary so
controller input can be directed to the intended foreground window.
"""

import ctypes
import time
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

_get_window_thread_process_id = _user32.GetWindowThreadProcessId
_get_window_thread_process_id.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_get_window_thread_process_id.restype = wintypes.DWORD

_attach_thread_input = _user32.AttachThreadInput
_attach_thread_input.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
_attach_thread_input.restype = wintypes.BOOL

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_get_current_thread_id = _kernel32.GetCurrentThreadId
_get_current_thread_id.argtypes = []
_get_current_thread_id.restype = wintypes.DWORD

_SW_RESTORE = 9


def _raise_via_attached_input(hwnd: int) -> None:
    """Ask again while sharing the foreground window's input queue.

    Windows enforces a foreground lock: a process that is not currently in the foreground
    is generally refused when it asks to raise a window, which is what stops background
    apps stealing focus mid-sentence. That rule bites us the moment we hand focus to the
    game, because we then cannot hand it back.

    Attaching to the foreground thread's input queue makes the two threads share focus
    state for the duration, so the request is honoured. Detaching again is not optional --
    staying attached couples our input processing to another application's, and if that
    application hangs, so do we.
    """
    foreground = _get_foreground_window()
    if not foreground:
        return
    other = _get_window_thread_process_id(foreground, None)
    ours = _get_current_thread_id()
    if other == 0 or other == ours:
        return
    if not _attach_thread_input(ours, other, True):
        return
    try:
        _show_window(hwnd, _SW_RESTORE)
        _set_foreground_window(hwnd)
    finally:
        _attach_thread_input(ours, other, False)


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


def focus_window(hwnd: int, timeout_s: float = 0.5) -> bool:
    """Restore and focus a valid window, returning whether focus really changed.

    ``SetForegroundWindow`` can report success even when Windows refuses the foreground
    change, so its return value is not trusted; the foreground handle is read back instead.
    Otherwise a caller sends controller input into whatever window actually has focus.

    But the change is ASYNCHRONOUS. Reading the foreground handle immediately catches the
    outgoing window and reports failure for a change that is about to succeed -- which is
    worse than not checking at all, because a false alarm on every command trains the
    operator to ignore the one that matters. So the result is polled until it lands or the
    timeout expires.
    """
    if hwnd <= 0 or not _is_window(hwnd):
        return False
    if _is_iconic(hwnd):
        _show_window(hwnd, _SW_RESTORE)
    _set_foreground_window(hwnd)

    deadline = time.perf_counter() + max(0.0, timeout_s)
    retried = False
    while True:
        if current_foreground() == hwnd:
            return True
        if not retried:
            # The plain request is refused whenever we are not already the foreground
            # process -- which is exactly the case when handing focus back after driving
            # the game. Try once more sharing the foreground thread's input queue.
            retried = True
            _raise_via_attached_input(hwnd)
            continue
        if time.perf_counter() >= deadline:
            return False
        time.sleep(0.01)


def current_foreground() -> int:
    """Return the current foreground window handle, or zero when there is none."""
    return int(_get_foreground_window() or 0)
