# Platform notes

Hard-won facts about driving a real Windows game from outside it. Every entry here cost us
debugging time at least once, and several of them produce symptoms that look exactly like a
bug in our own code. Read this before concluding the runtime is broken.

The recurring theme: **when an integration fails, the fault is usually in a boundary you did
not know existed, and the other system's logs will tell you faster than your own will.**

---

## 1. The virtual controller's lifetime is a real device's lifetime

**The trap.** `vgamepad`'s virtual pad is destroyed when the owning process exits. To the
game, that is a physical controller being unplugged. Titles with console ports — Factorio 2.0
included — **pause when the active controller disconnects**, because console certification
requires it.

A one-shot CLI therefore hot-plugs a brand-new pad on every invocation. Factorio's log makes
it unmistakable:

```
instance: 0 ... connected / disconnected
instance: 1 ... connected / disconnected
instance: 2 ... connected / disconnected
```

**The symptoms it produces.** Two, and neither looks like the real cause:
- The game "pauses itself" at the end of a command that pressed **no buttons at all**.
- The *next* run's first action chunk appears to do nothing, because it is spent getting back
  into a running game.

**The rule.** The agent's hands must outlive any single decision. Use `demo session`, which
holds one pad open. The real runtime is long-lived and gets this for free.

**Why it matters beyond the demo.** A controller that vanishes between recordings breaks
exactly the continuity the demonstration dataset exists to capture. This would have quietly
poisoned training data months from now.

---

## 2. Controller input goes to the focused window

**The trap.** A virtual pad is a system-wide device. Whichever window has focus receives its
input. Driving a game from a terminal means the terminal must not be focused.

**The symptom.** A chunk that starts while the terminal still has focus simply goes nowhere —
indistinguishable from a movement bug. This manufactures phantom bugs and wastes review cycles
on code that was always correct.

**The rule.** `player2.window.focus_window()` raises the game before a sequence, and the
matching substring comes from the profile's `window_title_contains`, so the runtime never
learns a game's name. Never rely on a human alt-tabbing fast enough; that is a reflex test,
not a design.

---

## 3. `SetForegroundWindow` lies in two different directions

Two separate defects, both live, both worth understanding before touching this code.

**It completes asynchronously.** Reading `GetForegroundWindow()` immediately afterwards
returns the *outgoing* window, so a verification check reports failure for a change that is
about to succeed. Our first version printed `could not focus the game window` on every single
command while focusing perfectly. A false alarm on every command is worse than no check at
all — it trains the operator to ignore the one that matters. **Poll until the change lands.**

**It is refused by the foreground lock.** Windows only honours a foreground request from a
process that is already in the foreground. This is deliberate — it stops background apps
stealing focus mid-sentence — but it bites the instant we hand focus to the game, because we
then cannot hand it back. `SetForegroundWindow` may still *return success*.

The workaround is `AttachThreadInput`: briefly share the foreground thread's input queue so
the request is honoured, then detach. **Detaching is not optional.** Staying attached couples
our input processing to another application's, and if that application hangs, so do we.

**The rule.** Never trust `SetForegroundWindow`'s return value. Read the foreground handle
back, poll for it, and fall back to attached input once.

---

## 4. The game may ignore a controller it can plainly see

**The trap.** Factorio's `config.ini` has `input-method = keyboard-and-mouse | game-controller`.
In keyboard-and-mouse mode it detects the pad and logs the connection, but ignores stick input.

**The diagnostic that solved it.** Factorio's own log:

```
166.051 Game controller connected: instance: 0, Xbox 360 Controller
177.201 Game controller disconnected: instance: 0, Xbox 360 Controller
```

Eleven seconds apart — exactly one demo run. That single line eliminated the entire "our pad is
broken" branch instantly. **Read the other system's logs before touching your own code.**

**Setup.** `%APPDATA%\Factorio\config\config.ini`, set `input-method=game-controller`. Factorio
must be **closed** first — it rewrites the file on exit and will clobber the edit. A backup is
at `config.ini.player2-backup`. Using mouse or keyboard in-game switches the mode back on its
own.

Controller bindings live in the same file and are the authoritative source for a profile's
macros. Read them rather than guessing:

```
pause-game-controller = controller-righttrigger + controller-start
move-controller       = controller-left-stick
mine-controller       = controller-x
```

---

## 5. Elevation matters for keyboard/mouse, not for the controller

**Controller: unaffected.** ViGEm creates the device at the driver level, so any process can
read it via XInput regardless of elevation. An unelevated agent can drive an elevated game.

**Keyboard and mouse: affected.** Windows UIPI blocks synthetic `SendInput` from a
lower-integrity process into a higher-integrity window. If the game runs as administrator, the
agent must too. This will matter for the keyboard/mouse adapter and for typing into in-game
chat; it does not matter today.

---

## 6. Elevation also breaks the test suite

An elevated run creates `%TEMP%\pytest-of-<user>` owned by a different token. Every later
non-elevated run then dies with `PermissionError` **before a single test executes**, which
looks like catastrophic breakage and is not.

Fixed by pinning pytest's `basetemp` into the repo (`addopts = "--basetemp=.pytest-tmp"` in
`pyproject.toml`), which makes the suite hermetic.

---

## 7. Diagnostics

Reach for these before theorising. Each one eliminates a whole branch of the search.

| Command | Answers |
|---|---|
| `python -m player2.demo probe` | Does the virtual pad exist and can Windows see it? |
| `python -m player2.demo readback` | Is the pad *actually moving*? Drives each direction and reads the device state back through `XInputGetState`, **with no game running**. Correct output is ±32767 on the matching axis. |
| `python -m player2.demo hold --direction up --seconds 3` | Does one specific direction work, in isolation? |
| `python -m player2.demo session --profile <toml>` | Everything interactive. Keeps one pad open and focuses the game itself. |
| `%APPDATA%\Factorio\factorio-current.log` | What did the *game* see? Controller connects, disconnects, input method. |

`readback` exists specifically to settle "is our pad failing, or is the game ignoring a pad
that works?" — a question that costs hours to answer by guesswork and seconds by asking
Windows. It reported ±32767 on all four axes and ended that debate permanently.

---

## 8. Symptom → likely cause

| Symptom | Look at first |
|---|---|
| Nothing moves at all | Game in keyboard-and-mouse mode (§4); wrong window focused (§2) |
| Game pauses on its own after a command | Controller hot-unplug on process exit (§1) |
| First action of a run does nothing, rest work | Previous run left the game paused (§1) |
| "could not focus the game window" but focus works | Async foreground change (§3) |
| Focus goes to the game but never comes back | Foreground lock (§3) |
| Some directions work, one does not | Terrain or an open menu — confirm with `hold` (§7) |
| `PermissionError` before any test runs | Stale elevated temp dir (§6) |
| Buttons work but sticks do not | Input method (§4) — buttons and axes are handled separately |
