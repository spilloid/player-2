"""Drive the virtual controller from the command line.

This is a harness demo, not a game bot. It knows nothing about any particular game: it
submits action chunks describing stick positions over time, exactly as a model would.
That is the whole point -- if this can walk a character in a square, the motor path from
"an agent decided something" to "the game moved" is real.

(Naming a game here would fail tests/test_profile.py::TestRuntimeStaysGameAgnostic, which
greps player2/ for game names. That test is deliberately blunt: the day it starts getting
weakened to accommodate a "harmless" mention is the day the boundary starts eroding.)

Usage:
    python -m player2.demo session    keep ONE pad plugged in and drive it interactively
    python -m player2.demo probe      verify the virtual pad exists and Windows sees it
    python -m player2.demo readback   drive the stick and read the pad's real state back

Prefer `session` for anything interactive. The one-shot commands create a virtual
controller and destroy it on exit, which the game sees as a device being hot-plugged and
yanked out -- and titles with console ports typically pause when the active controller
disappears, so the next command then runs against a paused game.
    python -m player2.demo square     walk a square, then release
    python -m player2.demo deadman    hold forward, then go silent to prove the pad releases
    python -m player2.demo macro --profile <toml> --name <macro>   replay one named macro
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from dataclasses import replace
from pathlib import Path

from player2.clock import SessionClock
from player2.contracts import ActionChunk, Keyframe
from player2.control.null import NullControllerAdapter
from player2.control.scheduler import Scheduler, SchedulerThread
from player2.profile import load_profile
from player2.window import WindowInfo, current_foreground, find_window, focus_window

# Cardinal directions as left-stick positions. +y is up, matching XInput.
DIRECTIONS: dict[str, tuple[float, float]] = {
    "up": (0.0, 1.0),
    "right": (1.0, 0.0),
    "down": (0.0, -1.0),
    "left": (-1.0, 0.0),
}


class _XInputGamepad(ctypes.Structure):
    _fields_ = [
        ("wButtons", ctypes.c_uint16),
        ("bLeftTrigger", ctypes.c_uint8),
        ("bRightTrigger", ctypes.c_uint8),
        ("sThumbLX", ctypes.c_int16),
        ("sThumbLY", ctypes.c_int16),
        ("sThumbRX", ctypes.c_int16),
        ("sThumbRY", ctypes.c_int16),
    ]


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint32), ("Gamepad", _XInputGamepad)]


def _load_xinput() -> ctypes.CDLL | None:
    for name in ("XInput1_4", "XInput1_3", "XInput9_1_0"):
        try:
            return ctypes.windll.LoadLibrary(name)
        except OSError:
            continue
    return None


def probe() -> int:
    """Create the virtual pad and ask Windows, through XInput, whether it can see it.

    Worth doing separately: if the game never reacts, this distinguishes "the pad was
    never created" from "the game ignored a pad that exists".
    """
    from player2.control.vigem import ViGEmXboxAdapter

    pad = ViGEmXboxAdapter()
    time.sleep(0.5)  # the driver needs a moment to enumerate the new device
    try:
        xinput = _load_xinput()
        if xinput is None:
            print("could not load any XInput DLL")
            return 1
        state = _XInputState()
        found = [i for i in range(4) if xinput.XInputGetState(i, ctypes.byref(state)) == 0]
        if found:
            print(f"virtual pad visible to XInput on slot(s): {found}")
            print("Windows sees a controller. Open joy.cpl to confirm visually.")
            return 0
        print("no XInput device found -- is ViGEmBus running?")
        return 1
    finally:
        # The device itself goes away when vgamepad is collected at process exit; all we
        # can do here is make sure it is not holding anything on the way out.
        pad.reset()


def _run(
    chunks: list[ActionChunk],
    *,
    dry_run: bool,
    delay: float = 0.0,
    wrap: ActionChunk | None = None,
) -> int:
    """Submit chunks in order, letting each play out before the next is offered.

    If `wrap` is given it is replayed once before and once after the sequence. This code
    does not know what it does -- in one profile it toggles pause, in another it might open
    a photo mode. Which macro to use, and what it contains, is the profile's business.
    """
    if dry_run:
        output: object = NullControllerAdapter()
    else:
        from player2.control.vigem import ViGEmXboxAdapter

        output = ViGEmXboxAdapter()

    if wrap is not None:
        chunks = [wrap, *chunks, wrap]

    # decision_seq must strictly increase or the scheduler rejects a chunk as stale, so the
    # caller's numbering is replaced with the actual submission order.
    chunks = [replace(chunk, decision_seq=i) for i, chunk in enumerate(chunks)]

    # The virtual pad reaches whichever window has focus, so give the human time to click
    # back into the game before anything starts moving.
    for remaining in range(int(delay), 0, -1):
        print(f"  focus the game... {remaining}", flush=True)
        time.sleep(1.0)

    scheduler = Scheduler(output=output, clock=SessionClock(), max_hold_ms=250.0)  # type: ignore[arg-type]
    thread = SchedulerThread(scheduler)
    thread.start()
    try:
        for chunk in chunks:
            if not scheduler.submit(chunk):
                print(f"chunk seq={chunk.decision_seq} REJECTED", file=sys.stderr)
                continue
            print(f"chunk seq={chunk.decision_seq} accepted ({chunk.duration_ms:.0f}ms)")
            # Re-submit before the previous chunk's deadman would fire, exactly as the
            # agent loop will: the pad only keeps moving because fresh intent keeps arriving.
            time.sleep(chunk.duration_ms / 1000.0)
        print("going silent -- the deadman should now release everything")
        time.sleep(0.6)
        for event in scheduler.drain_events():
            print(f"  event {event.kind} @{event.session_ms:.0f}ms  {event.detail}")
    finally:
        thread.stop()
        print("stopped; pad released")
    return 0


def readback() -> int:
    """Drive the stick and read the pad's real state back out of Windows.

    This exists to end an argument that would otherwise cost hours: when a game does not
    respond, is our pad failing to move, or is the game ignoring a pad that is moving fine?
    Asking XInput what the device is actually reporting answers that with evidence instead
    of theories, and needs no game running at all.
    """
    from player2.control.vigem import ViGEmXboxAdapter

    xinput = _load_xinput()
    if xinput is None:
        print("could not load any XInput DLL")
        return 1

    output = ViGEmXboxAdapter()
    time.sleep(0.5)
    scheduler = Scheduler(output=output, clock=SessionClock(), max_hold_ms=3000.0)
    thread = SchedulerThread(scheduler)
    thread.start()
    observed: list[tuple[int, int]] = []
    try:
        for seq, (label, vector) in enumerate(DIRECTIONS.items()):
            scheduler.submit(ActionChunk(
                keyframes=(
                    Keyframe(t_ms=0.0, left_stick=vector),
                    Keyframe(t_ms=600.0, left_stick=vector),
                ),
                decision_seq=seq,
            ))
            time.sleep(0.3)
            state = _XInputState()
            if xinput.XInputGetState(0, ctypes.byref(state)) != 0:
                print("  slot 0 reported no device")
                continue
            pad = state.Gamepad
            observed.append((pad.sThumbLX, pad.sThumbLY))
            print(f"  commanded {label:<5} {str(vector):<12} "
                  f"XInput reports LX={pad.sThumbLX:>7} LY={pad.sThumbLY:>7}")
            time.sleep(0.3)
    finally:
        thread.stop()

    moved = [pair for pair in observed if max(abs(pair[0]), abs(pair[1])) > 16000]
    if len(moved) == len(DIRECTIONS):
        print("\nAll four directions registered at full deflection.")
        print("The virtual pad is working. If a game does not respond, the game is either")
        print("paused, showing a menu that swallows movement, or not in controller mode.")
        return 0
    print(f"\nOnly {len(moved)}/{len(DIRECTIONS)} directions registered -- this IS our bug.")
    return 1


def hold(direction: str, seconds: float, dry_run: bool, delay: float = 0.0) -> int:
    """Hold exactly one direction, once. The least ambiguous test available.

    `square` chains four chunks and can only be reported as a whole; if one side does not
    land, there is no way to tell whether that side failed or the run started late. One
    direction at a time removes that guesswork entirely.
    """
    vector = DIRECTIONS[direction]
    ms = seconds * 1000.0
    chunk = ActionChunk(keyframes=(
        Keyframe(t_ms=0.0, left_stick=vector),
        Keyframe(t_ms=ms, left_stick=vector),
    ))
    print(f"holding {direction} {vector} for {seconds}s -- nothing else, no buttons")
    return _run([chunk], dry_run=dry_run, delay=delay)


SESSION_HELP = """commands:
  up | down | left | right [seconds]   hold one direction (default 2s)
  square [seconds]                     walk a square
  macro <name>                         replay a named macro from --profile
  macros                               list macros in the loaded profile
  window                               re-resolve the game window
  events                               print buffered scheduler events
  help | quit
"""

# Windows needs a moment to actually complete a foreground change before input lands in
# the newly raised window. Too short and the first part of a chunk goes to the terminal.
FOCUS_SETTLE_S = 0.25


def _resolve_window(needle: str | None) -> WindowInfo | None:
    if not needle:
        return None
    found = find_window(needle)
    if found is None:
        print(f"  no window matching '{needle}' -- is the game running?")
    else:
        print(f"  game window: '{found.title}' (hwnd {found.hwnd})")
    return found


def session(profile_path: str | None) -> int:
    """Keep ONE virtual pad plugged in and drive it interactively.

    Every one-shot command creates a virtual controller and destroys it on exit, which the
    game sees as a real device being hot-plugged and yanked out. Games log a fresh
    controller-connected instance for each run -- and titles with console ports typically
    pause when the active controller vanishes. Test after test then began against a paused
    game, which looked exactly like broken movement.

    The deeper point is that the agent's hands should outlive any single decision. The real
    runtime is long-lived so it gets this for free; a one-shot CLI does not.
    """
    from player2.control.vigem import ViGEmXboxAdapter

    profile = load_profile(Path(profile_path)) if profile_path else None
    output = ViGEmXboxAdapter()
    scheduler = Scheduler(output=output, clock=SessionClock(), max_hold_ms=250.0)
    thread = SchedulerThread(scheduler)
    thread.start()
    seq = 0
    print("virtual pad connected and STAYING connected until you quit.")
    game = _resolve_window(profile.window_title_contains if profile else None)
    if game is None:
        print("  no game window: you will have to alt-tab yourself before each command")
    print(SESSION_HELP)
    try:
        while True:
            try:
                raw = input("player2> ").strip().split()
            except EOFError:
                break
            if not raw:
                continue
            cmd, rest = raw[0].lower(), raw[1:]
            if cmd in {"quit", "exit", "q"}:
                break
            if cmd in {"help", "?"}:
                print(SESSION_HELP)
                continue
            if cmd == "events":
                for event in scheduler.drain_events():
                    print(f"  {event.kind} @{event.session_ms:.0f}ms  {event.detail}")
                continue
            if cmd == "macros":
                print("  " + (", ".join(sorted(profile.macros)) if profile else "no --profile"))
                continue
            if cmd == "window":
                game = _resolve_window(profile.window_title_contains if profile else None)
                continue

            pending: list[ActionChunk] = []
            if cmd in DIRECTIONS:
                secs = float(rest[0]) if rest else 2.0
                vector = DIRECTIONS[cmd]
                pending = [ActionChunk(keyframes=(
                    Keyframe(t_ms=0.0, left_stick=vector),
                    Keyframe(t_ms=secs * 1000.0, left_stick=vector),
                ))]
            elif cmd == "square":
                secs = float(rest[0]) if rest else 1.5
                pending = [ActionChunk(keyframes=(
                    Keyframe(t_ms=0.0, left_stick=v),
                    Keyframe(t_ms=secs * 1000.0, left_stick=v),
                )) for v in DIRECTIONS.values()]
            elif cmd == "macro":
                if profile is None or not rest:
                    print("  need --profile and a macro name")
                    continue
                try:
                    pending = [profile.get_macro(rest[0])]
                except KeyError as error:
                    print(f"  {error}")
                    continue
            else:
                print(f"  unknown command '{cmd}'")
                continue

            # Raise the game, run the sequence, then hand focus back so the next command
            # can be typed straight away. Without this the human has to alt-tab faster
            # than the first chunk, which is not a design so much as a reflex test.
            terminal = current_foreground()
            if game is not None:
                if focus_window(game.hwnd):
                    time.sleep(FOCUS_SETTLE_S)
                else:
                    print("  could not focus the game window; input may go elsewhere")
            try:
                for chunk in pending:
                    seq += 1
                    if scheduler.submit(replace(chunk, decision_seq=seq)):
                        time.sleep(chunk.duration_ms / 1000.0)
                    else:
                        print(f"  chunk seq={seq} REJECTED")
            finally:
                if game is not None and terminal:
                    focus_window(terminal)
    finally:
        thread.stop()
        print("pad released and disconnected")
    return 0


def _wrap_chunk(profile_path: str | None) -> ActionChunk | None:
    """Load the profile's nominated wrap macro, if a profile was supplied."""
    if not profile_path:
        return None
    profile = load_profile(Path(profile_path))
    if profile.wrap_macro is None:
        print(f"profile '{profile.name}' defines no wrap_macro; continuing unwrapped",
              file=sys.stderr)
        return None
    print(f"wrapping with macro '{profile.wrap_macro}' from profile '{profile.name}'")
    # A wrap macro is a TOGGLE, and the runtime is currently blind -- it has no way to see
    # what state the game is in, so it cannot know which way a toggle will flip. Get this
    # backwards and the "pause around the test" becomes "run the whole test paused", which
    # looks exactly like a broken controller. This goes away once capture lands and the
    # agent can see the screen; until then, say it out loud.
    print("  NOTE: this macro is a toggle and the runtime cannot see game state.")
    print("  It assumes the game is currently PAUSED. If it is running, this will pause it")
    print("  and the sequence will execute against a frozen game.")
    return profile.get_macro(profile.wrap_macro)


def square(seconds_per_side: float, dry_run: bool, delay: float = 0.0,
           wrap: ActionChunk | None = None) -> int:
    """Walk a square. Four sides, one chunk each, held for the full side."""
    ms = seconds_per_side * 1000.0
    chunks = [
        ActionChunk(
            keyframes=(
                Keyframe(t_ms=0.0, left_stick=vector),
                Keyframe(t_ms=ms, left_stick=vector),
            ),
            decision_seq=i,
        )
        for i, vector in enumerate(DIRECTIONS.values())
    ]
    return _run(chunks, dry_run=dry_run, delay=delay, wrap=wrap)


def deadman(dry_run: bool, delay: float = 0.0, wrap: ActionChunk | None = None) -> int:
    """Hold forward once, then stop talking. Proves silence means stop."""
    chunk = ActionChunk(
        keyframes=(
            Keyframe(t_ms=0.0, left_stick=(0.0, 1.0)),
            Keyframe(t_ms=500.0, left_stick=(0.0, 1.0)),
        )
    )
    return _run([chunk], dry_run=dry_run, delay=delay, wrap=wrap)


def macro(profile_path: str, name: str, dry_run: bool, delay: float = 0.0) -> int:
    """Replay one named macro from a profile. Useful for checking a binding in isolation."""
    profile = load_profile(Path(profile_path))
    print(f"replaying macro '{name}' from profile '{profile.name}'")
    return _run([profile.get_macro(name)], dry_run=dry_run, delay=delay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",
                        choices=["probe", "readback", "session", "hold", "square",
                                 "deadman", "macro"])
    parser.add_argument("--direction", choices=sorted(DIRECTIONS), default="right",
                        help="direction for the 'hold' command")
    parser.add_argument("--seconds", type=float, default=1.0, help="seconds per side")
    parser.add_argument("--dry-run", action="store_true",
                        help="use the null adapter; creates no virtual device")
    parser.add_argument("--delay", type=float, default=5.0,
                        help="seconds to wait before moving, so you can focus the game")
    parser.add_argument("--profile", default=None,
                        help="path to a game profile TOML under profiles/")
    parser.add_argument("--name", default=None, help="macro name, for the 'macro' command")
    parser.add_argument("--wrap", action="store_true",
                        help="replay the profile's wrap_macro before and after (needs --profile)")
    args = parser.parse_args(argv)

    delay = 0.0 if args.dry_run else args.delay
    if args.command == "probe":
        return probe()
    if args.command == "readback":
        return readback()
    if args.command == "session":
        return session(args.profile)
    if args.command == "hold":
        return hold(args.direction, args.seconds, args.dry_run, delay)
    if args.command == "macro":
        if not args.profile or not args.name:
            parser.error("macro requires --profile and --name")
        return macro(args.profile, args.name, args.dry_run, delay)

    wrap = _wrap_chunk(args.profile) if args.wrap else None
    if args.wrap and wrap is None and not args.profile:
        parser.error("--wrap requires --profile")
    if args.command == "square":
        return square(args.seconds, args.dry_run, delay, wrap)
    return deadman(args.dry_run, delay, wrap)


if __name__ == "__main__":
    raise SystemExit(main())
