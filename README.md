# Player 2

**[Read the project site →](https://spilloid.github.io/player-2/)**

A game-independent embodied AI teammate. It sees the screen, hears the game and you, speaks
back, remembers what it learns, and plays using an ordinary controller — the same input device
a human uses. No game APIs, no memory reading, no injection.

The test this project is built around:

> Could I launch a game the AI has never played, hand it Player 2, and say
> *"okay, I'll show you how this works"*?

## The hard constraint

**No game-specific knowledge in the runtime.** There is no `heal_party()`, no `seek_cover()`,
no `aim_at_enemy()`. The runtime only knows *"make the input device have these states at these
times."* Game understanding lives in the model.

The test for every component: *could this run unmodified in Factorio, Baldur's Gate 3, and an
unknown future game?* Screen capture, controller output, episodic memory, action scheduling —
yes, core runtime. Locating an enemy, choosing a healing spell, planning a factory — no,
those belong to the model.

Facts about a specific game live in **profiles** (`profiles/*.toml`) — which window to
capture, which button chord pauses it, which text patterns must never be typed into its chat
box. The runtime transports and validates them; it never interprets them. This is enforced by
a test that greps `player2/` for game names, and it has already caught three real leaks.

## Status

**Milestone 1 reached.** An agent process manipulated a real commercial game (Factorio 2.0.7)
through a standard controller abstraction, with action chunks authored the way a model will
author them.

| Subsystem | State |
|---|---|
| Time base + action-chunk contracts | Done, hardened |
| Motor system (scheduler, ViGEm pad) | Done, hardened, verified on real hardware |
| Profiles (game facts as config) | Done, pause chord verified in-game |
| Window find/focus | Done |
| Eyes (screen capture) | Done, hardened, verified against a live game |
| Recorder (synchronized demonstrations) | Done, hardened |
| Agent seam (Observation, policy, loop) | Done, hardened |
| Direct SDK transport (Anthropic/OpenAI/CLI/Ollama) | Done, not yet live-tested |
| Budget governor (token-rate degrade ladder) | Done, not yet live-tested |
| Keyboard/mouse + text entry | Planned |
| Ears, voice, memory, MCP control plane | Planned |

## Quickstart

Requires Windows, Python 3.12, and [ViGEmBus](https://github.com/nefarius/ViGEmBus).

```bash
pip install -e ".[dev]"
python -m pytest
```

Then, with a controller-capable game running:

```bash
python -m player2.demo session --profile profiles/factorio.toml
```

```
player2> up 3
player2> square 1.5
player2> macro toggle_pause
player2> events
player2> quit
```

`session` keeps one virtual pad open for its lifetime and focuses the game itself — both of
which matter more than they sound. See [docs/PLATFORM-NOTES.md](docs/PLATFORM-NOTES.md).

To see what the agent sees:

```bash
python -m player2.demo capture --profile profiles/factorio.toml --frames 6
```

Not working? `python -m player2.demo readback` drives the pad and reads its real state back
out of Windows, **with no game running**, which settles "is our pad broken or is the game
ignoring it?" in about five seconds.

## Architecture

Five independent threads, so nothing cognitive can ever stall the hands:

| Thread | Owns | May block? |
|---|---|---|
| Scheduler | 120Hz tick, **sole writer to the controller** | Never |
| Capture | frames → ring buffer | Never |
| Fast policy | perceive → action chunk → submit | Bounded |
| Conversation | mic → STT → LLM → TTS → speakers | Freely |
| Recorder | drains the event bus to disk | Buffered |

They communicate only through a command queue into the scheduler and an event bus out of it.
That is what makes *"it keeps moving while it talks"* true by construction rather than by hope.

### Invariants

Violating any of these is a defect, not a style choice.

1. **The scheduler thread is the sole writer to the controller.** Everything else submits.
2. **A new action chunk resolves against NEUTRAL, not live state.** Anything a chunk does not
   mention is released. This is what stops a chunk that only says "press A" from inheriting a
   held stick and walking the character into a lake forever.
3. **Absent is not empty.** `buttons=None` inherits; `buttons=frozenset()` releases.
4. **One clock.** Everything timestamps from `player2.clock`.
5. **The recorder captures what the device actually did**, not what the model proposed.
6. **MCP is the control plane, never the servo bus.** Frames and chunks never travel over it.
7. **Voice is opt-in.** Nothing in the core imports `player2.audio`.

Silence means stop: if no fresh chunk arrives, a deadman releases every input. That is the
difference between a teammate and a character sprinting into a lake.

## Documentation

| Document | What it's for |
|---|---|
| [docs/CARRYOVER.md](docs/CARRYOVER.md) | **Start here.** Full project state, written to be read cold |
| [CLAUDE.md](CLAUDE.md) | Working agreement: roles, routing, review protocol |
| [docs/STORAGE.md](docs/STORAGE.md) | Frame storage and inference-cost measurements, and where both go next |
| [docs/PLATFORM-NOTES.md](docs/PLATFORM-NOTES.md) | Windows and game-integration gotchas, and the diagnostics that resolve them |
| [docs/DEV-PROCESS.md](docs/DEV-PROCESS.md) | Every review round, measured — a routing policy derived from our own numbers |

## How this is built

Claude is the lead engineer and owns architecture, tests, and the final diff. Codex
(`gpt-5.6-sol` / `terra` / `luna`) is a consultant that implements against Claude-authored
tests and reviews diffs adversarially. Every finding is adjudicated — reproduced or rejected
with a reason — and every round is logged with its token cost.

`docs/DEV-PROCESS.md` is a deliverable, not overhead. Some of what it has already recorded:

- **A green suite is evidence about the tests, not the code.** Unit 2 passed 194/194 with
  strict types and clean lint, and still had four critical defects that could strand a held
  controller.
- **Luna is viable for tightly-specified work and cheap** — it even caught a real bug in
  Claude's tests — but it does not think adversarially about its own output.
- **Adversarial review reads diffs, so it finds defects that live in diffs.** The single
  worst bug so far lived in the *lifetime of a resource across process boundaries*, was
  invisible in every diff, and was caught by a human watching the real system.
