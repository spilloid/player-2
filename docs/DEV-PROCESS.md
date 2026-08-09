# Dev Process Log

This file records every adversarial review round between the lead engineer (Claude) and the consultant (Codex), and exists to derive a routing policy from our own measurements rather than vendor benchmarks.

## Findings

_No data yet. First revision after Unit 5._

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
