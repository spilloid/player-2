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

> **Machine transition in progress as of 2026-08-13.** Everything below this note describes
> the Surface Laptop 4 this project was built on through Unit 8 and both live-verification
> passes. Joey is moving the work to a separate gaming PC — likely exactly the "gaming rig"
> roadmap item 7 already anticipated for GPU-accelerated local inference. **A fresh session on
> the new machine must re-verify every fact below, not assume it carries over**: CPU/GPU/RAM,
> whether ViGEmBus is installed, whether Factorio is installed and at what path, and whether
> Ollama is installed with which models pulled. None of that is guessable from this file.
> Redo the setup checklist in §9 below on the new machine before trusting anything that
> depends on it (live gameplay, `demo.py agent`, Ollama's OpenAI-compatible transport).

- Dev machine **was** a **Surface Laptop 4**: i7-1185G7, **Iris Xe, no discrete GPU**, 16GB.
  The "fast" policy was not fast there — confirmed directly, not just assumed, by the
  2026-08-13 live Ollama runs (25s–180s+ per decision on an 8B vision+tools model). Update
  this line with the new machine's real specs once known; do not keep citing Surface Laptop
  numbers as if they still apply.
- **ViGEmBus is installed and running** *on the Surface Laptop*; `vgamepad` binds to it.
  Virtual pad appears on XInput slot 0 there. **Unverified on the new machine** — ViGEmBus is
  a per-machine driver install, not something that travels with the repo.
- Target game: **Factorio: Space Age 2.0.7**, was at `C:\Games\Factorio\bin\x64\factorio.exe`
  *on the Surface Laptop*. **The install path (or whether Factorio is installed at all) on
  the new machine is unknown.** `input-method=game-controller` has been set in
  `%APPDATA%\Factorio\config\config.ini` (backup at `config.ini.player2-backup`) — that is a
  per-user Factorio config, so it needs to be set again on the new machine's own
  `%APPDATA%\Factorio\config\config.ini`. **Factorio must be closed when editing that file**
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

1. ~~**Direct SDK transport**~~ — **done** (`docs/DEV-PROCESS.md` Unit 7). `player2/agent/`
   gained `model_schema.py` + `model_policy.py` (a provider-neutral `SDKPolicy` behind an
   injectable `IModelTransport` seam) and three transports: `anthropic_transport.py`,
   `openai_transport.py` (optional import, `pip install -e .[models]`), and
   `cli_transport.py` (the `codex exec` fallback CARRYOVER called for, zero credentials
   needed). `SDKPolicy` reuses `contracts.chunk_from_dict` and `video.encode.{downscale,
   encode_jpeg}` rather than a second validator or image pipeline. Not yet wired into
   `AgentLoop` in a live session — that's config, not code, and is a natural first task for
   whoever picks up step 2. Two things explicitly **not** done here, on purpose: `SDKPolicy`
   does not shrink its timeout to the observation's remaining decision budget, and frame
   preprocessing has no bound on count or memory. Both are step 2's job, not patched here.
2. ~~**Budget governor**~~ — **done** (`docs/DEV-PROCESS.md` Unit 8), together with a 4th
   transport requested mid-unit. `player2/agent/budget.py`: `BudgetGovernor` (rolling
   tokens-per-minute window, NORMAL/DEGRADED/EXHAUSTED, non-sticky recovery) and
   `GovernedTransport` (a transparent `IModelTransport` wrapper that feeds it — zero changes
   needed to `SDKPolicy` or the three Unit 7 transports beyond one `last_usage` property
   each). `AgentLoop` gained an optional `governor=` param; `governor=None` is byte-for-byte
   the old behavior. `player2/agent/ollama_transport.py` (`OllamaTransport`, a thin
   `OpenAITransport` subclass against Ollama's OpenAI-compatible endpoint) shipped in the
   same unit. All four transports + the governor are now wired into `python -m player2.demo
   agent` behind `--policy sdk --provider {anthropic,openai,cli,ollama} --budget-tpm N`; the
   `agent_notes` field(profile.py) that step 1 anticipated is read from the profile and
   becomes the SDK policy's goal. **Still not done, explicitly out of scope by review
   adjudication:** true multi-caller admission control (the governor assumes one governed
   call in flight at a time; two AgentLoops sharing one governor could both start expensive
   calls before either is recorded) and the "raise the change threshold" lever, which needs
   step 3 (the perceptual gate) to exist first. **Live-tested against real local Ollama on
   2026-08-11** (see DEV-PROCESS.md Unit 8's live-verification note): connectivity, transport,
   `SDKPolicy`, and the governor's failure-path accounting all confirmed working end-to-end.
   The remaining gap narrowed to something specific: neither small model pulled on this
   machine (`smollm2:1.7b` — tools but no vision; `moondream` — vision but no tools) can do
   both halves of `propose()`'s job. **Anthropic and OpenAI remain completely untested against
   real credentials** — nobody has set an API key yet.
3. **Wire the perceptual gate into `AgentLoop`.** Smallest of the original three levers, and
   now also what unblocks the "raise the change threshold" degrade step the governor
   currently can't use.
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
7b. ~~**Ollama transport**~~ — **done**, see item 2. Landed the same day it was requested
   (Joey initially said "within months," then corrected to "not within months" mid-session —
   see `docs/DEV-PROCESS.md` Unit 8). Live-tested the same day, sooner than planned, because
   Joey installed Ollama and pulled a model unprompted. First result: neither `moondream`
   (this transport's original shipped default — vision, no tools) nor `smollm2:1.7b` (tools,
   no vision) could do the job. **Resolved 2026-08-13**: `hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF`
   does both, confirmed against the real production schema and a real `Frame` — a complete,
   valid `ActionChunk` came back through `SDKPolicy.propose()`. Now the shipped default.
   Latency is real and not small: 25s–180s+ per decision on this CPU-only Surface Laptop 4,
   nowhere near the ~1.5s fast-policy cadence — confirmed, not assumed, exploratory-use-only
   for now. Along the way, hit and diagnosed an unrelated, unresolved Windows-specific Ollama
   bug (`ollama/ollama#15074`, 401 on every new registry pull); worked around it by pulling
   from Hugging Face directly (`ollama pull hf.co/<org>/<repo>`), which bypasses Ollama's own
   registry entirely. Full account in `docs/DEV-PROCESS.md`'s 2026-08-13 live-verification
   note.
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

---

## 9. Setup checklist for a new machine

Nothing below is committed to the repo — it's per-machine state that has to be redone. Verify
each one directly (probe, don't assume) before trusting anything that depends on it.

1. **Python 3.12**, then `pip install -e ".[dev]"` from the repo root. Run `python -m pytest`
   once, clean, before touching anything else — it should be ~560 tests, all green, stable
   across 3 runs (`for i in 1 2 3; do python -m pytest -q; done`). If it isn't, something
   about the new machine is different in a way that matters; find out before writing code.
2. **ViGEmBus**: install from https://github.com/nefarius/ViGEmBus, then
   `python -m player2.demo probe` — confirms Windows sees the virtual pad at all — followed by
   `python -m player2.demo readback`, which drives all four stick directions and reads them
   back through XInput with no game running. Both must pass before believing any game
   integration issue is our code and not a missing/broken driver.
3. **Factorio 2.0.7**, any install path. Set `input-method=game-controller` in that install's
   own `%APPDATA%\Factorio\config\config.ini` (Factorio must be closed while editing — it
   rewrites the file on exit). `python -m player2.demo capture --profile
   profiles/factorio.toml --frames 6` is the fastest way to confirm the window is found and
   frames actually arrive.
4. **For local models (Ollama)**: install Ollama, then `ollama pull
   hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF` (this transport's current default, confirmed working
   — see §7b above and DEV-PROCESS.md's 2026-08-13 note). Use the `hf.co/...` form, not
   Ollama's own library search, if `ollama pull <name>` from the regular registry starts
   returning `401` — that's a known unresolved upstream bug
   (`ollama/ollama#15074`), not a project or machine problem, and the Hugging Face path
   sidesteps it entirely. A GPU changes the viability calculus here enormously: every latency
   number in DEV-PROCESS's live-verification notes (25s–180s+ per decision) was measured
   CPU-only. Re-measure on the new hardware before assuming it's still too slow for the fast
   policy's ~1.5s cadence — that conclusion was hardware-specific, not a property of the model.
5. **For cloud models (Anthropic/OpenAI)**: neither has been live-tested by this project at
   all, on any machine, as of this writing. Set `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` and
   that path becomes available; nobody has done it yet, so treat the first live call as truly
   first-of-its-kind, same caution as the Ollama live-verification passes got.
6. **Codex CLI**: confirm `codex --version` works from PowerShell (not necessarily Bash — see
   §3's note on PATH). Re-auth (`codex exec` prompts if needed) before routing any unit to
   Terra/Sol/Luna.
