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

_Provisional after 2 units. Do not trust these yet; first real revision after Unit 5._

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
  their authors cannot.

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
