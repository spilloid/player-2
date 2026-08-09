"""Contract for player2.video.fingerprint -- asking "has anything changed?" for free.

One mechanism, two payoffs, which is why it is worth building carefully:

  * The recorder uses an EXACT content hash to store identical frames once. The first live
    session wrote 281 byte-identical frames because the game sat behind a menu.
  * The agent loop uses a PERCEPTUAL hash to decide whether a decision is even needed. A
    static screen should cost zero tokens, and a measured remote call costs ~12,500 of them.

The second use is the expensive one to get wrong, in both directions. Too sensitive and we
pay for a decision every time a cloud drifts across the screen. Too coarse and the agent
walks into a lake without noticing the lake. So the tests below pin the boundary explicitly
rather than trusting a threshold that "looks about right".
"""

from __future__ import annotations

import numpy as np
import pytest

from player2.contracts import Frame
from player2.video.fingerprint import (
    content_hash,
    distance,
    has_changed,
    perceptual_hash,
)


def frame_from(array: np.ndarray, seq: int = 0) -> Frame:
    array = np.ascontiguousarray(array)
    return Frame(seq=seq, session_ms=float(seq), source_ms=None,
                 width=array.shape[1], height=array.shape[0],
                 pixel_format="BGRA8", data=array)


def noise(width: int = 64, height: int = 64, seed: int = 0) -> Frame:
    rng = np.random.default_rng(seed)
    return frame_from(rng.integers(0, 255, (height, width, 4), dtype=np.uint8))


def solid(value: int = 30, width: int = 64, height: int = 64) -> Frame:
    array = np.full((height, width, 4), value, dtype=np.uint8)
    array[:, :, 3] = 255
    return frame_from(array)


class TestContentHash:
    def test_identical_pixels_hash_identically(self) -> None:
        assert content_hash(solid()) == content_hash(solid())

    def test_different_pixels_hash_differently(self) -> None:
        assert content_hash(solid(30)) != content_hash(solid(31))

    def test_is_independent_of_frame_metadata(self) -> None:
        """Two captures of an unchanged screen differ in seq and timestamp but are the same
        image. Hashing the metadata would defeat the entire purpose."""
        first = solid()
        second = Frame(seq=99, session_ms=12345.0, source_ms=1.0, width=first.width,
                       height=first.height, pixel_format="BGRA8", data=first.data)
        assert content_hash(first) == content_hash(second)

    def test_is_stable_across_calls(self) -> None:
        frame = noise()
        assert content_hash(frame) == content_hash(frame)

    def test_returns_a_short_hex_string(self) -> None:
        """Goes in every row of frames.jsonl, so it should not dominate the index."""
        value = content_hash(solid())
        assert isinstance(value, str)
        assert 16 <= len(value) <= 64
        int(value, 16)


class TestPerceptualHash:
    def test_identical_frames_have_zero_distance(self) -> None:
        assert distance(perceptual_hash(solid()), perceptual_hash(solid())) == 0

    def test_a_tiny_change_stays_close(self) -> None:
        """A few pixels of drift is not a new scene. Treating it as one is how a static
        screen ends up costing a decision every frame."""
        base = np.full((64, 64, 4), 30, dtype=np.uint8)
        base[:, :, 3] = 255
        nudged = base.copy()
        nudged[0, 0] = (200, 200, 200, 255)
        assert distance(perceptual_hash(frame_from(base)),
                        perceptual_hash(frame_from(nudged))) <= 4

    def test_a_different_scene_is_far_away(self) -> None:
        assert distance(perceptual_hash(noise(seed=1)),
                        perceptual_hash(noise(seed=2))) > 8

    def test_is_insensitive_to_scale(self) -> None:
        """The same screen captured at two resolutions is the same screen. Otherwise a
        window resize would read as a scene change forever after."""
        rng = np.random.default_rng(7)
        big = rng.integers(0, 255, (256, 256, 4), dtype=np.uint8)
        import cv2

        small = cv2.resize(big, (64, 64), interpolation=cv2.INTER_AREA)
        assert distance(perceptual_hash(frame_from(big)),
                        perceptual_hash(frame_from(small))) <= 8

    def test_returns_an_integer_hash(self) -> None:
        assert isinstance(perceptual_hash(solid()), int)

    def test_distance_is_symmetric_and_zero_on_self(self) -> None:
        a, b = perceptual_hash(noise(seed=3)), perceptual_hash(noise(seed=4))
        assert distance(a, b) == distance(b, a)
        assert distance(a, a) == 0


class TestHasChanged:
    """The gate itself. This is the function that decides whether to spend money."""

    def test_an_unchanged_screen_does_not_trigger(self) -> None:
        assert has_changed(perceptual_hash(solid()), perceptual_hash(solid())) is False

    def test_a_new_scene_triggers(self) -> None:
        assert has_changed(perceptual_hash(noise(seed=1)),
                           perceptual_hash(noise(seed=2))) is True

    def test_no_previous_hash_always_triggers(self) -> None:
        """The first observation of a session must always produce a decision; there is
        nothing to compare it against and 'unchanged' would mean 'never act'."""
        assert has_changed(None, perceptual_hash(solid())) is True

    def test_threshold_is_configurable(self) -> None:
        """Different games move at different rates. The runtime carries the number; the
        profile decides it."""
        a, b = perceptual_hash(noise(seed=1)), perceptual_hash(noise(seed=2))
        assert has_changed(a, b, threshold=64) is False
        assert has_changed(a, b, threshold=0) is True


class TestCostOfTheGate:
    def test_hashing_is_cheap_enough_to_run_on_every_frame(self) -> None:
        """The gate has to be far cheaper than what it is gating. A remote decision costs
        about 12,500 tokens and several seconds; this must cost microseconds."""
        import time

        frame = noise(width=512, height=512, seed=11)
        perceptual_hash(frame)  # warm any lazy imports
        start = time.perf_counter()
        for _ in range(50):
            perceptual_hash(frame)
        elapsed_ms = (time.perf_counter() - start) * 1000.0 / 50
        assert elapsed_ms < 5.0, f"perceptual hash took {elapsed_ms:.2f}ms per frame"


class TestRejectsUnusableFrames:
    @pytest.mark.parametrize("fn", [content_hash, perceptual_hash])
    def test_a_non_pixel_buffer_is_rejected(self, fn: object) -> None:
        bad = Frame(seq=0, session_ms=0.0, source_ms=None, width=4, height=4,
                    pixel_format="BGRA8", data=b"not pixels")
        with pytest.raises(ValueError):
            fn(bad)  # type: ignore[operator]
