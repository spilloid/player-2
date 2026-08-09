# Frame storage: where this goes next

Two questions came up after the first real recordings, both worth answering properly because
they change the shape of the dataset rather than just its size.

## Where we are

| Encoding | Per frame | 1 hour of play |
|---|---|---|
| Lossless PNG, full resolution (first attempt) | ~6.5 MB | ~540 GB |
| Downscaled JPEG (current default) | ~207 KB | ~16 GB |

The current default is not merely cheaper, it is more correct: the model is handed downscaled
JPEG, so storing lossless full-resolution frames records something the policy never saw.

16 GB/hour is still too much to keep casually.

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

## Order of work

1. Content-hash dedup — small, no dependencies, fixes the pathological case immediately.
2. Shared encode cache — correctness win, makes the dataset provably match the model's input.
3. Segmented hardware-accelerated video — the real 10x, and the point at which recording every
   session by default stops being an imposition.
