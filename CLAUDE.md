# Player 2 — working agreement

## What this project is

A **game-independent embodied AI Player 2**: eyes (screen capture), hands (virtual controller
and keyboard/mouse), ears (game audio + mic), a voice, and memory. It plays commercial games
through ordinary player I/O — no game APIs, no memory reading, no OCR hacks the model doesn't
perform visually itself.

### The hard constraint

**No game-specific knowledge in the runtime.** The lowest abstraction is the human input device.
There is no `heal_party()`, no `seek_cover()`, no `aim_at_enemy()`. The runtime only knows:
*make the input device have these states at these times.* Game understanding lives in the model.

The test for every component: *could this run unmodified in Factorio, BG3, and an unknown future
game?* If yes, it belongs in the runtime. If no, it belongs in the model, in config, or in an
experiment-specific layer.

Game-specific facts — which window to capture, that `controller-x` mines in Factorio, that `^/`
is a forbidden chat prefix — live in **config and prompts only**. The runtime transports and
timestamps them; it never interprets them.

### Architectural invariants

Violating any of these is a defect, not a style choice:

1. **The scheduler thread is the sole writer to the controller.** Everything else submits through
   its command queue. This is what makes "keeps moving while it talks" true by construction.
2. **A new action chunk resolves against NEUTRAL, not the live pad state.** Anything a chunk does
   not specify is released. Safety over continuity — this prevents a chunk that only mentions a
   button from silently inheriting a held stick and running the character off forever.
3. **Absent is not empty.** `buttons=None` means inherit; `buttons=frozenset()` means release all.
   They serialize as *key absent* vs `[]`.
4. **One clock.** Everything timestamps from `player2.clock`. Audio timestamps by sample count
   with measured drift, never by arrival time.
5. **The recorder captures what the device actually did**, not what the model proposed. Proposals
   get preempted, rejected, clamped, deadman'd, and overridden.
6. **MCP is the control plane, never the servo bus.** Frames and action chunks never travel over it.
7. **Voice is opt-in.** Nothing in the core imports `player2.audio`. `pip install -e .[voice]`.

## Roles

I am the lead engineer. I own architecture, the tests, the final diff, and all validation.
Codex is a consultant. I never accept an external suggestion because it came from a bigger model —
only because I ran it and it held up.

## Invoking Codex

```powershell
$env:PATH = "$env:APPDATA\npm;$env:PATH"   # codex is NOT on the Bash tool's PATH, only PowerShell's
$prompt | codex exec -m <model> -c model_reasoning_effort="<effort>" `
    -s read-only --skip-git-repo-check --ephemeral --color never
```

- **There is no `--reasoning-effort` flag.** Effort is `-c model_reasoning_effort="high"`.
- **Valid efforts: `low, medium, high, xhigh, max`** — plus **`ultra` on Sol and Terra only**
  (Luna caps at `max`). `none` is not valid.
- Models: `gpt-5.6-sol` | `gpt-5.6-terra` | `gpt-5.6-luna`. All accept images via `-i <FILE>`.
- **Reviewers get `-s read-only`.** A reviewer has no business writing. Implementers get
  `-s workspace-write`.
- Pipe the prompt on **stdin** — it avoids Windows quoting hell and keeps diffs intact.
- `-o <file>` writes just the final message; `--output-schema <file>` constrains it to a JSON schema.
- Every call prints a harmless `failed to load models cache: missing field base_instructions` warning.

## Routing

Default to the cheapest tier that can do the job. **Escalate up the ladder; never start at the top.
If Terra solves it, stop.**

| Tier | Use for |
|---|---|
| Luna | mechanical work: scaffolding, docs, formatting, test scaffolding, lint sweeps, CRUD |
| Terra | the default consultant: standard feature work, bug fixes, test writing |
| Terra/high | escalation when Terra's first answer is thin or wrong |
| Sol/high | architecture, security review, tricky debugging, migrations |
| Sol/ultra | multi-file refactors touching 5+ files, **and only after Joey approves in-session** |

Never Sol/ultra for single-file work or for review.

## Adversarial review protocol

**Override to the original draft:** Codex takes the **first crack at implementation** on most units.
I do not write every first draft. Instead:

1. **I write the tests first, always.** Tests are the contract, and the contract is mine. The test
   file is the spec handed to Codex.
2. Codex implements against those tests, or reviews a diff — depending on the unit's routing.
3. When sending a diff for review, **do not say it was AI-written.** Ask for a defect list ranked
   by severity, not a rewrite. Send the minimum diff plus the interface it touches — never the repo.
4. **Adjudicate every finding myself:** reproduce it, or mark it rejected with a one-line reason.
   Never apply a fix I haven't reproduced as a real defect.
5. Run the full suite before and after. A change with no failing test preceding it is speculative —
   write the test first or drop the change.
6. Log the round in `docs/DEV-PROCESS.md`.

Periodically **invert it**: I implement a unit and Codex evaluates me. This is how we learn where
each model actually fails.

## Budget

- No hard call cap; use judgment, ~2 calls per work unit is the norm. Ask before anything expensive.
- **Log estimated tokens and wall time per call. Cost data is a deliverable, not overhead.**
- Never send the whole repo.

## The real deliverable: `docs/DEV-PROCESS.md`

After each work unit, append a row:

| Unit | Task type | Author | Reviewer | Defects found | Defects real | Caught by tests instead | Est. tokens | Verdict |

Every 5 units, revise the **Findings** section with rules that earned their place — e.g. "Sol/high
catches concurrency bugs Terra misses; Terra is sufficient for CRUD review." Delete rules the data
stops supporting. The goal is a routing policy backed by our own numbers.

**Standing question:** Joey never uses Luna. Route to it deliberately and record where it succeeds
and where it breaks, so the cost offset is measured rather than assumed.

## Definition of done for a work unit

Tests pass, review round logged, `DEV-PROCESS.md` updated, diff is one coherent commit whose
message explains **why**.

## Commands

```powershell
python -m pytest              # full suite
python -m ruff check .        # lint
python -m mypy player2        # types (strict)
python -m player2.demo        # drive the attached game
```

## Environment notes

- Dev machine is a Surface Laptop 4: i7-1185G7, **Iris Xe, no discrete GPU**, 16GB. The "fast"
  policy is not fast here and the docs must keep saying so until better hardware or a trained
  motor policy exists.
- **ViGEmBus is installed and running**; `vgamepad` binds to it.
- First target is **Factorio 2.0.7** (`C:\Games\Factorio\bin\x64\factorio.exe`, save `newbie.zip`).
  It has native gamepad support: set `input-method=game-controller` in
  `%APPDATA%\Factorio\config\config.ini`.
