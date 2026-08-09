# Carryover pack

State of the project as of commit `91447e5`. Written to be read cold by a fresh session.

**Read these four files first, in order:** this one, `CLAUDE.md` (working agreement),
`docs/PLATFORM-NOTES.md` (Windows/game gotchas that look exactly like bugs), and
`docs/DEV-PROCESS.md` (what we have measured about how to build this).

---

## 1. What this is

A **game-independent embodied AI Player 2**. It sees the screen, hears the game and you,
speaks back, remembers what it learns, and plays with an ordinary controller — the same input
device a human uses. No game APIs, no memory reading, no injection.

The test the whole thing is built around:

> Could I launch a game the AI has never played, hand it Player 2, and say
> *"okay, I'll show you how this works"*?

The framing is a media project as much as an engineering one: *"I gave an AI Player 2 and
taught it how to play games with me."* Not "ChatGPT beats a game" — an NPC that has somehow
been handed a controller, that accumulates a shared history with you, argues about strategy,
develops favourite tactics, and occasionally saves or spectacularly fails you.

### The hard constraint

**No game-specific knowledge in the runtime.** No `heal_party()`, no `seek_cover()`. The
lowest abstraction is the human input device: *make the pad have these states at these times.*
Game understanding lives in the model.

Test for every component: *could this run unmodified in Factorio, Baldur's Gate 3, and an
unknown future game?* Facts about a specific game live in `profiles/*.toml`. This is enforced
by `test_no_game_names_appear_in_the_runtime`, which greps `player2/` for game names and has
already caught three real leaks. **Never weaken that test; reword the offending line.**

### Invariants (violating one is a defect, not a style choice)

1. The scheduler thread is the **sole writer** to the controller. Everything else submits.
2. A new action chunk resolves against **NEUTRAL**, not live state. Anything a chunk does not
   mention is released. This stops a chunk that only says "press A" inheriting a held stick.
3. **Absent is not empty.** `buttons=None` inherits; `buttons=frozenset()` releases.
4. **One clock.** Everything timestamps from `player2.clock`.
5. The recorder captures **what the device actually did**, not what the model proposed.
6. **MCP is the control plane, never the servo bus.** Frames and chunks never travel over it.
7. **Voice is opt-in.** Nothing in core imports `player2.audio`.
8. **Silence means stop.** No fresh chunk ⇒ the deadman releases every input.

---

## 2. Where the project actually is

**Milestone 1 met and exceeded.** The runtime sees a real commercial game, decides, acts, and
records all three on one clock — verified live against Factorio, then replayed showing the
controller state at the instant of each frame.

| Subsystem | State |
|---|---|
| `clock.py`, `contracts.py` — time base, action chunks | Done, hardened |
| `control/` — scheduler, ViGEm pad, null adapter | Done, hardened, verified on hardware |
| `profile.py`, `profiles/` — game facts as config | Done |
| `window.py` — find/focus the game window | Done |
| `video/` — capture, ring buffer, encode, fingerprint | Done, hardened |
| `record/` — manifest, streams, dedup, replay | Done, hardened |
| `agent/` — Observation, IFastPolicy, AgentLoop | Done, hardened |
| Keyboard/mouse + text entry (in-game chat) | **Planned** (see §7) |
| Ears, voice, memory, MCP control plane | **Planned** |

**414 tests**, `ruff` + `mypy --strict` clean, 20 commits. Suite is stable across repeat runs;
several tests are threaded, so always run it 3x before believing it.

### Try it

```bash
python -m player2.demo session --profile profiles/factorio.toml   # interactive, one pad
python -m player2.demo agent --profile profiles/factorio.toml \
    --pattern spiral --warmup menu_confirm --seconds 45           # full loop + recording
python -m player2.demo replay --session recordings/<id>           # frame ↔ pad alignment
python -m player2.demo readback                                   # is the pad really moving?
```

---

## 3. Environment facts you cannot guess

- Dev machine is a **Surface Laptop 4**: i7-1185G7, **Iris Xe, no discrete GPU**, 16GB. The
  "fast" policy is not fast here and the docs must keep saying so.
- **ViGEmBus is installed and running**; `vgamepad` binds to it. Virtual pad appears on
  XInput slot 0.
- Target game: **Factorio: Space Age 2.0.7**, `C:\Games\Factorio\bin\x64\factorio.exe`.
  `input-method=game-controller` has been set in `%APPDATA%\Factorio\config\config.ini`
  (backup at `config.ini.player2-backup`). **Factorio must be closed when editing that file**
  — it rewrites it on exit.
- Codex CLI lives at `%APPDATA%\npm\codex.cmd` and is **not on the Bash tool's PATH**, only
  PowerShell's. There is **no `--reasoning-effort` flag**; use `-c model_reasoning_effort=`.
  Valid: `low, medium, high, xhigh, max`, plus `ultra` on Sol/Terra only. `none` is invalid.
- **The shell tool caps at 10 minutes.** Sol/ultra and long fix rounds exceed it. Run those
  with `run_in_background: true` or expect a SIGTERM — note the work usually completes and
  only the verification loop gets killed, so *check the tests before assuming failure*.
- pytest `basetemp` is pinned into the repo (`.pytest-tmp`), because an elevated run creates
  `%TEMP%\pytest-of-<user>` under a different token and every later normal run then dies with
  `PermissionError` before a single test executes.

---

## 4. How we build (working agreement)

Claude is lead engineer and owns architecture, **the tests**, the final diff, and validation.
Codex is a consultant. **Codex takes first crack at implementation on most units**, but
**Claude writes the tests first, always** — the test file is the spec handed to Codex.

Every finding is adjudicated: reproduced, or rejected with a one-line reason. Every round is
logged in `docs/DEV-PROCESS.md` with token cost. That log is a deliverable, not overhead.

### Routing, as our own numbers support it

| Tier | Verdict from measurement |
|---|---|
| **Luna** | Genuinely viable for tightly-specified work and cheap. Passed 93/94 first try on a 94-test spec at 32k tokens, and caught a real bug in Claude's tests. Never modifies a test it was told not to. **Cannot verify what it cannot run** — no desktop in its sandbox. |
| **Terra/high** | Solid implementer, weak reviewer of its own domain. Wrote both the scheduler and the capture layer and was blind to its own lock discipline in each. |
| **Sol/high** | Worth its cost on threads, lifetimes, or any resource that must be released. Found 4 criticals in Terra output twice and 11 in Sol/ultra output once — every time invisible to a fully green suite. |
| **Sol/ultra** | Strong implementer of large multi-file units; **not** a substitute for review. One pass produced 6 coherent modules across 2 packages, green and clean, and Sol/high still found 15 defects in it. Requires Joey's in-session approval. |

### Process rules that earned their place

- **A green suite is evidence about the tests, not the code.** Unit 2 passed 194/194 with
  strict types and clean lint and still had four criticals that could strand a held controller.
- **Claude's tests are the least-reviewed and most load-bearing artifact.** Four broken ones
  caught so far. Ask reviewers to grade the tests explicitly.
- **Review reads diffs, so it finds defects that live in diffs.** The three worst bugs of the
  project did not: a controller unplugged on process exit, a focus check that reported failure
  for every success, and 1.8GB of frames per 12 seconds. All three were found by *running the
  real system*. Budget for that separately.
- **When a reviewer indicts the spec rather than the code, believe it.** The worst defect in
  Units 5+6 was Claude's own design decision, faithfully implemented.
- **Read the other system's logs before touching your own code.** Factorio's log settled two
  debugging sessions instantly.

---

## 5. Measured numbers (do not re-derive these)

### Transport — this shapes everything next

One Luna decision via `codex exec`, structured output:

| Input | Latency | Tokens |
|---|---|---|
| **no image, one-line prompt** | 6.5 s | **12,507** |
| 384px frame | 4.9 s | 11,436 |
| 1280px frame | 5.9 s | 12,817 |

**The image is nearly free; the harness is not.** That ~12.5k floor is `codex exec` shipping a
full coding-agent scaffold per invocation. One decision every 2s ⇒ ~1.9M tokens per 5 minutes.

### Storage, on comparable live sessions

1.8 GB (lossless PNG) → 43.6 MB (downscaled JPEG) → **2.7 MB** (de-duplicated, *paused game*).

### Live gameplay changes the picture

45s of the agent walking a spiral through real terrain:

- **Dedup saves nothing live** — 266 frames, 266 unique. The 93% was purely the paused case.
- **Gate is 2–5x, not 200x.** Adjacent perceptual distance p50=9, p90=14, max=19, so the
  useful threshold band is 8–16; above ~20 it never fires.
- Per-frame is the wrong denominator: 45s at a 700ms cadence is ~64 decisions, not 274.

**Ranked levers: transport 10x > cadence 2x > gate 2.5x.** They multiply.

---

## 6. Open defects and known limitations

1. **The recorder only captures ~23% of frames** (1,170 captured, 266 recorded). It is fed by
   whatever `AgentLoop` polls, so the dataset holds the agent's *sampling* of the screen rather
   than the screen. Fix: the recorder should own frame delivery on its own cadence — which also
   resolves the leading/trailing gap findings from the Unit 5+6 review.
2. **No `fsync`.** JSONL is flushed but not synced, so a host power loss can leave a clean
   manifest with missing rows. Deliberate; documented rather than half-fixed.
3. **Recorder admission takes a short lock**, so "non-blocking" means bounded, not lock-free.
4. **`frames_dropped` counts ring evictions, not loss.** A run reported 219 "dropped" while
   recording 281 of 283 frames with zero gaps. Misleading name; needs renaming.
5. Open speakers will make the agent hear its own voice once TTS lands (headset assumed).
6. Single machine only. The LAN split is anticipated by the interfaces but not built.

---

## 7. Next steps, in order

1. **Direct SDK transport** — the 10x, and the thing standing between us and a Luna-driven v1.
   Install the real `openai`/`anthropic` packages instead of shelling out. Build it behind
   `IDeliberativeModel`/`IFastPolicy` so the CLI adapter stays as a zero-credential fallback.
2. **Budget governor** — tokens-per-minute cap that *degrades* rather than stopping: lengthen
   the chunk horizon, raise the change threshold, drop frames per observation, and finally hand
   control back with an explicit event. Belongs conceptually next to the deadman; both are
   "fail safe when a resource runs out." Joey's explicit concern: *do not cost a user their
   whole usage window in five minutes of play.*
3. **Wire the perceptual gate into `AgentLoop`** (smallest of the three levers, still worth it).
4. **Fix recorder frame ownership** (defect 1 above).
5. **Keyboard/mouse adapter + text entry**, including in-game chat with the profile's opaque
   deny-pattern list (`^/` for Factorio, because its chat box is also a Lua console). Note:
   **Windows UIPI blocks synthetic input into an elevated window** — if the game runs as admin,
   we must too. Controller input is unaffected.
6. Ears, voice (local faster-whisper + Piper, cloud LLM for words), memory (SQLite, model-
   curated rules), MCP control plane (stdio + HTTP).
7. **SmolVLM / Moondream on the gaming rig** — as a Luna *alternative* for the fast policy, not
   as a gate. The gate stays deterministic; a perceptual hash beats a tiny model at that job by
   four orders of magnitude.
8. Segmented hardware-accelerated video (Quick Sync) — the remaining storage 10x.

Also planned: **a GitHub docs page**, so keep documenting decisions in a form that lifts out.

---

## 8. Things Joey has asked for that are easy to forget

- Voice stays **opt-in and modular** (`pip install -e .[voice]`); the Surface must not pay for
  it. Deferred deliberately, not dropped.
- Record demonstrations **from day one** — the dataset is the point, not a side effect.
- Don't use any of this for cheating in ranked competitive multiplayer. Private matches,
  co-op, campaigns, local, mod-friendly environments.
- Keep the DEV-PROCESS log honest, including where Claude was wrong. Several entries record
  exactly that and they are the most useful rows in the table.
