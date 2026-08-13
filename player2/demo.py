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
import math
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

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


def capture(profile_path: str | None, count: int, out_dir: str, delay: float) -> int:
    """Capture frames from the game window and write them to disk.

    The first thing the runtime has ever done with eyes. Saving them is not the point --
    proving that frames arrive with monotonic sequence numbers, plausible timestamps, and
    an accounting of everything that did NOT arrive is the point, because those are the
    properties a demonstration dataset lives or dies on.
    """
    from player2.clock import SessionClock as _Clock
    from player2.video.base import CaptureError
    from player2.video.wgc import WindowsGraphicsCapture

    needle = None
    if profile_path:
        needle = load_profile(Path(profile_path)).window_title_contains
    target = _resolve_window(needle)
    if target is None:
        print("no window to capture; pass --profile with window_title_contains")
        return 1

    for remaining in range(int(delay), 0, -1):
        print(f"  capturing in {remaining}...", flush=True)
        time.sleep(1.0)

    clock = _Clock()
    source = WindowsGraphicsCapture(clock=clock, hwnd=target.hwnd)
    try:
        source.start()
    except CaptureError as error:
        print(f"could not capture: {error}")
        return 1
    try:
        deadline = time.time() + 10.0
        while time.time() < deadline and source.stats.frames_captured < count:
            time.sleep(0.05)
        frames = source.latest(count)
    finally:
        source.stop()

    stats = source.stats
    print(f"captured={stats.frames_captured} dropped={stats.frames_dropped} "
          f"errored={stats.frames_errored}")
    if not frames:
        print("no frames arrived")
        return 1

    previous: float | None = None
    for f in frames:
        gap = "" if previous is None else f"  (+{f.session_ms - previous:.1f}ms)"
        previous = f.session_ms
        src = "none" if f.source_ms is None else f"{f.source_ms:.1f}"
        print(f"  seq={f.seq:<4} {f.width}x{f.height} session={f.session_ms:8.1f} "
              f"source={src:>9}{gap}")

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    try:
        import cv2

        for f in frames:
            path = directory / f"frame_{f.seq:04d}.png"
            # Frame.data is deliberately opaque -- contracts.py must not know about numpy --
            # so the concrete buffer type is only known here, at the point of use.
            pixels: Any = f.data
            cv2.imwrite(str(path), cv2.cvtColor(pixels, cv2.COLOR_BGRA2BGR))
        print(f"wrote {len(frames)} images to {directory}")
    except Exception as error:  # noqa: BLE001 - image writing is a convenience, not the test
        print(f"(could not write images: {error})")
    return 0


def spiral_chunks(segments: int = 28, base_ms: float = 700.0,
                  growth_ms: float = 55.0) -> list[ActionChunk]:
    """Walk an expanding spiral: heading rotates, each leg a little longer than the last.

    Deliberately not a square. A square retraces the same few headings and revisits ground
    it has already seen, which produces a recording full of near-duplicate frames -- the
    exact thing that made the earlier sessions collapse to a handful of unique images. A
    spiral keeps the camera moving over new terrain, so the footage is actually worth
    something as demonstration data.
    """
    chunks = []
    for i in range(segments):
        angle = i * (math.tau / 8.0)  # eight headings per revolution
        vector = (round(math.cos(angle), 3), round(math.sin(angle), 3))
        duration = base_ms + i * growth_ms
        chunks.append(ActionChunk(keyframes=(
            Keyframe(t_ms=0.0, left_stick=vector),
            Keyframe(t_ms=duration, left_stick=vector),
        )))
    return chunks


_SDK_BASE_INTERVAL_MS = 1500.0
_SDK_FRAMES_PER_OBSERVATION = 1


def _build_sdk_policy(
    provider: str, model: str | None, budget_tpm: float, clock: object, agent_notes: str | None,
) -> tuple[object, object, str | None]:
    """Build a governed SDKPolicy for the requested provider.

    Every one of these transports still carries its own module docstring's caveat: real
    behavior against a live API has not been verified by this project yet (see
    docs/DEV-PROCESS.md Units 7 and 8). This is the first place that caveat becomes reachable
    from the command line rather than only from a test's fake client.
    """
    from player2.agent.budget import BudgetGovernor, GovernedTransport
    from player2.agent.model_policy import IModelTransport, SDKPolicy

    transport: IModelTransport
    if provider == "anthropic":
        from player2.agent.anthropic_transport import AnthropicTransport
        transport = AnthropicTransport(model=model) if model else AnthropicTransport()
    elif provider == "openai":
        from player2.agent.openai_transport import OpenAITransport
        transport = OpenAITransport(model=model) if model else OpenAITransport()
    elif provider == "ollama":
        from player2.agent.ollama_transport import OllamaTransport
        transport = OllamaTransport(model=model) if model else OllamaTransport()
    else:
        from player2.agent.cli_transport import CLITransport
        transport = CLITransport(model=model) if model else CLITransport()

    governor = BudgetGovernor(
        tokens_per_minute_limit=budget_tpm,
        base_interval_ms=_SDK_BASE_INTERVAL_MS,
        base_frames_per_observation=_SDK_FRAMES_PER_OBSERVATION,
        clock=clock,  # type: ignore[arg-type]
    )
    governed = GovernedTransport(inner=transport, governor=governor)
    policy = SDKPolicy(transport=governed)
    goal = agent_notes if agent_notes else "Play the game shown in the frames."
    return policy, governor, goal


def agent(profile_path: str | None, seconds: float, delay: float, out_dir: str,
          pattern: str = "square", warmup: str | None = None, policy_kind: str = "scripted",
          provider: str = "anthropic", model: str | None = None,
          budget_tpm: float = 60_000.0) -> int:
    """The whole loop, end to end: see the game, decide, act, and record all of it.

    Capture -> policy -> action chunk -> scheduler -> virtual pad -> game, with every frame,
    every executed pad state, and every scheduler event written to a session directory on
    one clock. `policy_kind="scripted"` (the default) is a deterministic stand-in that needs
    no credentials; `policy_kind="sdk"` runs a real model behind the same seam, governed by a
    token-rate budget so it degrades cadence rather than running unbounded. Nothing else in
    this function's path differs between the two -- that is the point of the seam.
    """
    from player2.agent.fast_stub import ScriptedPolicy
    from player2.agent.loop import AgentLoop
    from player2.control.vigem import ViGEmXboxAdapter
    from player2.record.writer import RecordingOutput, SessionRecorder
    from player2.video.base import CaptureError
    from player2.video.wgc import WindowsGraphicsCapture

    profile = load_profile(Path(profile_path)) if profile_path else None
    target = _resolve_window(profile.window_title_contains if profile else None)
    if target is None:
        print("no game window found; pass --profile with window_title_contains")
        return 1

    for remaining in range(int(delay), 0, -1):
        print(f"  starting in {remaining}...", flush=True)
        time.sleep(1.0)

    clock = SessionClock()
    recorder = SessionRecorder(
        root=Path(out_dir),
        clock=clock,
        metadata={
            "profile": profile.name if profile else None,
            "window_title": target.title,
            "policy": policy_kind,
            "provider": provider if policy_kind == "sdk" else None,
        },
    )
    recorder.start()
    print(f"recording to {recorder.directory}")

    video = WindowsGraphicsCapture(clock=clock, hwnd=target.hwnd)
    try:
        video.start()
    except CaptureError as error:
        print(f"could not capture: {error}")
        recorder.stop()
        return 1

    # RecordingOutput wraps the real pad, so what gets recorded is what the device was
    # actually told to do -- not what the policy asked for. Those differ constantly.
    output = RecordingOutput(inner=ViGEmXboxAdapter(), recorder=recorder, clock=clock)
    scheduler = Scheduler(output=output, clock=clock, max_hold_ms=250.0)
    sched_thread = SchedulerThread(scheduler)

    governor = None
    if policy_kind == "sdk":
        print(f"SDK policy: provider={provider} budget={budget_tpm:g} tok/min "
              "-- UNVERIFIED against a live API by this project yet; watch the first "
              "few decisions closely (see docs/DEV-PROCESS.md Units 7-8)")
        try:
            sdk_policy, governor, goal = _build_sdk_policy(
                provider, model, budget_tpm, clock,
                profile.agent_notes if profile else None,
            )
        except ImportError as error:
            print(f"could not build the '{provider}' policy: {error}")
            recorder.stop()
            video.stop()
            return 1
        loop = AgentLoop(policy=sdk_policy, video=video, scheduler=scheduler,  # type: ignore[arg-type]
                         clock=clock, goal=goal, recorder=recorder,
                         min_interval_ms=_SDK_BASE_INTERVAL_MS,
                         frames_per_observation=_SDK_FRAMES_PER_OBSERVATION,
                         governor=governor)  # type: ignore[arg-type]
    elif pattern == "spiral":
        chunks = spiral_chunks()
        goal, interval = "walk an expanding spiral over new ground", 700.0
        loop = AgentLoop(policy=ScriptedPolicy(chunks), video=video, scheduler=scheduler,
                         clock=clock, goal=goal, recorder=recorder, min_interval_ms=interval)
    else:
        chunks = [
            ActionChunk(keyframes=(Keyframe(t_ms=0.0, left_stick=v), Keyframe(t_ms=800.0)))
            for v in DIRECTIONS.values()
        ]
        goal, interval = "walk in a square", 800.0
        loop = AgentLoop(policy=ScriptedPolicy(chunks), video=video, scheduler=scheduler,
                         clock=clock, goal=goal, recorder=recorder, min_interval_ms=interval)

    sched_thread.start()
    try:
        focus_window(target.hwnd)
        # Run any warm-up macro INSIDE this pad connection, before the loop starts. Doing it
        # as a separate one-shot command creates and destroys a controller, and a game that
        # pauses on controller disconnect will simply undo whatever the macro achieved.
        if warmup and profile is not None:
            time.sleep(0.4)
            if scheduler.submit(replace(profile.get_macro(warmup), decision_seq=1)):
                print(f"warm-up macro '{warmup}' submitted")
                time.sleep(profile.get_macro(warmup).duration_ms / 1000.0 + 0.5)
        loop.start()
        time.sleep(seconds)
    finally:
        loop.stop()
        sched_thread.stop()
        video.stop()
        recorder.stop()

    stats = loop.stats
    capture_stats = video.stats
    print(f"\npolicy:   proposed={stats.chunks_proposed} accepted={stats.chunks_accepted} "
          f"rejected={stats.chunks_rejected} errors={stats.policy_errors} "
          f"budget_skips={stats.budget_skips}")
    print(f"capture:  frames={capture_stats.frames_captured} "
          f"dropped={capture_stats.frames_dropped} errored={capture_stats.frames_errored}")
    print(f"events:   {stats.events_seen}")
    if governor is not None:
        budget_stats = governor.stats  # type: ignore[attr-defined]
        print(f"budget:   level={budget_stats.level.value} "
              f"tokens_in_window={budget_stats.tokens_in_window:g}")
    print(f"\nsession written to {recorder.directory}")
    print("  inspect with: python -m player2.demo replay --session " + str(recorder.directory))
    return 0


def replay(session_dir: str) -> int:
    """Read a recorded session back and show that it is actually aligned."""
    from player2.record.writer import load_session

    session = load_session(Path(session_dir))
    manifest = session.manifest
    print(f"schema v{manifest['schema_version']}  clean={manifest['clean_shutdown']}  "
          f"truncated={session.truncated}")
    print(f"metadata: {manifest.get('metadata')}")
    print(f"counts:   {manifest.get('counts')}")
    print(f"\nframes={len(session.frames)} pad={len(session.pad)} "
          f"chunks={len(session.chunks)} events={len(session.events)}")

    # The property that makes this a dataset rather than two log files: for any frame, what
    # was the controller doing at that instant?
    pad = sorted(session.pad, key=lambda row: float(row["session_ms"]))
    print("\nframe -> controller state at that moment:")
    for frame in session.frames[:8]:
        t = float(frame["session_ms"])
        preceding = [row for row in pad if float(row["session_ms"]) <= t]
        if not preceding:
            print(f"  frame seq={frame['seq']:<4} t={t:8.1f}ms  (no pad state yet)")
            continue
        state = preceding[-1]
        stick = state.get("left_stick", [0.0, 0.0])
        print(f"  frame seq={frame['seq']:<4} t={t:8.1f}ms  "
              f"left_stick=({stick[0]:+.2f},{stick[1]:+.2f})  "
              f"buttons={state.get('buttons', [])}")

    for event in session.events[:10]:
        print(f"  event {event['kind']:<12} @{float(event['session_ms']):8.1f}ms  "
              f"{event.get('detail', '')}")
    return 0


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
                        choices=["probe", "readback", "capture", "agent", "replay", "session",
                                 "hold", "square", "deadman", "macro"])
    parser.add_argument("--session", default=None, help="session directory for 'replay'")
    parser.add_argument("--frames", type=int, default=5, help="frames for the 'capture' command")
    parser.add_argument("--out", default="captures", help="output directory for 'capture'")
    parser.add_argument("--recordings", default="recordings",
                        help="session root directory for 'agent'")
    parser.add_argument("--pattern", choices=["square", "spiral"], default="square",
                        help="movement pattern for 'agent' when --policy scripted")
    parser.add_argument("--warmup", default=None,
                        help="macro to replay once before the loop starts, same pad session")
    parser.add_argument("--policy", choices=["scripted", "sdk"], default="scripted",
                        help="'scripted' needs no credentials; 'sdk' runs a real governed "
                             "model policy for 'agent'")
    parser.add_argument("--provider", choices=["anthropic", "openai", "cli", "ollama"],
                        default="anthropic", help="model transport for --policy sdk")
    parser.add_argument("--model", default=None,
                        help="model override for --policy sdk (provider-specific default "
                             "otherwise)")
    parser.add_argument("--budget-tpm", type=float, default=60_000.0,
                        help="tokens-per-minute cap for --policy sdk before it degrades "
                             "cadence, then stops proposing")
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
    if args.command == "capture":
        return capture(args.profile, args.frames, args.out, delay)
    if args.command == "agent":
        return agent(args.profile, args.seconds, delay, args.recordings, args.pattern,
                     args.warmup, args.policy, args.provider, args.model, args.budget_tpm)
    if args.command == "replay":
        if not args.session:
            parser.error("replay requires --session")
        return replay(args.session)
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
