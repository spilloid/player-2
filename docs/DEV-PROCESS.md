# Dev Process Log

This file records every adversarial review round between the lead engineer (Claude) and the consultant (Codex), and exists to derive a routing policy from our own measurements rather than vendor benchmarks.

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
