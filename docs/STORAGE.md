# Frame storage: where this goes next

Two questions came up after the first real recordings, both worth answering properly because
they change the shape of the dataset rather than just its size.

## Where we are

Measured on comparable ~10 second live sessions:

| Stage | Session size | Per frame |
|---|---|---|
| Lossless PNG, full resolution (first attempt) | 1.8 GB | ~6.5 MB |
| Downscaled JPEG (default since) | 43.6 MB | ~207 KB |
| Content-hash de-duplication | **2.7 MB** | 13 unique images for 95 frames |

The JPEG default is not merely cheaper, it is more correct: the model is handed downscaled
JPEG, so storing lossless full-resolution frames records something the policy never saw.

**Caveat on the dedup number.** That session had the game paused behind a menu, so it is the
best case rather than the typical one. Real gameplay will dedupe far less. Two things hold
regardless: the pathological case now costs almost nothing, and the *ratio itself is a
diagnostic* — 95 frames collapsing to 13 unique images is the system telling you nothing
happened during that recording. We had captured a paused game twice without noticing until
the hash said so.

## Live gameplay, measured — and it deflates most of the above

45 seconds of the agent walking an expanding spiral through real terrain:

| | Paused session | **Live gameplay** |
|---|---|---|
| Unique frames | 13 / 95 (14%) | **266 / 266 (100%)** |
| Adjacent perceptual distance | 0 (p50, p90, max) | **p50 9, p90 14, max 19** |

**De-duplication saves nothing on live gameplay.** Every frame differs. The 93% figure was
entirely an artefact of recording a paused game. Dedup remains worth having — it makes the
idle case free and the ratio is a genuine did-anything-happen diagnostic — but it is not a
storage strategy for real play. Segmented video still is.

The change gate is similarly deflated, and the honest version is:

| Threshold | Frames passing |
|---|---|
| 8 | 70% |
| 10 | 58% |
| 16 | 39% |
| 24 | 21% |

A 2–5x reduction, not the 200x the paused measurement implied. Note also that the maximum
adjacent distance over the whole session was 19 out of 64 bits, so the useful threshold band
is roughly 8–16; anything above ~20 never fires at all.

### The lever is decision cadence, not frame count

The gate figures above are per *frame*, which is the wrong denominator. Frames arrive at
~30fps; the loop decides every 700ms. Over 45 seconds that is about **64 decisions**, not 274.
Cost follows the decisions:

| Configuration | 45s of play | Per minute |
|---|---|---|
| `codex exec`, decide every 700ms | ~800k tokens | ~1.07M |
| Direct SDK, decide every 700ms | ~77k | ~103k |
| Direct SDK + gate at 16 | ~30k | ~40k |
| Direct SDK + gate at 16 + 1.5s cadence | ~14k | ~19k |

So the ordering of wins is: **transport (10x) > cadence (2x) > gate (2.5x)**, and they
multiply. The gate is real but it is the smallest of the three, which is worth knowing before
building anything clever on top of it.

## The same hash is also the spend gate

`content_hash` de-duplicates storage; `perceptual_hash` answers "has anything changed?" for
about 0.1ms and zero tokens. That second question is worth real money: a measured remote
decision through `codex exec` costs **~12,500 tokens** (see the transport note below), so a
static screen that triggers a decision every frame is pure waste.

On the paused session, gating decisions on perceptual change took 215 potential decisions
down to 1 — roughly 2,688k tokens down to 12k. Again a best case, and again the point stands:
the runtime should never pay to be told that nothing moved.

One mechanism, two consumers, which is why it was built as its own module rather than inside
either of them.

## Transport, measured

Before optimising what we send, it was worth measuring what it costs to send anything.
Luna via `codex exec`, one action-chunk decision, structured output:

| Input | Latency | Tokens |
|---|---|---|
| no image, one-line prompt | 6.5 s | **12,507** |
| 384px frame | 4.9 s | 11,436 |
| 768px frame | 6.7 s | 13,187 |
| 1280px frame | 5.9 s | 12,817 |

**The image is nearly free; the transport is not.** The ~12.5k floor is `codex exec` shipping
a full coding-agent harness — system instructions, tool definitions, session scaffolding — on
every invocation. One decision every two seconds would cost ~1.9M tokens for five minutes of
play.

The conclusion is not that the model is too expensive but that the *harness* is: a direct SDK
call with a minimal prompt should be roughly 800–1,500 tokens including a small frame. That is
a 10–15x reduction from changing transport alone, before the gate above is applied.

## 1. Store the differential, not the frame

**Yes — and the right form of it is a video codec, not a hand-rolled delta.**

A game screen is a video stream, and inter-frame compression is exactly the thing video
codecs already do: P-frames encode only what changed since the last frame. Hand-rolling
differencing might win 5–10x. H.264 on game footage routinely wins 50–200x, because it also
does motion compensation, which is most of what changes between two frames of a game.

Rough target: **~10–20 KB per frame equivalent**, so roughly 1–2 GB/hour rather than 16.

Three things it costs, and what each needs:

- **Random access.** A dataset wants "the frame at t=X" cheaply, and video needs to seek to a
  keyframe and decode forward. Fixed by forcing keyframes every 1–2 seconds, at some size
  cost. This is the main reason not to just crank the GOP length.
- **Crash tolerance.** The current append-only JSONL survives a kill; a video container being
  written when the process dies can be unplayable. Fixed by writing **bounded segments**
  (say 10s each) rather than one long file — which also satisfies the segmented-storage
  finding deferred back in the Unit 1 review.
- **Encode cost.** Software H.264 at capture resolution would eat CPU the runtime needs. On
  this hardware that is a solved problem: **Intel Quick Sync** on the Iris Xe does H.264/HEVC
  in fixed-function silicon, so the encode is close to free and does not compete with the
  scheduler or the capture callback.

Crucially, **the index stays JSONL.** `frames.jsonl` maps `seq -> (segment, offset)` instead
of `seq -> path`, and everything else about alignment, gap records, and crash tolerance is
unchanged. That the storage backend can be swapped without touching the alignment contract is
a consequence of having kept the index separate from the pixels from the start.

### The cheap intermediate that is worth doing first

**Content-hash deduplication.** The very first live recording wrote 281 *byte-identical*
frames, because the game was paused behind a menu. Hashing each encoded frame and storing
identical content once turns that session into one image and 281 index rows.

It is strictly weaker than video for the general case and strictly better for the pathological
one, it needs no new dependency, and the index already exists to point at it. It also makes a
real failure visible: a run that dedupes to almost nothing means the game was paused and the
"recording" contains no gameplay.

## 2. Flyweight the frames handed to the model

**Yes, and this is a correctness argument before it is a performance one.**

Today the recorder encodes a frame for disk, and the agent path would encode the same frame
again for the model. Two encodes of the same pixels, producing two artifacts that are only
*probably* identical — different quality settings, different downscale, a library upgrade, and
they diverge silently.

Encode once and share the immutable result, and the dataset stops merely *resembling* what the
model saw and starts literally containing the same bytes. That is the strongest possible
version of the argument for a shared encoder, which is why the encoder was made shared in the
first place.

This is safe to memoise precisely because of decisions already made:

- `Frame` is a frozen dataclass.
- Captured pixel buffers are marked read-only at the capture boundary, so no consumer can
  mutate a frame after it has been encoded.

So an encoded artifact keyed by `(seq, format, max_dim)` can be cached in a small bounded LRU
and handed to both consumers. Bounded, because an unbounded cache in the process that owns the
runtime's eyes is a slow memory leak that ends with the OOM killer taking out the thing that
holds the controller.

## Open defect: the recorder only sees what the agent looked at

The same live run captured **1,170 frames and recorded 266** — about 23%. The recorder is fed
by `AgentLoop._record_new_frames`, which forwards whatever the loop happened to poll, so the
dataset contains the agent's *sampling* of the screen rather than the screen.

For "what did the model see when it decided" that is arguably correct. For a demonstration
dataset meant to train a motor policy, losing three quarters of the visual timeline is not.
The fix is for the recorder to pump the video source itself on its own cadence, independent of
the decision loop — at which point the leading/trailing gap findings from the review get
resolved by the same change, since the recorder would own the frame stream end to end.

Filed rather than fixed, because it changes who owns frame delivery and deserves its own unit.

## Order of work

1. ~~Content-hash dedup~~ — **done.** 43.6MB → 2.7MB on a paused session; ratio doubles as a
   did-anything-happen diagnostic.
2. ~~Shared encode cache~~ — **done.** `EncodeCache`, keyed on content rather than sequence
   number, bounded LRU, thread-safe.
3. ~~Perceptual-hash change gate~~ — **built**, not yet wired into the agent loop.
4. Wire the gate into `AgentLoop` and measure the call-volume reduction on *live gameplay*
   rather than on a paused screen.
5. Direct SDK transport plus a **budget governor** — a tokens-per-minute cap that degrades
   gracefully (longer chunk horizon, higher change threshold, fewer frames per observation,
   and finally handing control back with an explicit event) rather than stopping dead. It
   belongs conceptually next to the deadman: both are "fail safe when a resource runs out".
6. Segmented hardware-accelerated video — the remaining 10x, and the point at which recording
   every session by default stops being an imposition.
