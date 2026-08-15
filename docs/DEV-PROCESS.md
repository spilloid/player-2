# Dev Process Log

This file records every adversarial review round between the lead engineer (Claude) and the consultant (Codex), and exists to derive a routing policy from our own measurements rather than vendor benchmarks.

## Milestone 1 — reached 2026-08-09

**An agent process manipulated a real commercial game through a standard controller
abstraction.** Factorio 2.0.7, virtual Xbox pad via ViGEm, action chunks authored the same way
a model will author them. No game API, no memory reading, no injection — the same input device
a human uses.

Observed in the live run: 6 chunks accepted, 5 preemptions as each new intent replaced the
previous one, and the deadman releasing the pad at 6706ms when the agent went silent. The
safety property that matters most — *silence means stop* — held in a real game, not just in
tests.

Getting there took one diagnostic worth remembering. When the first attempt moved nothing, the
question "is our pad broken or is the game ignoring it?" was answered not by guessing but by
Factorio's own log:

```
166.051 Game controller connected: instance: 0, Xbox 360 Controller
177.201 Game controller disconnected: instance: 0, Xbox 360 Controller
```

Eleven seconds apart — exactly the demo run. That single line eliminated the entire
"our code is broken" branch and pointed straight at `input-method=keyboard-and-mouse`.
**Rule: when an integration fails, read the other system's logs before touching your own code.**

## The controller-lifetime bug — found by Joey, not by the models

Symptom: the character walked three sides of a square but not the first, and the game
"paused itself" at the end even though the command being run pressed **no buttons at all**.

Claude proposed terrain and focus-change. Both wrong. Joey's guess — *"I think it's you
unplugging the controller"* — was right, and the log proved it immediately:

```
instance: 0 ... connected / disconnected
instance: 1 ... connected / disconnected
instance: 2 ... connected / disconnected
```

**Every one-shot CLI run hot-plugs a brand-new virtual controller and rips it out.** Titles
with console ports pause when the active controller disappears, so each run left the game
paused, and the *next* run spent its first chunk getting back into a running game. Both
symptoms, one cause.

Why the models missed it: the defect was not in any diff. Every module was individually
correct, all 230 tests passed, and the bug lived in the *lifetime* of a resource across
process boundaries — visible only by watching the system behave over several runs. Neither
reviewer was ever shown that, because a diff cannot contain it.

The architectural lesson outlives the demo: **the agent's hands must outlive any single
decision.** The real runtime is long-lived and gets this free; a one-shot CLI does not. It
also would have quietly corrupted the demonstration dataset later — a device that vanishes
between recordings breaks exactly the continuity the dataset exists to capture. Fixed with
`demo session`, which keeps one pad plugged in.

**Rule: adversarial review reads diffs, so it finds defects that live in diffs. Bugs in
lifetime, deployment, and cross-run state need someone watching the real system.** Budget
for that separately; do not expect a reviewer to supply it.

## Findings

_Revised after 7 units. Backed by our own measurements; delete any rule the data stops
supporting._

### Routing, as the numbers actually support it

- **Luna is viable for tightly-specified work and genuinely cheap.** Given tests as the spec it
  passed 93/94 on a first attempt at 32k tokens, and it caught a real bug in Claude's tests.
  It has never once modified a test it was told not to touch.
- **Luna does not think adversarially about its own output**, and cannot verify work it cannot
  run — its sandbox has no desktop, so every window test failed there and passed here. It
  reported that accurately rather than claiming success. **Never ship Luna output unreviewed;
  never delegate OS-integration verification to it.**
- **Terra/high is a solid implementer and a weak reviewer of its own domain.** It wrote the
  scheduler and the capture layer and was blind to its own lock discipline in both.
- **Sol/high is worth its cost on anything with threads, lifetimes, or a resource that must be
  released.** Four criticals in Terra output twice, eleven criticals in Sol/ultra output once,
  every time invisible to a fully green suite. **Rule: threads, locks, shutdown ordering, or a
  released resource ⇒ Sol/high review regardless of diff size.**
- **Sol/ultra is a strong implementer of large multi-file units and is not a substitute for
  review.** One pass produced six coherent modules across two packages, green and clean — and
  a separate Sol/high pass still found 15 defects in it. Ultra reduces the number of rounds,
  not the need for them.

### About the process itself

- **A green suite is evidence about the tests, not the code.** Unit 2 passed 194/194 with
  strict types and clean lint and still contained four criticals that could strand a held
  controller. Tests written by the same mind that specified the design encode the same blind
  spots.
- **Claude's tests are the least-reviewed and most load-bearing artifact here.** Four broken
  ones caught so far — three by a reviewer, one by an implementer hitting a fixture that
  raised before reaching the code under test. Ask reviewers to grade the tests explicitly.
- **The most valuable review finding may not be a defect.** Sol's best contribution in Unit 4
  was noticing that none of its other twelve findings were reachable by any test. The fix was
  a seam, not a patch.
- **Review reads diffs, so it finds defects that live in diffs.** The three worst bugs of the
  project so far did not: a controller unplugged on process exit, a focus check that reported
  failure for every success, and 1.8GB of frames per twelve seconds. All three were found by
  running the real system. **Budget for that separately; a reviewer cannot supply it.**
- **A review finding can be right about the problem and wrong about the fix.** Adjudicate the
  *problem*, then choose the fix yourself. Several fixes here differ from what was proposed.
- **When a reviewer indicts the spec rather than the code, believe it.** The worst defect in
  Units 5+6 was Claude's design decision, faithfully implemented.

- **Luna is sufficient to implement a tightly test-specified module, and cheap.** Given 94 tests
  as the spec, Luna/medium passed 93 on the first attempt at 32k tokens. Both Unit 0 and Unit 1
  landed correct happy-path code with no scope creep.
- **Luna does not think adversarially about its own output.** Terra/high found 9 real defects in
  Luna's implementation, two of them safety-critical (a "frozen" state that stayed aliased to a
  caller's mutable list; a malformed wire payload silently decoding to the real command RELEASE
  ALL BUTTONS). None were things the passing tests could have caught, because the tests encoded
  the same assumptions the implementation did. **Rule: never ship Luna output without at least a
  Terra/high review.** Cheap to write is not cheap to trust.
- **A cheap tier still beats the lead engineer at reading the lead engineer's spec.** Luna caught
  a genuine bug in Claude's test suite (`zip(a, a[1:], strict=True)` always raises) and, as
  instructed, implemented to the spec anyway and flagged it rather than editing the test.
  Instructing the implementer not to touch the tests is doing real work — keep that instruction.
- **The reviewer is not a substitute for the author's own review.** The one defect neither model
  found — every deserialization failure collapsing into one useless message — was introduced *by
  the fix round* and caught by Claude reading the diff afterwards. Re-read diffs after a fix
  round; the fix round is where regressions enter.
- **A review finding can be right about the problem and wrong about the fix.** Terra flagged
  ambiguity in what the pad does before a chunk's first keyframe and proposed changing `sample()`.
  The better fix was to legislate the ambiguity away: require the first keyframe at `t_ms=0`.
  Adjudication means judging the *problem*, then choosing the fix yourself.
- **A green suite is evidence about the tests, not about the code.** Unit 2 passed 194/194 with
  strict types and clean lint, and still contained four critical defects that could strand a held
  controller. Tests written by the same mind that specified the design encode the same blind
  spots. **Rule: a green suite does not retire the review round — on concurrency it barely
  starts it.**
- **Sol/high earns its cost on concurrency and lifecycle; Terra/high does not appear to.** Terra
  wrote the scheduler and was blind to its own lock discipline. Sol found 4 criticals in it.
  Provisional routing: **anything involving threads, locks, shutdown ordering, or a resource that
  must be released goes to Sol/high for review, regardless of how small the diff is.**
- **Ask the reviewer to grade the tests too.** Sol caught a test of Claude's that asserted after
  ticking and therefore could not fail for the reason it claimed. Reviewers see faked tests that
  their authors cannot. This has now happened three times — twice caught by a reviewer, once by
  Terra finding a fixture that raised before reaching the code under test. **Claude's tests are
  the least-reviewed artifact in this process and the most load-bearing.**
- **The most valuable review finding may not be a defect.** Sol's best contribution in Unit 4
  was the observation that none of its other 12 findings were reachable by any test, because the
  code sat behind an API that could only be smoke-tested. The fix was a seam, not a patch. Ask
  reviewers explicitly: *what here is untestable, and what would make it testable?*
- **Sol/high has now found 4 criticals in Terra/high output twice.** Both times in code Terra
  wrote and believed finished, both times invisible to a full green suite, both times concerning
  a resource whose lifetime or ownership crossed a thread boundary. The routing rule stands and
  is now supported by two independent observations rather than one.

## Routing

- Luna = mechanical work
- Terra = default consultant
- Terra/high = escalation
- Sol/high = architecture, security, tricky debugging
- Sol/ultra = multi-file refactors of 5+ files, requires approval in session.

## Log

| Unit | Task type | Author | Reviewer | Defects found | Defects real | Caught by tests instead | Est. tokens | Verdict |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A | Architecture review (greenfield design, no code) | Claude | Sol/high | 20 | 13 now + 4 deferred | n/a (pre-code) | 10,828 (191s) | Worth it. Found a real safety bug: analog state surviving preemption when the next chunk omits it — the agent would hold a stick forever. Now invariant #2. 3 findings rejected/reframed: assumed asyncio starvation (already threaded), deadman self-hang (unfixable in-process, documented), `propose()` rewrite framing (concrete fix adopted, framing not). |
| 0 | Scaffold (pyproject, packages, gitignore, DEV-PROCESS skeleton) | Luna/low | Claude | 0 | 0 | n/a | 8,618 (96s) | Luna followed a tight spec exactly — right structure, no invented content, no scope creep, obeyed all four "do not" instructions. Cheapest tier is sufficient for specced scaffolding. Open question: does that hold when the spec is behavioural rather than structural (Unit 1)? |
| 1 | Pure-logic contracts: clock, PadState/Keyframe/ActionChunk/Frame, validate, resolve, sample, JSON codec | Luna/medium | Terra/high | 12 (11 Terra + 1 Claude) | 10 | 1 — but in *Claude's* tests, not Luna's code | 100,314 (565s across 3 calls) | See notes below. |

| 2 | Motor system: IControllerOutput, null + ViGEm adapters, tick-driven Scheduler, SchedulerThread | Terra/high | **Sol/high** | 10 (9 Sol + 1 Claude) | 9 | **0** | 147,098 (912s across 3 calls) | See notes below. The headline: 194 passing tests found **none** of the 9 defects. |

| 4 | Eyes: IVideoSource, FrameRing, fake source, Windows Graphics Capture backend | Terra/high | **Sol/high** | 14 (13 Sol + 1 Terra, in Claude's tests) | 13 | 0 | ~205,000 (1,350s across 3 calls) | See notes below. Sol's most valuable finding was not a defect at all. |

| 5+6 | Recorder + agent seam + shared frame encoding (6 files, 2 packages) | **Sol/ultra** | **Sol/high** | 17 (15 Sol + 2 from live runs) | 17 | 0 | ~480,000 (~2,900s across 4 calls) | First use of ultra. Produced a large, coherent, working implementation in one pass — and still needed 15 defects fixed. |

| 7 | Direct SDK transport: provider-neutral policy seam (`model_schema`, `model_policy`) + three transports (Anthropic, OpenAI, `codex exec` CLI fallback) — 5 files | **Sol/ultra** | **Sol/high** | 9 | 8 real, 1 correctly self-rejected by the reviewer | 0 | ~143,000 (666s + 1 call) | See notes below. First unit where two of the reviewer's findings were reproduced directly against real Windows process behavior before being adjudicated, rather than adjudicated from the writeup alone. |

| 8 | Budget governor (`budget.py`: rolling token-rate window, degrade ladder, `GovernedTransport`) + `AgentLoop` cadence integration + 4th transport (`OllamaTransport`, OpenAI-compat) + `last_usage` telemetry on all 4 transports — 7 files | **Terra/high** | **Sol/high** | 11 + 1 Claude (self-caught before review) | 11 real, 6 fixed with new tests, 5 accepted-and-documented as scoped limitations | 0 | 57,167 (Terra) + 19,505 (Sol/high review) | See notes below. Cheapest full-unit implementation to date at this scope (7 files) — a well-decomposed, test-first spec let Terra/high do it directly with no Sol/ultra escalation. |

| 9 | Remote-host config layer: `player2/config.py` (CLI > env var > `.env` > default precedence) + `demo.py` CLI wiring for a LAN Ollama endpoint (`--ollama-base-url`, `--env-file`/`--no-env-file`) | Terra/medium (`config.py`) + **Claude** (`demo.py` wiring, inverted routing) | Terra/high (design consult + both diffs) + Claude (`config.py` review) | 1 (`demo.py` review) | 1, confirmed by direct reproduction | 0 — `demo.py` has no test file by standing project convention | 7,013 (design) + 28,655 (`config.py` impl) + 34,198 (`demo.py` review) = 69,866 | See notes below. First unit mixing a Codex-authored file with a Claude-authored file in the same unit, both reviewed by Codex. |
| 10 | Fix: `AgentLoop._decide()` silently discarded policy-error exception detail (`player2/agent/loop.py`) | **Claude** (inverted routing — `loop.py` has a real suite) | Terra/high | 2 | 1 real, confirmed by direct reproduction; 1 rejected (hypothetical positional-construction compatibility, no such caller exists) | 0 | 49,879 (review) | See notes below. The fix's first draft introduced a worse bug than the one it fixed — an unprotected `str(error)` call turned a poisoned exception's broken `__str__` into full worker-thread death, caught only because the diff was reviewed instead of trusted on a passing test. |
| 11 | Ollama `keep_alive`: `OllamaTransport.warm()` (native-API pre-load, `player2/agent/ollama_transport.py`) + `ollama_keep_alive` config field + `demo.py` wiring (`--ollama-keep-alive`) | Terra/medium (`warm()`) + Luna/low (`config.py` field) + **Claude** (`demo.py` wiring) | Terra/high | 4 | 2 real, both confirmed by direct reproduction; 2 accepted as documented limitations (not fixed) | 0 | (not separately logged this call) | See notes below. Both real findings were confirmed BEFORE trusting the reviewer's writeup, by re-running the exact failure live — same discipline the design phase used to disprove an assumption about the OpenAI-compat endpoint in the first place. |
| 12 | Per-frame relative timestamps in `observation_to_prompt()` (`player2/agent/model_schema.py`) — infrastructure for a future multi-frame observation, deliberately not paired with actually raising `frames_per_observation` above 1 | **Claude** | Terra/high | 3 | 1 accepted-but-unreachable (tightened via docs, not runtime validation — no real caller violates it); 2 real, fixed (`-0 ms` float-formatting artifact; loose substring tests that would pass on a mislabeled frame) | 0 | (not separately logged this call) | See notes below. Joey's own framing going in: "there's a good chance we make the overall experience worse" once frame count actually rises — this unit deliberately ships only the labeling infrastructure, guarded to be a no-op at today's default, so that risk stays isolated to a future, explicitly measured step. |
| 13 | Fix: unbounded scheduler history in `observation_to_prompt()` grew past the model's 4096-token context ceiling over a long session, corrupting every response (`player2/agent/model_schema.py`, `model_policy.py`, `config.py`, `demo.py` — new `history_window`, capped and configurable) | **Claude** | Terra/high | 2 | 2 real, both fixed (a self-caught truthy-vs-presence bug on `--history-window 0`; a ValueError-escapes-uncaught bug in `agent()`'s exception handling, same defect class as Unit 11's `warm()` finding) | 0 | (not separately logged this call) | See notes below. Root cause found by SSH into the remote box and reading `llama-server`'s own logs directly — not inferred, not guessed at from wrapped error messages. |

### Unit 13 detail — reading the other machine's logs instead of guessing at our own

Direct continuation of the live 30-minute run in Unit 12's own live-verification session: 69 of
152 decisions failed with malformed-JSON errors ("unexpected end of JSON input", "invalid
character after decimal point"), overwhelmingly clustered in the run's second half, never
self-recovering. Two increasingly specific hypotheses were tested and ruled out live before the
real cause was found: (1) the newly-expanded `agent_notes` pushing total prompt size near the
4096-token ceiling — disproven by directly reproducing the exact real payload, which measured
2,255 tokens, nowhere near the limit; (2) the model's "thinking" tokens eating the 2048-token
output budget — plausible but unconfirmed, no way to inspect it from the client side alone.

Joey: SSH in and read the actual server. Passwordless key auth and passwordless sudo were both
already configured on the box (`ssh -o BatchMode=yes`, `sudo -n true` — checked before asking for
credentials, per this project's own security instincts). `journalctl -u ollama` needed `sudo`
(the service's own logs aren't readable by a regular user); once past that, `llama-server
--log-verbosity 4`'s per-slot logging gave the exact mechanism, no more inference needed:

- `task.n_tokens` climbs by roughly 20 tokens every single decision across the whole session —
  because `observation_to_prompt()` renders `observation.history` (the scheduler's execution
  log) in full, and history grows every decision as more scheduler events accumulate. Nothing
  else in the prompt grows during a session: the image is fixed-size, `agent_notes`/goal are
  static once a session starts.
- `n_ctx_slot = 4096` is fixed, confirmed the same hard ceiling multi-frame testing hit earlier.
- The exact failing request: `slot release: id 0 | task 54607 | stop processing: n_tokens =
  4095, truncated = 1`. `truncated = 1` is the smoking gun — `llama-server` cut the response off
  wherever generation happened to be once the growing prompt filled the context window,
  chopping the JSON tool-call output mid-string, mid-number, mid-object. The model was never
  malfunctioning; its answer was being guillotined by the context limit.
- One honest self-correction, caught by reading the logs rather than trusting a hunch: an
  apparent "mysterious context reset" (`task.n_tokens` dropping to 164) turned out to be Claude's
  own earlier diagnostic probe landing in the request queue — the server has a single processing
  slot (`-np 1`), so investigating a live session competes with the session for the same slot.

Claude wrote tests first (`tests/test_agent_model_schema.py`, `test_agent_model_policy.py`,
`test_config.py`), implemented `observation_to_prompt(observation, *, history_window=30)`
(renders `observation.history[-history_window:]`, not the full tuple, with an "N older event(s)
omitted" note only when something was actually dropped) directly since it's a small, precisely
tested change. `SDKPolicy` gained a matching constructor parameter (same positive-int validation
shape as its existing `max_image_dim`/`jpeg_quality`) threaded into both `propose()` and
`deliberate()`. `AgentLoop`'s own 256-event retention is unchanged — only what gets rendered into
the prompt is windowed. `player2/config.py` gained a `history_window` field mirroring
`ollama_keep_alive`'s shape exactly, routed to **Luna/low** (a deliberate test of the standing
question about mechanical work, clean diff on the first pass). `--history-window` wired into
`demo.py`, following the same CLI > env > `.env` > default precedence as every other knob.

**Self-caught one defect before requesting review**: the first `demo.py` draft copied the
existing `if ollama_base_url:` truthy-check pattern for the new `sdk_kwargs["history_window"]`
assignment — correct for strings (where `""` and `None` are both meaningless, already filtered
upstream), wrong for an int where `0` is a real, if invalid, value. `--history-window 0` would
have silently fallen back to `SDKPolicy`'s own default instead of reaching its validation error.
Reproduced directly (`sdk_kwargs` stayed `{}` for an explicit `0`), fixed to a presence check
(`is not None`), reproduced clean after.

**Terra/high review, 2 findings, both real:**

1. **P2, confirmed by reproduction.** `config.py`'s `_parse_history_window` validates type
   (coerces a string to int) but not range — a well-typed but invalid value (`0`, negative)
   sails through config resolution untouched and is only caught by `SDKPolicy`'s own
   construction-time validation, which runs *after* `agent()` has already started the recorder
   and video capture. `agent()`'s `except ImportError` around `_build_sdk_policy()` didn't catch
   the resulting `ValueError`, so it would have escaped uncaught and skipped
   `recorder.stop()`/`video.stop()` — the identical defect class Unit 11 already found and fixed
   for `warm()`'s `OSError`, just reached through a different, newly-added parameter. Reproduced
   directly (`_build_sdk_policy('cli', ..., history_window=0)` raised `ValueError` uncaught by
   the existing handler). Fixed by broadening the catch to `except (ImportError, ValueError)`.
2. **P3, confirmed, fixed.** The new `SDKPolicy` test covered `history_window` threading through
   `propose()` but not `deliberate()` — both call sites do pass it correctly (Terra verified
   manually), but a future one-call-site regression wouldn't have been caught. Added the missing
   test.

**Live-verified against the real server after the fix**, same rigor as the root-cause
investigation: sent a request with 50 history events (more than the ~24 that had already caused
visible degradation in the original run) through the real `SDKPolicy`/`OllamaTransport` path.
Client reported `input_tokens=2517`; the server's own log for that exact request confirmed
`task.n_tokens = 2517` and, critically, **`truncated = 0`** — no truncation, unlike the
`truncated = 1` that caused the original collapse. Same diagnostic method that found the bug now
proves the fix, not a different, less rigorous one.

Final: 609 tests passing, ruff and mypy `--strict` clean. Deferred idea (summarizing/compacting
dropped history instead of discarding it) logged on the roadmap in `docs/CARRYOVER.md` §7 rather
than built now — not enough evidence yet that simple truncation costs real decision quality.

### Unit 12 detail — infrastructure ahead of an experiment, not the experiment itself

Joey asked directly: does the model get any timestamp with the frames it sees, given how much
motor timing matters? Answer, checked against the actual code rather than assumed: at today's
default (`frames_per_observation=1`, everywhere in the SDK path), yes — the single frame's
timestamp IS the observation's cutoff, stated in the prompt. But `observation_to_prompt()` never
enumerated per-frame timestamps, so if `frames_per_observation` were ever raised above 1 to let
the model perceive motion across several stills, the model would receive N images with no way to
know the time gap between them — the per-frame data (`Frame.session_ms`) already existed, the
text renderer just never used it.

Joey, unprompted, drew the right line before any code was written: this could easily make the
overall experience *worse*, not better — more image tokens per decision directly fights the
9.6-90s latency this session already spent real effort reducing (Units 9-11), and "we just set a
real baseline (v0.2.0), I'm glad we have it to compare against" was the explicit framing. So this
unit ships ONLY the labeling infrastructure, guarded (`if len(observation.frames) > 1`) to be a
verified no-op — zero added tokens, zero behavior change — at the shipped default. Raising
`frames_per_observation` itself stays a deliberately separate, future, measured step: latency and
token cost at N=3/N=5 compared directly against tonight's numbers on the same remote box, before
any default changes.

Claude wrote tests first (`tests/test_agent_model_schema.py`), implemented directly (small,
well-specified, in an already-tested module), sent to Terra/high for review. **3 findings, 2
real:**

1. **Medium, real but unreachable today — accepted via documentation, not runtime validation.**
   The prompt's "oldest to newest, 0 ms = newest" claim assumes `observation.frames` is sorted
   ascending by `session_ms`; nothing enforces that structurally. Reproduced with a permitted
   (if unrealistic) out-of-order `Observation` — the claim genuinely becomes false, an earlier
   frame gets labeled `0 ms`. Not fixed with a runtime check: `AgentLoop._decide()` is the only
   real constructor of an `Observation` in this codebase, and WGC — the only real
   `IVideoSource` — already rejects backwards session times, so no reachable caller violates
   this. Adding validation against an unreachable input would be exactly the kind of scope this
   project's own conventions argue against. Fixed instead by tightening
   `IVideoSource.latest()`'s docstring to state the ascending-`session_ms` contract explicitly
   rather than the vaguer "chronological" — closing the "silently assumed" gap without dead code.
2. **Low, real, fixed.** Relative offsets were formatted with Python's `:g`, which renders a
   frame captured a fraction of a millisecond before the cutoff as `-0 ms` (confirmed:
   `f"{-0.3:.0f}"` → `"-0"`) — a genuinely confusing label for a model reasoning about direction
   of motion, and more reachable than the initial "Low" severity suggested (any two frames
   within 0.5ms of each other trigger it). Fixed with `round()` to a plain Python int before
   formatting — ints have no signed zero, so this is structurally impossible after the fix, not
   just less likely.
3. **Low, real, fixed.** The multi-frame test only substring-matched (`"-520" in text`), which
   would still pass if a label were attached to the wrong frame. Rewritten to assert exact lines
   in the rendered output, plus a dedicated regression test for the `-0 ms` case above.

Final: 592 tests passing, ruff and mypy `--strict` clean.

### Unit 9 detail — a second machine, and a defect only the untested half produced

Joey: move Ollama inference off the CPU-only Surface Laptop onto a second LAN machine (a Linux
box, RX 6600, 8GB VRAM, ROCm with `HSA_OVERRIDE`-style overrides) while Factorio itself stays on
Windows — "let's plan and talk" first, then build it. `OllamaTransport` already accepted an
injectable `base_url` (Unit 8), so the runtime needed zero architectural change; the actual gap
was CLI/config plumbing only, which is the outcome a correctly-designed seam should produce.

**Model prep, over HTTP, no SSH:** deleted the box's existing `qwen3:4b` (text-only, no vision —
useless for `propose()`) via Ollama's `DELETE /api/delete`, then pulled
`hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF:Q4_K_M` (Unit 8's confirmed vision+tools model) via
`POST /api/pull`, pinning the quant explicitly given the 8GB card.

**A capability label that lied, caught before it was trusted:** the freshly-pulled model's
`/api/tags` entry reported capabilities `["completion", "vision"]` — no `"tools"` — despite being
the exact model Unit 8 confirmed does forced tool-calling. Rather than trust the label, ran the
real production path against it directly (a real `Frame`, `action_chunk_tool_schema()`,
`OllamaTransport.complete_tool()`, no simplified stand-in): succeeded in **9.6s**, a valid
`ActionChunk` back through the real schema, first attempt. Conclusion: Ollama's capability
metadata for HF-pulled GGUFs cannot be trusted over an actual live call — this project's own
"live-verify before trusting a transport" rule (Units 7–8) caught a second, differently-shaped
version of the same risk. Also notable: 9.6s beats Unit 8's 25.4s CPU-only baseline by a wide
margin, despite the RX 6600 being a modest card running ROCm overrides — real GPU inference, even
constrained, comfortably beats a modern CPU here.

**Design consult before any test was written**, at Joey's explicit request: Terra/high on
precedence chain, module shape, hand-rolled parser vs. `python-dotenv`, naming (`PLAYER2_*`,
deliberately *not* aliasing Ollama's own bare `OLLAMA_HOST` — that variable configures the Ollama
server/CLI's own target, a different meaning that would silently double up), `.env` lookup
location, testability without touching real `os.environ`, and the absent-vs-empty question this
project has hit before (`contracts.py`'s `buttons=None` vs `buttons=frozenset()`). Settled in one
call, 7,013 tok.

Claude wrote 20 tests (`tests/test_config.py`) as the spec, reusing that same absent-is-not-empty
invariant for CLI-mapping key presence rather than inventing a second convention. Terra/medium
implemented `player2/config.py` against them in one pass — 20/20 passing, ruff and mypy clean,
28,655 tok. The only wrinkle: this machine's `.pytest-tmp` was permission-locked from an earlier
elevated run (the exact failure mode `pyproject.toml`'s own comment warns about); Terra correctly
read that as environment noise, not a spec defect, and verified against an isolated basetemp
instead of touching the test file. Claude reviewed the diff directly (no findings) and
independently re-ran the full suite after clearing the stale directory — 579 passed, 1 skipped,
ruff and mypy `--strict` clean.

`demo.py`'s CLI wiring is untested glue by this project's own standing convention — `main()` has
never had a test file, being argparse plumbing over already-tested internals. Rather than force a
spec onto that shape, **Claude authored the wiring directly** (an inversion of the usual routing,
per the standing instruction to periodically implement and let Codex evaluate) and sent the diff
to Terra/high for adversarial review, 34,198 tok.

**Terra found one real defect, confirmed by direct reproduction, not trusted from the writeup:**
config resolution (`.env` load + `resolve_runtime_config`) ran unconditionally before command
dispatch, so a malformed `.env` or an invalid `PLAYER2_PROVIDER`/`PLAYER2_BUDGET_TPM` broke
*every* command — including `probe`, `readback`, and `replay`, none of which touch config — with
a raw traceback instead of this file's normal `parser.error()` style. Reproduced directly:
`python -m player2.demo probe --env-file <malformed>` raised an uncaught `ValueError` to the
terminal. Fixed by dispatching `probe`/`readback`/`replay` before config resolution runs at all,
and wrapping the remaining resolution in `try`/`except (ValueError, OSError)` →
`parser.error(...)`. Re-reproduced clean after the fix: `probe` now survives the same malformed
file untouched; `agent` now fails with `demo.py: error: invalid configuration: ...` instead of a
traceback.

Final: 579 tests passing, ruff and mypy `--strict` clean, plus the live remote smoke test above
run outside the suite. `.env` added to `.gitignore`; `.env.example` committed as the documented
template.

#### Live verification, 2026-08-14 — the remote box actually played the game

Joey opened Factorio, loaded the `newbie` save, left it paused, and asked to hook it up. Ran
`agent --policy sdk --provider ollama --ollama-base-url http://192.168.68.3:11434/v1 --model
hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF:Q4_K_M --profile profiles/factorio.toml --warmup
toggle_pause --seconds 90` — the first time the remote-host config landed in this unit was
actually exercised end to end, not just smoke-tested against a synthetic frame.

Result: window resolved (`'Factorio: Space Age 2.0.7'`), `toggle_pause` warmup unpaused the game
inside the same pad session, 10 chunks proposed, **10 accepted, 0 rejected**, 2 policy errors,
budget stayed at `normal` (15,185 of 60,000 tok/min). Replay's sampled-frame view showed an
all-neutral pad at first glance — misleading until checked against the raw pad log: 465 of 5,983
recorded pad samples were non-neutral, including a real interpolated stick movement at t=42.7s
(`left_stick` ramping smoothly from ~0.50 toward neutral, not noise) and `A`/`Y`/`START` presses.
The neutral-looking sampled frames were `deadman` correctly zeroing the pad between decisions
that took longer than their own chunk's duration to arrive — the safety property working exactly
as designed, not a defect, and a reminder that `replay`'s 8-frame preview is not sufficient
evidence on its own for "did anything happen" on a run this sparse; the raw pad log is the source
of truth.

**Resolved same session, once Joey asked to circle back:** 2 of 12 decision attempts errored
rather than producing an accepted chunk. Root cause found by code inspection, not a captured
traceback — `_decide()`'s swallow (see Unit 10 below) meant there was nothing to inspect from the
run itself. `SDKPolicy` defaults `timeout_s` to 10s (`model_policy.py`), sized for a fast cloud
API; `_build_sdk_policy()` never overrode it. Against this project's own measured latency —
9.6s baseline on this same remote box minutes earlier, 25.4–65.1s on the CPU-only laptop in Unit
8, one attempt over 180s — a 10s cutoff was always going to clip some fraction of real decisions.
Fixed: `_SDK_TIMEOUT_S = 90.0` in `demo.py`, clearing every successful measurement on record
without chasing the one outlier. (First pass at the justifying comment named the game directly in
`demo.py` — caught immediately by `TestRuntimeStaysGameAgnostic`, reworded before it shipped.)

**Zoom controls, added same session** (Joey: view felt too tight). Read `zoom-in-controller`/
`zoom-out-controller` straight from `config.ini` (right-trigger + D-pad up/down, same source
discipline as every other binding in this profile) rather than guessing, added `zoom_in`/
`zoom_out` macros mirroring `toggle_pause`'s RT-before-chord timing, and a line in `agent_notes`
so the live model itself knows zoom exists. Verified with the recorder rather than trusting the
result blind, per Joey's explicit ask: captured a frame before, ran `zoom_out`, captured a frame
after — genuinely wider field of view (two more power poles, radar panel, two buildings entered
frame). Along the way, hit `PLATFORM-NOTES.md` §1 face-first: the Game menu kept reappearing
after every fix attempt because each attempt was its own one-shot `session`/`capture` invocation,
and disconnecting the pad on exit re-triggers Factorio's console-style pause-on-disconnect,
independent of anything the macros did. First guess at closing the menu (`menu_back`, the B
button) did not work; second attempt read Factorio's own UI rather than guessing again — a
"Confirm (Y)" tooltip was visible over the highlighted "Resume" button in the captured frame,
so `menu_confirm` (Y) was tried instead. Also did not close the menu in the following capture,
consistent with §1: the pad disconnects at the end of every one-shot command regardless of what
was pressed, so the menu reappearing was not evidence the fix failed. Not fully closed out this
session — correct per §1's own rule ("the agent's hands must outlive any single decision") is to
never disconnect between the fix and the check, which the next real `agent` run's warmup already
does automatically.

### Unit 10 detail — a fix for a silent swallow that briefly introduced a worse one

Direct follow-up to Unit 9's open item, at Joey's request to "circle onto the errors." Diagnosing
the timeout (above) surfaced a second, independent problem: `AgentLoop._decide()`
(`player2/agent/loop.py`) caught any exception from `self._policy.propose(observation)`,
incremented `policy_errors`, and discarded the exception itself — by original design, so a
malformed model response could never kill cognition, but with the side effect that a live
session reporting "2 policy errors" carried zero information about what they actually were.

Claude wrote the test first (`tests/test_agent.py`): a policy that always raises
`RuntimeError("distinctive failure detail")`, asserting the new field carries that string AND
that the pre-existing `last_error` field (worker-thread death) stays `None` — proving the two
stay genuinely independent, not just that the new one exists. Implemented directly (untested-glue
routing didn't apply here; `loop.py` has a real suite) — a new `AgentStats.last_policy_error`
field, a `_record_policy_error()` method mirroring the existing `_set_last_error()`, deliberately
NOT reusing `last_error` since conflating "one flaky decision" with "the worker thread died"
would make `last_error` unreliable as a liveness signal. Surfaced in `demo.py`'s printed summary.

**Sent for Terra/high review** (an inversion of the usual routing — Claude as author, Codex as
reviewer, per the standing instruction to periodically swap roles) rather than assumed correct
because a test passed. **Found 1 real defect, confirmed by direct reproduction**, not trusted
from the writeup: `_record_policy_error`'s `str(error)` call was unprotected. An exception whose
own `__str__` raises (a real, not hypothetical, shape for a transport error wrapping a
provider-SDK object) would escape `_decide()`'s handler entirely, reach `_run()`'s outer
`except BaseException`, and kill the whole worker thread — turning the exact failure this fix
exists to make survivable into something *more* fatal than the code it replaced. Reproduced
directly: a policy raising an exception with a poisoned `__str__` left `policy_errors` at 0 and
set `last_error` instead of `last_policy_error` — full cognition death, confirmed via `is_running`
before any fix. Fixed with a shared `_describe()` helper (try `str()`, fall back to
`type(error).__name__` on failure) applied to both `_record_policy_error` and `_set_last_error`,
closing the same latent bug in the pre-existing method too. Regression test added, reproduction
re-run clean after the fix (`policy_errors: 9`, `last_policy_error: 'BrokenStr'`, `last_error:
None`). Codex's second finding — `AgentStats.last_policy_error` breaking hypothetical positional
construction by external callers — rejected: no such caller exists anywhere in this codebase and
the dataclass is not a documented public API boundary.

Final: 581 tests passing, ruff and mypy `--strict` clean.

### Unit 11 detail — the same "verify before trusting" discipline, twice in one unit

Joey: "let's implement Keep Alive and make sure we're using Ollama in a way that's time
efficient" — the 30s+ cold-load tax measured live in Unit 9 (and again this unit) is real money
against a session, not a hypothetical.

**Design assumption disproved before any code was written.** The obvious approach — pass
`keep_alive` in the OpenAI-compatible request body, since `OllamaTransport` already goes through
that endpoint for everything — was tested live against the real box first rather than assumed.
Result: Ollama's `/v1/chat/completions` silently ignores it; `/api/ps`'s `expires_at` stayed on
the default 5-minute window regardless of what was sent. Only the native `/api/generate` endpoint
honors `keep_alive`, confirmed by the same live check (expiry landed within a couple seconds of
the requested duration). A third live check closed the design: a normal OpenAI-compat inference
call afterward *extends* an already-set expiry rather than resetting it to 5 minutes — so one
warm-up ping before a session starts is sufficient; every real decision naturally re-extends the
same window. This is why `warm()` exists as a distinct method hitting a different endpoint
entirely, rather than a parameter threaded through the inherited `complete_tool`/`complete_text`.

Claude wrote tests for both new pieces. `OllamaTransport.warm()`/`keep_alive` (`player2/agent/
ollama_transport.py`) went to Terra/medium — a pure payload-building function plus an injectable
native-API pinger, mirroring the project's existing DI pattern for the OpenAI-compat client.
`ollama_keep_alive` in `player2/config.py` — identical in shape to the already-reviewed
`ollama_base_url` field — went to **Luna/low**, a deliberate test of the standing question about
routing genuinely mechanical, already-established-pattern work to the cheapest tier: clean diff,
exact match to spec, only the same pre-existing `.pytest-tmp` permission quirk (correctly not
touched). `demo.py`'s CLI wiring (`--ollama-keep-alive`, calling `.warm()` in `_build_sdk_policy()`
before the loop starts) was Claude-authored directly, same untested-glue convention as Units 9-10.

**Live-verified end to end through the real CLI**, not just the isolated class: `--ollama-keep-alive
15m` against the real box, confirmed via `/api/ps` that `expires_at` landed at 14m58s out —
correct to the second, and specifically through `main()` → `agent()` → `_build_sdk_policy()` →
`OllamaTransport.warm()`, not a shortcut around any of that plumbing.

**Terra/high review, 4 findings, 2 real, both confirmed by reproduction before being trusted —
same discipline as the design phase's live checks, now aimed at the diff instead of an
assumption:**

1. **High, confirmed:** `warm()`'s network call could raise `URLError`/`HTTPError`/`TimeoutError`
   (all subclasses of `OSError`, confirmed by checking Python's own exception hierarchy rather
   than assuming), and `agent()`'s only catch around `_build_sdk_policy()` was `except
   ImportError` — an unrelated exception type. Reproduced directly: pointing `_build_sdk_policy()`
   at an unreachable port raised `urllib.error.URLError` straight out, uncaught, which would have
   skipped `recorder.stop()`/`video.stop()` entirely (no `finally` wraps that section of
   `agent()`). Fixed: `warm()`'s failure is now caught at the call site (`OSError`, printed as a
   warning, session continues without the pre-warm benefit) — an optimization failing must not
   make the whole session fail, but it also must not vanish silently, the same principle Unit 10
   just established for policy errors, now applied to a different failure mode in the same file.
2. **Medium, confirmed:** the native URL was derived by chaining `.removesuffix("/v1")` then
   `.removesuffix("/")` — correct for `.../v1`, wrong for `.../v1/` (an entirely ordinary shape to
   type or paste; this project's own remote box's URL took that exact form the first time it was
   entered tonight). The trailing slash survives unstripped, producing `.../v1/api/generate`
   instead of `.../api/generate`. Reproduced directly against the literal function. Fixed by
   replacing suffix-stripping with `urllib.parse.urlsplit`/`urlunsplit`, discarding the path
   entirely rather than guessing which suffix to strip — correct by construction for any path
   shape, not just the one case that happened to get tested. Regression test added.
3. **Low, accepted as a documented limitation, not fixed:** a sub-second `keep_alive` could
   plausibly expire before the first real decision arrives, since `warm()` runs before policy
   construction, scheduler startup, window focus, and the first captured frame. Not fixed: nobody
   would configure a sub-second keep_alive in practice, and defending against a value that
   nonsensical is scope this project's own conventions argue against.
4. **Low, accepted:** flagged test-coverage gaps beyond the trailing-slash case (now closed) and
   `demo.py`'s CLI-level paths. The latter stay unaddressed by unit test, consistent with this
   project's standing convention that `demo.py` has no test file — but both paths flagged (the
   success path and the warm-failure path) were live-reproduced directly against the real
   `_build_sdk_policy()` and the real remote box, immediately above and in this section.

Final: 589 tests passing, ruff and mypy `--strict` clean, both fixes re-verified live against the
real box after the fix (correct URL for a trailing-slash base_url; graceful continuation with a
printed warning for an unreachable one).

### Unit 8 detail — a Critical finding that was invisible to my own test spec by construction

Direct successor to Unit 7 by explicit user instruction, given before any live test of the SDK
transports was allowed to happen: *"Build the Budget Governor first, no live test until then."*
Scope grew mid-flight, twice, on further explicit instruction — Ollama support ("with that in
mind, not within months" — i.e. now, not deferred) landed in the same unit as the governor
rather than its own.

Claude wrote 96 tests across 5 files (`test_budget_governor.py`, `test_budget_governed_transport.py`,
`test_agent_budget_integration.py`, plus additions to the three existing transport test files
and a new `test_agent_ollama_transport.py`) as the spec for: `BudgetGovernor` (rolling
tokens-per-minute window, NORMAL/DEGRADED/EXHAUSTED ladder, non-sticky recovery),
`GovernedTransport` (a transparent `IModelTransport` wrapper that feeds the governor as a side
effect — zero changes required to `SDKPolicy` or the three Unit 7 transports beyond one added
`last_usage` property each), `AgentLoop`'s new optional `governor=` hook, and `OllamaTransport`
as a thin subclass of `OpenAITransport` (Ollama's OpenAI-compatible endpoint means zero
duplicated request/response logic).

**Terra/high** implemented all of it in one pass (57,167 tok) — cheapest full-unit
implementation to date at this file count, credited to a genuinely well-decomposed, fully
test-first spec rather than anything about the tier. 96/96 targeted tests green, `ruff` and
`mypy --strict` clean. Terra's own full-suite run reported 4 failures; independent
re-verification (Claude) found all four were artifacts of running two pytest processes against
the same `.pytest-tmp` and my own concurrent profile.py edit landing mid-run — a clean
sequential re-run showed 548/548 passing, not a real defect. **Worth noting for the log: this
is the inverse of the usual failure mode here — not a green suite hiding a real bug, but a red
report hiding a non-bug.** Verify before trusting either direction.

**Claude caught one bug before sending anything for review**, by reading the diff rather than
trusting it: `_decide()` stamped `Observation.deadline_ms` from the constructor's *fixed*
`min_interval_ms`, not the governor's actual per-cycle interval — so a degraded cycle that
doubled its interval would still tell the model it had only the base amount of time left. The
model reasons about its remaining budget directly from that field (see
`model_schema.observation_to_prompt`), so this was a lie to the model, not a cosmetic stats gap.
Fixed by threading the real `interval_ms` into `_decide()`. Flagged for the reviewer as a
*class* of bug to hunt for elsewhere in the diff (a value computed once, used somewhere it
needed recomputing) — see finding 6 below, which is exactly that pattern recurring one layer up.

**Sol/high review** (19,505 tok) returned 11 ranked findings, headlined by one that says
something uncomfortable about test-first development itself:

1. **Critical, and invisible to my own 96 tests by construction, not by accident: a failed
   model response recorded zero tokens.** `GovernedTransport` only called `governor.record()`
   inside the success path — I had written `test_a_failing_call_records_nothing` *asserting*
   that as correct, reasoning "we don't know what a failed call cost, so don't guess." Sol's
   rebuttal: a response that fails LOCAL parsing (wrong tool, malformed JSON) was still
   generated and billed by the provider — and that failure mode is exactly what a small,
   possibly-tool-calling-unreliable local Ollama model is most likely to produce. The result:
   the one caller most likely to need the budget cap (an experimental local model) is exactly
   the one the governor would have been blind to, forever, at real cost. **My own test spec
   encoded the bug as a passing assertion.** Fixed by always recording — real usage on
   success, a conservative estimate on failure — and replacing that test with
   `test_a_failing_call_still_records_a_conservative_estimate`. A second, subtler bug
   surfaced while fixing this: naively reading the inner transport's `last_usage` after a
   failure would have double-counted a PRIOR successful call, since `last_usage` intentionally
   retains its last value across a failure. Added
   `test_a_failing_call_does_not_charge_a_stale_usage_from_a_prior_success` to pin that down
   too — never found by Sol, found by Claude while implementing Sol's fix.
2. **High, fixed:** the token estimate fallback counted only prompt characters and image
   count — a 20,000-character system prompt or a large tool schema would look almost free.
   `estimate_tokens` now includes system and schema length plus a non-zero output floor
   (a response that generated zero output tokens is not the realistic case a fallback exists
   to cover).
3. **High, documented as an accepted scope limit, not fixed:** budget admission isn't atomic
   — `decide()` is a snapshot, not a reservation, so two callers sharing one governor could
   both start expensive calls between a decision and the matching record. Real, but the only
   real caller (`AgentLoop._run`) never has two `propose()` calls in flight at once; true
   per-call admission control is a heavier mechanism than CARRYOVER asked for. Documented
   explicitly in `budget.py`'s module docstring as a single-caller assumption.
4. **High-when-shared/Low-otherwise, same documented limitation as 3:** `last_usage` is
   ambient mutable state on a transport instance; concurrent callers on the same instance
   could interleave. Same single-caller assumption, documented in the same place.
5. **High for non-default windows, dormant at the shipped default:** `tokens_per_minute_limit`
   was compared directly against the raw window sum regardless of `window_seconds` — a 30s
   window would silently admit twice the configured per-minute rate, a 120s window half of it.
   Every one of my 96 tests used the 60s default, so this was arithmetically invisible to the
   whole suite. Fixed by scaling the limit by `window_seconds / 60`; added
   `test_the_limit_scales_with_a_non_default_window_length` and a companion test for a longer
   window, specifically because the default-only test pattern is what let this one through.
6. **Medium, documented, not fixed:** cadence transitions can lag by up to one stale interval
   — `_run` re-evaluates `decide()` every iteration, but the current cycle's wait gate was
   already set from the previous cycle's interval. Bounded and non-unsafe (the deadman still
   governs the pad regardless of cognition's cadence); re-architecting `_run`'s wait loop to
   avoid it touches hardened, already-reviewed hot-path timing code for a cosmetic cadence lag,
   not a safety gap. Documented in `AgentLoop.__init__`'s docstring.
7. **Medium, documented, not fixed:** `AgentLoop`'s own `min_interval_ms`/`frames_per_observation`
   become dead configuration whenever a governor is supplied — nothing cross-checks them
   against the governor's own base values. Documented in the same docstring; the real fix is
   wiring discipline (derive both from one source when constructing them together in demo.py),
   not new validation code in an already-reviewed constructor.
8. **Medium, fixed:** a broken usage object or a broken governor could turn an otherwise-
   successful call into a raised exception, or mask a real failure's exception with an
   accounting one. `GovernedTransport._record` now swallows its own exceptions unconditionally
   — accounting is subordinate to the actual result, same principle `loop.py` already applies
   to the recorder ("a broken log must not kill cognition"). Added two tests: a broken
   governor doesn't fail a successful call, and doesn't mask a real failure either.
9. **Low–Medium, fixed:** `TokenUsage` rejected negative counts but accepted bools, floats,
   NaN, and infinity — a NaN entry makes every `<` comparison in `_decision()` false, which
   reads as permanently EXHAUSTED and never recovers even once it ages out of the window.
   Tightened to the same bool-before-int, `math.isfinite` pattern already used throughout
   `contracts.py`.
10. **Low for the real clock, Medium for an arbitrary one, documented:** `_prune` assumes
    append-order matches time-order, which any `Clock` satisfying this package's own monotonic
    contract guarantees (and the real `SessionClock` — `time.perf_counter()`, lock-serialized
    reads — genuinely does). One-line comment added; no code change, since enforcing it would
    duplicate a guarantee the `Clock` Protocol already makes.
11. **Low, resolved as a side effect of fixing 2:** `record(None)`'s fallback used to be
    `estimate_tokens(prompt_chars=0, image_count=0)` — one token, which reads as "this cost
    nothing" rather than "unknown." The non-zero output floor from fixing finding 2 already
    raises this to a more honestly conservative figure; no separate constant needed.

**New process lesson, the one worth keeping:** finding 1 was not a gap in coverage, it was a
test that *actively certified the bug as correct behavior*, written by the same mind that
designed the mechanism it was testing — the exact failure mode Unit 2's log first named
("tests written by the same mind that specified the design encode the same blind spots") but
sharper here, because this time the flawed reasoning was captured in the test's own docstring,
not just its absence. **Rule: when a reviewer's finding is "your test asserts the wrong thing,"
that is not a lower-severity version of "you missed a case" — treat it as evidence to
specifically re-examine every OTHER test whose docstring contains a confident justification,
because that confidence is exactly what stopped the author from spotting the same class of
error a second time** (finding 5's window-scaling bug is the same pattern: real, arithmetically
simple, and invisible to 96 tests because every one of them used the one default value that
made the bug dormant).

Final: 560 tests stable across 3 runs, `ruff` and `mypy --strict` clean.

#### Live verification, same day — the first real API call this project has ever made

Joey installed Ollama locally and pulled `smollm2:1.7b` unprompted, which forced the first
actual live test of the transport stack built in Units 7 and 8, sooner than planned. Two
findings, both from running the real thing rather than a fake client:

- **`smollm2:1.7b` (capabilities: `completion`, `tools` — no `vision`) declined the forced
  tool call outright.** A raw diagnostic call against Ollama's OpenAI-compatible endpoint
  showed the model returning plain text — *"The query cannot be answered with the provided
  tools."* — instead of a `tool_calls` entry, despite the endpoint's own capability listing
  claiming `tools` support and despite `tool_choice` being forced. `OllamaTransport` raised
  `ModelTransportError` correctly; nothing crashed, nothing hung. This is real evidence that
  the module docstring's "UNVERIFIED whether the default model honors forced tool_choice"
  caveat, written before any live call, was warranted — not hedging.
- **`moondream` (capabilities: `completion`, `vision` — no `tools`) confirmed a second gap:**
  the vision-capable model available in Ollama's library at this size does not advertise
  tool-calling support at all. Between the two models pulled on this machine, **no single
  local model currently does both halves of `propose()`'s job** — seeing the frame and
  returning a forced structured call. This is a real constraint on the "small, accept the
  slowness" Ollama experiment Joey asked for, not a bug in this project's code.
- **The Unit 8 fix for finding 1 held up live, not just in the 96 tests that exercise it in
  isolation.** The failed call above still left `governor.stats.tokens_in_window == 740` —
  `GovernedTransport` correctly recorded a conservative estimate despite the failure, exactly
  the property that finding closed. First time a Sol/high finding's fix has been confirmed
  against a real failure rather than only a fake one.

Decision: stop here for now rather than chase a third local model. The plumbing — Ollama
connectivity, `OllamaTransport`, `SDKPolicy`, `GovernedTransport`, the budget governor's
failure-path accounting — is now confirmed working end-to-end against a real server. What
remains unverified is narrower and more specific than before: not "does this whole stack
work," but "does any small local model reliably do forced tool-calling over vision input."
`ollama_transport.py`'s module docstring updated to record this rather than repeat the
now-partially-answered original caveat. The Anthropic and OpenAI transports remain entirely
unverified against real credentials — neither has been live-tested yet.

#### Live verification, 2026-08-13 — found one, and hit an unrelated platform bug on the way

Joey: *"I'm game, let's find a small model that does both."* Two things happened before a
working model did, neither of them a defect in this project:

- **Every new `ollama pull` from Ollama's own registry started failing with `401
  Unauthorized`,** for any model name tried, while the two models pulled two days earlier kept
  working fine. Traced to a known, unresolved, Windows-specific bug in Ollama itself
  (`ollama/ollama#15074`) — registry auth breaks after some local state change, `ollama
  signin`'s login flow doesn't recover it. Confirmed this was a platform issue and not a
  project one exactly the way PLATFORM-NOTES.md's own rule says to: read the other system's
  logs (here, its GitHub issue tracker) before touching ours. Workaround: `ollama pull
  hf.co/<org>/<repo>` pulls directly from Hugging Face, bypassing Ollama's registry — and its
  auth bug — entirely.
- **`hf.co/Qwen/Qwen3-VL-8B-Instruct-GGUF` (capabilities: vision AND tools) is the first model
  found that does both halves of `propose()`'s job**, confirmed against this project's actual
  production code path, not a simplified stand-in: a real `Frame`, the real
  `action_chunk_tool_schema()`, real `SDKPolicy.propose()`. First real attempt against that
  strict schema hit the 180s timeout entirely (model was warm, per `ollama ps` — this was
  genuine constrained-decoding slowness against a harder schema, not a cold-load artifact).
  Second attempt, timeout raised to 480s, returned in 65.1s but tripped one of
  `contracts.py`'s own business rules (`first keyframe must be at t_ms=0`) — correctly
  rejected as `ModelResponseError`, exactly the intended behavior for a malformed-but-honest
  proposal, not a transport failure. Third attempt succeeded completely: a valid `ActionChunk`
  back through the full stack in 25.4s once warm. **8B was needed, not chosen** — every
  smaller model tried (1B–1.7B) failed at either vision or tool-calling; "small" for this
  experiment turned out to mean "smallest model that actually does the job."
  `_DEFAULT_MODEL` in `ollama_transport.py` changed from `moondream` (confirmed unable to
  tool-call at all) to this model, now a confirmed-working default instead of a
  confirmed-broken one.

**Numbers worth keeping:** real token usage for the successful strict-schema call was 1,664
input + 81 output = 1,745 total — confirming the whole telemetry path (`OllamaTransport.
last_usage` → `GovernedTransport` → `BudgetGovernor`) reports real provider numbers live, not
just estimates, when a model supports it. Latency ranged from 25s (warm, clean pass) to over
180s (warm, but a harder schema) on this CPU-only Surface Laptop 4 — confirming CARRYOVER's
standing note that the "fast" policy loop's ~1.5s cadence is not remotely achievable with this
model on this hardware. Fine for the exploratory purpose Joey framed this as; not yet viable
for live gameplay.

### Unit 7 detail — reproducing a reviewer's claims instead of trusting the writeup

CARRYOVER.md's roadmap named this the next step in plain terms: replace the ad-hoc `codex
exec` calls used to *measure* transport cost with a real model-backed policy, built behind
`IFastPolicy`/`IDeliberativeModel` so a CLI fallback needs no API key. Claude wrote 74 tests
across five files first: `model_schema.py` (pure prompt/schema/decode logic, reusing
`contracts.chunk_from_dict` and `video.encode.{downscale,encode_jpeg}` rather than inventing a
second validator or a second image pipeline) and `model_policy.py` (a provider-neutral
`SDKPolicy` behind an injectable `IModelTransport` seam), plus three thin transports —
Anthropic, OpenAI (optional import, mirroring `voice`'s opt-in pattern), and a `codex exec`
CLI fallback. Every transport test uses a fake client or a fake process runner; none needs a
real API key or the `codex` binary, closing the same gap Unit 4 named — untestable-without-a-
seam glue — before it could open.

**Sol/ultra** implemented all five files in one pass (121,635 tok, 666s): 74/74 tests green,
`ruff` and `mypy --strict` clean, no deviations from spec. Independent re-verification (Claude,
not trusted from the implementer's own report) confirmed all of it and additionally caught two
things Sol/ultra's own report missed: a stale default model id (`claude-sonnet-4-5`, not this
account's actual `claude-sonnet-5`) and two lint failures — but both were in *Claude's own test
files* (an unused import, one over-length line), not in the generated code. First time this log
has recorded a lint slip in the test-authoring side rather than the implementation side.

**Sol/high** review (21,781 tok) returned 9 ranked findings, 2 critical, 3 high, 3 medium, 1
low. Two of them made falsifiable claims about real OS behavior rather than claims about the
diff's logic, and were reproduced directly instead of adjudicated from the writeup:

1. **Critical, confirmed by reproduction: Windows does not kill a subprocess's children.**
   `subprocess.run(timeout=...)`'s `TimeoutExpired` handling only sends `TerminateProcess` to
   the direct child. `codex.cmd` is an npm shim that spawns a real `node.exe` child, so a
   timed-out call could leave that process running past the deadline the caller thought it
   enforced. Reproduced with a throwaway parent/grandchild script before believing it: a bare
   kill left the grandchild alive; `taskkill /F /T /PID <parent>` killed the whole tree. Fixed
   by switching `cli_transport.py`'s default runner to `Popen` with a `taskkill`-based
   tree-kill fallback on timeout (POSIX path uses `os.killpg`).
2. **High, confirmed by inspection: no `cwd` meant codex could read the real working
   directory.** `-s read-only` permits reads and read-oriented tools; without an explicit
   `cwd`, a model that decided to look around before answering would find the actual repo, not
   nothing. This is not hypothetical here specifically because the caller's prompt is built
   from **live, attacker-reachable content** — screen text, scheduler event detail strings —
   so a crafted in-game message is a real (if narrow) prompt-injection surface. Fixed by
   confining every call to the same disposable per-call temp directory already used for images
   and the schema file; codex now has nothing to find but its own inputs.
3. **Critical, accepted without a repro (a design gap visible directly in the diff, not a claim
   about external behavior): unbounded SDK auto-retry can multiply the caller's timeout
   budget.** Fixed by setting `max_retries=0` on both default clients (Anthropic and OpenAI).
   The residual risk the reviewer named — a slow-trickling connection that never fails a
   single read but never finishes either — is real and is **not** fixed here; it is exactly
   the "degrade rather than hang" problem CARRYOVER already scopes to the next unit, the
   Budget Governor, and fixing a slice of it piecemeal here would fragment that design instead
   of anticipating it.
4. **High, accepted and deferred to the same Budget Governor unit:** `SDKPolicy` never shrinks
   `timeout_s` to the observation's remaining decision budget, and frame preprocessing
   (encode/downscale/base64) is unbounded in count and memory. Both are literally the shape of
   work CARRYOVER's roadmap already assigns to that unit ("lengthen the chunk horizon... drop
   frames per observation").
5. **Medium, fixed (cheap, mechanical, no repro needed):** OpenAI's transport had no output
   token cap, unlike Anthropic's sibling implementation — an inconsistency, not a hypothesis.
   Added a matching `max_completion_tokens` cap. Also made the `anthropic` import optional,
   matching the pattern the OpenAI transport already used, so a base install doesn't pay a hard
   `ModuleNotFoundError` for an unused provider.
6. **Medium, accepted-as-described but rejected as a fix target:** the module-level retained-
   temp-directory scheme in `cli_transport.py` has a real cross-call race under concurrency —
   but the only code that ever reads a call's directory *after* the call returns is test
   introspection. No production caller touches the filesystem path; every real caller only
   ever sees the already-parsed return value. Documented rather than "fixed," because there is
   no reachable failure to fix yet.
7. **Low, deferred:** a malformed Anthropic text block (`text` present but non-string) is
   silently dropped rather than raising. Correct as described, low consequence (worst case,
   `deliberate()` returns `None` instead of an error), left as-is.
8. **Rejected outright, and the reviewer said so themselves:** `CLITransport` ignoring
   `tool_name` and not validating the returned object's shape beyond "is a dict" looked
   concerning in isolation, but `SDKPolicy.propose()` immediately hands the result to
   `chunk_from_dict`, which already rejects `{}` or an unrelated object. The reviewer's own
   "Judgment on the specifically raised points" section correctly concluded this is not a
   controller-integrity defect given the actual call chain — a good example of a reviewer
   grading its own finding down once shown the caller, and being right to.

**New process lesson:** a described defect is a hypothesis about the world until it's checked
against the world. Two of nine findings here were claims about real OS process semantics, not
claims about the code's logic — and cost about ten minutes apiece to settle with a throwaway
script rather than being adjudicated from prose alone. Both held up exactly as described. A
third, equally severe finding (the timeout/deadline gap) needed no repro because it was a
design fact readable directly in the diff. **Rule: when a finding claims something about an
external system's behavior rather than about the diff's logic, reproduce it before adjudicating
— the five minutes it costs is cheap next to shipping a fix for a bug that doesn't exist, or
rejecting one that does.**

Final: 489 tests stable across 3 runs, `ruff` and `mypy --strict` clean.

### Units 5+6 detail — the first ultra run, and a design error of Claude's

Combined at Joey's suggestion, correctly: the recorder writes frames to disk and the agent
hands frames to a model, and those are the same operation. Building them separately would have
guaranteed the two drift.

Claude wrote 64 tests as the spec. **Sol/ultra** implemented all six modules across two
packages in a single pass — 360 tests green, `mypy --strict` and `ruff` clean. As an
implementer of a large, well-specified, multi-file unit it was genuinely impressive: coherent
design, house style matched, no scope creep.

**Sol/high review then found 15 defects, 11 critical** (91,148 tok). All accepted; 13 fixed,
2 recorded as deliberate known limitations rather than half-fixed (no `fsync` durability
against host power loss; recorder admission takes a short lock, so "non-blocking" means
bounded rather than lock-free).

**The most important finding was a design error of Claude's, not Sol/ultra's.** Claude decided
the agent loop should be the sole drainer of scheduler events — correctly, since
`drain_events()` clears as it reads and two consumers would each get half. But the loop also
called `policy.propose()` on that same thread. So a hung model silently stopped takeovers,
deadman fires and rejections from ever reaching the recorder; they accumulated in a bounded
deque and the oldest were overwritten unseen. **The witness had been coupled to the slowest
component in the system.** Sol/ultra implemented the specified design faithfully; the spec was
wrong.

Two further defects came from **running the thing**, not from any review:

- A 12-second session wrote **1.8GB** of lossless PNG — 540GB/hour. The fix turned out to be
  principled rather than merely pragmatic: the model is handed downscaled JPEG, so lossless
  full-resolution frames record something the policy never saw. Same run now costs 43.6MB, a
  41x reduction, *and* is a better dataset.
- `frames_dropped` counts ring evictions, not frames lost to the consumer. A live run reported
  219 "dropped" while recording 281 of 283 frames with zero gaps. A metric whose name implies
  damage that did not occur is its own kind of corruption.

### Unit 4 detail — when the reviewer indicts the test strategy

Claude wrote 30 tests; Terra/high implemented (72,951 tok / 474s); everything passed, `mypy
--strict` and `ruff` clean, and live capture worked against a real game window.

Sol/high review (72,048 tok / 465s) returned **13 defects, 7 critical**, all accepted. The four
that would have silently corrupted the demonstration dataset — the failure mode that matters
here, because a crash is recoverable and bad data is not:

1. **A rejected timestamp poisoned the next one.** `_previous_derived_source_ms` was updated
   *before* the plausibility check, so after one capture-clock reset the following frame was
   validated against the value that had just been rejected, passed, and was recorded as
   trustworthy while being about a second wrong. One reset produced a single `None` and then a
   confidently misaligned timeline. This is exactly the "plausible-looking lie" the design
   explicitly forbade, implemented anyway.
2. **Callback failures created unlabelled gaps** — no drop counted, no sequence hole, so the
   next good frame falsely claimed uninterrupted capture.
3. **Consumers shared writable numpy arrays.** The model normalising in place would rewrite
   what the recorder serialises. Freezing a dataclass does not freeze the array inside it.
4. **No generation token**, so a straggler callback from a stopped session could be accepted
   into a new one and seed its timestamp anchor from the old capture clock.

Plus: session time not enforced monotonic, a failed `start()` leaving committed frames, the
user clock being called while holding the state lock (a clock that reads `source.stats`
deadlocks the capture thread), `latest()` copying the whole ring under lock, unbounded
`wait()` on shutdown, and a spontaneous close leaking a capture session.

**But the most valuable thing Sol said was not a defect.** It closed with: *"the real backend
has only live smoke coverage; none of the failure, clock, or lifecycle interleavings above are
exercised."* That is an indictment of the test strategy, not the code — and it was right. Every
one of the 13 lived behind a graphics API that could only be smoke-tested, so none of them were
reachable by any test. The fix was not 13 patches, it was **a seam**: an injectable backend
factory, after which 20 new tests drive the callbacks directly — delivering frames, raising
from them, closing spontaneously, restarting — with no graphics stack involved.

Terra also caught a real bug in Claude's new tests: `FakeFrame(width=-5)` allocated
`bytearray(-80)` and raised inside the fixture, so the rejection path it claimed to test was
never reached. Second time a Codex tier has found a broken test of Claude's.

Final: 296 tests stable over 3 runs, clean lint and types, and live capture of a real game at
1716x1316 with zero drops. The timestamp work visibly paid off in that capture: arrival deltas
were 31.5/49.8/33.0/35.8/47.9ms while acquisition deltas were 50.0/50.0/33.3/33.4/50.0ms —
quantised to the compositor's cadence. Recording both is what makes that difference visible
instead of invisible.

### Unit 2 detail — the most important data point so far

Claude wrote 45 tests as the spec. Terra/high implemented against them (47,318 tok / 230s) and
passed **194/194**, with `mypy --strict` and `ruff` clean. By every automated signal the unit was
finished.

Sol/high review (43,079 tok / 397s) then found **9 defects, 4 of them critical**, all sharing one
failure mode: *the pad ends up stuck holding an input and nothing can release it.* In a live game
that is not a hung process, it is a character sprinting into a lake while the model happily
proposes new plans that never execute.

The four criticals:
1. **I/O under the lock.** `tick()` held the state lock across `output.set_state()`. A blocking
   device driver would therefore also block `take_controller()`, `close()`, and even reading
   `.epoch` — and `stop()` joins the wedged thread *before* attempting release. Every escape
   route ran through the jam.
2. **`take_controller()` never actually neutralised.** It mutated memory and waited for the next
   tick. If the loop was late, stopped, or dead, the stick stayed down.
3. **`close()` set `_closed = True` before the neutral write**, so a single failed write made
   every subsequent `close()` a no-op and the pad was never released.
4. **`except Exception` in the thread loop** missed `SystemExit`/`KeyboardInterrupt`, and release
   was not in a `finally`.

Plus: a start/stop race that could strand a running loop, a single-clock violation (the thread
created its own `SessionClock`), a NaN clock silently disabling the deadman forever
(`now > deadline` is False against NaN), unbounded event and pending queues, and `submit()`
re-reading an untrusted `duration_ms` property after validation.

**All 9 accepted**, two with different fixes than proposed. Claude contributed a 10th finding —
a predicted self-deadlock in `submit()`'s failure path — which was **wrong**: `with` releases the
lock via `__exit__` as the exception propagates, before the enclosing `except` runs. The test was
kept as a regression guard, labelled as such, because the reasoning only holds for `with`.

Sol also caught a **fake test of Claude's**: `test_neutralises_immediately` called `tick()` before
asserting, so it only ever proved "neutral eventually" — and the implementation obligingly
provided only that. A test that cannot fail for the reason it claims to check is worse than no
test, because it consumes the budget of attention that a real test would have earned.

Final: 210 tests, stable across 5 consecutive runs, `mypy --strict` and `ruff` clean. Virtual pad
confirmed live on XInput slot 0.

### Unit 1 detail

Claude wrote 94 tests as the spec; Luna/medium implemented against them (32,397 tok / 174s),
passing 93. The single failure was a bug in **Claude's** test, which Luna correctly identified and
declined to "fix" by editing the spec.

Terra/high review (17,209 tok / 177s) returned 11 ranked findings. Adjudication: **9 accepted, 2
rejected.**

Accepted, in rough order of how badly they would have bitten:
1. `{"buttons": ""}` decoded to `frozenset()` — a malformed payload silently became the genuine
   controller command *release all buttons*.
2. `frozen=True` does not deep-freeze: `PadState(left_stick=[0.0, 0.0])` stayed aliased to the
   caller's list, so a validated state could be mutated out of range afterwards.
3. Undeclared exceptions escaping validation (`TypeError` from `math.isfinite("0")`,
   `OverflowError` from `10**400`) — these kill the scheduler thread, which is the only thing
   that can release the pad.
4. `bool` accepted wherever a number was expected, so JSON `true` became a full trigger pull.
5. No bound on keyframe count or chunk duration — an unbounded chunk is a DoS against a thread
   that must not miss an 8.3ms deadline.
6. `ManualClock.advance(nan)` passed the `< 0` check and then poisoned every derived deadline.
7. Non-deterministic button ordering in the JSON codec (frozenset iteration + hash randomization),
   which would make recorded sessions non-comparable across runs.
8. Deserialization did not validate at ingress.
9. `Frame.pixel_format` type unchecked.

Rejected:
- *"`sample()` applies the first keyframe before its timestamp"* — specified behaviour, correctly
  implemented. But it pointed at a real ambiguity, so it was fixed a different way: chunks must
  now start at `t_ms=0`, which deletes the ambiguous region instead of picking a rule for it.
- *"Trailing presses are never pulse-validated"* — also specified. `contracts` cannot know when
  preemption will arrive, so this is unenforceable here. **Carried forward to Unit 2** as a
  scheduler minimum-dwell question.

Claude then found a 12th defect by re-reading the fix diff: `chunk_from_dict` caught `InvalidChunk`
and re-raised it as a generic `"malformed chunk payload"`, destroying the specific reason. That
message is the feedback signal a model uses to correct its next chunk, so the causes were split
into distinct messages.

Final: 149 tests passing, `ruff` clean, `mypy --strict` clean, plus an independent adversarial
probe run outside the test suite to confirm each accepted finding was genuinely closed.
