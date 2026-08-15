"""An optional, always-on-top window that shows the most recent decision's live text.

Nothing in the core runtime imports this module -- same "opt-in, never a hard dependency"
discipline `player2.audio` already follows (see CLAUDE.md's invariant on voice). `demo.py`
imports it lazily, only when `--overlay` is passed, so a Python build without Tkinter (rare on
Windows, but not guaranteed) never breaks anything that does not ask for this.

This module knows nothing about controllers, frames, or any game -- it displays whatever string
it is handed. That string comes from a model's own `commentary`, so this stays on the model's
side of the "no game-specific knowledge in the runtime" line the same way the text itself does.
"""

from __future__ import annotations

import ctypes
import queue
import tkinter as tk
from tkinter import font as tkfont

_DEFAULT_WIDTH = 420
_DEFAULT_HEIGHT = 110
_SCREEN_MARGIN = 24
_POLL_MS = 100
_PLACEHOLDER_TEXT = "(waiting for the first decision...)"


def _declare_dpi_aware() -> None:
    """Ask Windows for real, unscaled pixel coordinates before any window exists.

    Without this, Windows silently virtualizes coordinates for a DPI-unaware process on a
    multi-monitor setup where monitors run different scaling factors -- exactly the
    "the overlay lands in a different random spot every time" symptom this exists to fix.
    Best-effort: `shcore` (per-monitor DPI, Windows 8.1+) is tried first, falling back to the
    older, coarser `user32` system-DPI call; either failing (very old Windows, or running under
    something that already declared awareness) is not fatal to opening the window at all.
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


class LiveOverlay:
    """A small, borderless, always-on-top window showing the latest line of live text.

    Thread safety: `update()` is the only method meant to be called from a different thread
    than the rest (the `AgentLoop` worker that invokes `on_decision`) -- it only ever pushes
    onto a thread-safe queue. Every actual Tkinter call happens on whichever thread calls
    `start`/`run_for`, which must be the same thread for the whole window's lifetime -- that is
    Tkinter's own requirement, not something this class adds.
    """

    def __init__(
        self, *, title: str = "player2", width: int = _DEFAULT_WIDTH, height: int = _DEFAULT_HEIGHT,
    ) -> None:
        self._title = title
        self._width = width
        self._height = height
        self._queue: queue.Queue[str] = queue.Queue()
        self._root: tk.Tk | None = None
        self._label: tk.Label | None = None
        self._drag_offset = (0, 0)

    def start(self) -> None:
        """Create the window. Must be called from the thread that will later call `run_for`."""
        _declare_dpi_aware()
        root = tk.Tk()
        root.title(self._title)
        root.attributes("-topmost", True)
        root.overrideredirect(True)  # borderless: a caption bar/taskbar entry would be one
        # more thing stealing attention from the game this window sits on top of.
        screen_width = root.winfo_screenwidth()
        x = max(0, screen_width - self._width - _SCREEN_MARGIN)
        root.geometry(f"{self._width}x{self._height}+{x}+{_SCREEN_MARGIN}")
        # overrideredirect(True) removes the OS window frame entirely, which otherwise would
        # have given this some edge against a busy, similarly-dark game background -- a thin
        # highlight border stands in for it without adding a caption bar back.
        root.configure(bg="#111318", highlightthickness=1, highlightbackground="#3a3f4b",
                       highlightcolor="#3a3f4b")
        label = tk.Label(
            root, text=_PLACEHOLDER_TEXT, fg="#e8e8ea", bg="#111318",
            font=tkfont.Font(family="Segoe UI", size=11),
            wraplength=self._width - 24, justify="left", anchor="nw", padx=12, pady=10,
        )
        label.pack(fill="both", expand=True)
        # Undecorated windows have no OS-provided title bar to drag by either -- bound to the
        # label (which covers almost the whole surface) and the root itself, so a click
        # anywhere except the close button below moves the window. winfo_pointerx/y (global
        # pointer position) rather than the motion event's own x/y avoids the drift a naive
        # delta-of-deltas approach accumulates over a long drag.
        for widget in (root, label):
            widget.bind("<ButtonPress-1>", self._on_drag_start)
            widget.bind("<B1-Motion>", self._on_drag_motion)
        # overrideredirect(True) above removes every bit of OS-provided window chrome,
        # including any close button -- WM_DELETE_WINDOW below has nothing to bind to on a
        # borderless window in practice, so without this there is no mouse-reachable way to
        # close the overlay at all, despite the session telling the user they can.
        close_button = tk.Label(
            root, text="×", fg="#8a8f98", bg="#111318",
            font=tkfont.Font(family="Segoe UI", size=12, weight="bold"), cursor="hand2",
        )
        close_button.place(relx=1.0, x=-2, y=2, anchor="ne")
        close_button.bind("<Button-1>", lambda _event: root.destroy())
        root.protocol("WM_DELETE_WINDOW", root.destroy)
        self._root = root
        self._label = label

    def update(self, text: str) -> None:
        """Queue text for display. Safe to call from any thread."""
        self._queue.put_nowait(text)

    def _on_drag_start(self, event: tk.Event) -> None:
        """Remember where inside the window the drag began (widget-local coordinates)."""
        self._drag_offset = (event.x, event.y)

    def _on_drag_motion(self, event: tk.Event) -> None:
        """Follow the pointer, holding the original click point fixed within the window."""
        if self._root is None:
            return
        x = self._root.winfo_pointerx() - self._drag_offset[0]
        y = self._root.winfo_pointery() - self._drag_offset[1]
        self._root.geometry(f"+{x}+{y}")

    def run_for(self, seconds: float) -> None:
        """Run the window's event loop for up to `seconds`, or until closed early.

        `mainloop()` returns as soon as its last window is destroyed, whether that destruction
        came from the timer below or from the user closing the window -- either way this
        returns promptly rather than hanging, so a caller using this in place of
        `time.sleep(seconds)` cannot get stuck past the session it belongs to.
        """
        if self._root is None:
            self.start()
        assert self._root is not None
        self._root.after(_POLL_MS, self._poll)
        self._root.after(max(0, int(seconds * 1000)), self._root.destroy)
        self._root.mainloop()

    def stop(self) -> None:
        """Close the window early. Idempotent -- a second call after the window is already
        gone (e.g. the user closed it, or the timer in `run_for` already fired) is a no-op
        rather than an error, matching the rest of this project's shutdown-path convention."""
        if self._root is None:
            return
        try:
            self._root.destroy()
        except tk.TclError:
            pass

    def _poll(self) -> None:
        """Drain the queue and show only the newest entry -- this is a live status line, not
        a scrollback, so anything older than the latest update is intentionally discarded."""
        if self._root is None or self._label is None:
            return
        latest: str | None = None
        while True:
            try:
                latest = self._queue.get_nowait()
            except queue.Empty:
                break
        if latest is not None:
            self._label.configure(text=latest)
        self._root.after(_POLL_MS, self._poll)
