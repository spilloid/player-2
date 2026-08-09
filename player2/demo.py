"""Drive the virtual controller from the command line.

This is a harness demo, not a game bot. It knows nothing about any particular game: it
submits action chunks describing stick positions over time, exactly as a model would.
That is the whole point -- if this can walk a character in a square, the motor path from
"an agent decided something" to "the game moved" is real.

(Naming a game here would fail tests/test_profile.py::TestRuntimeStaysGameAgnostic, which
greps player2/ for game names. That test is deliberately blunt: the day it starts getting
weakened to accommodate a "harmless" mention is the day the boundary starts eroding.)

Usage:
    python -m player2.demo probe     verify the virtual pad exists and Windows sees it
    python -m player2.demo square    walk a square, then release
    python -m player2.demo deadman   hold forward, then go silent to prove the pad releases
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

# Cardinal directions as left-stick positions. +y is up, matching XInput.
DIRECTIONS: dict[str, tuple[float, float]] = {
    "up": (0.0, 1.0),
    "right": (1.0, 0.0),
    "down": (0.0, -1.0),
    "left": (-1.0, 0.0),
}


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_uint32), ("Gamepad", ctypes.c_byte * 12)]


def probe() -> int:
    """Create the virtual pad and ask Windows, through XInput, whether it can see it.

    Worth doing separately: if the game never reacts, this distinguishes "the pad was
    never created" from "the game ignored a pad that exists".
    """
    from player2.control.vigem import ViGEmXboxAdapter

    pad = ViGEmXboxAdapter()
    time.sleep(0.5)  # the driver needs a moment to enumerate the new device
    try:
        xinput = None
        for name in ("XInput1_4", "XInput1_3", "XInput9_1_0"):
            try:
                xinput = ctypes.windll.LoadLibrary(name)
                break
            except OSError:
                continue
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
    parser.add_argument("command", choices=["probe", "square", "deadman", "macro"])
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
