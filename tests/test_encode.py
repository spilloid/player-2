"""Contract for player2.video.encode -- turning captured pixels into bytes.

This is the seam that makes the recorder and the agent one unit of work rather than two.
Both need the same operation: a BGRA capture buffer becomes a compressed image. The
recorder writes it to disk for a dataset; the agent hands it to a model. Building that
twice would guarantee the two drift, and a dataset whose frames do not match what the model
actually saw is worthless for training a policy on its own decisions.

The failure mode worth naming: BGRA and RGB differ only by channel order, so getting it
wrong produces a perfectly valid image with the red and blue channels swapped. Nothing
crashes. The model sees blue grass and orange sky, reasons sensibly about the wrong world,
and the recording preserves the error faithfully forever.
"""

from __future__ import annotations

import numpy as np
import pytest

from player2.contracts import Frame
from player2.video.encode import downscale, encode_jpeg, encode_png, to_rgb


def solid(width: int, height: int, bgra: tuple[int, int, int, int]) -> Frame:
    """A frame of one flat colour, given in BGRA order as the capture API delivers it."""
    buffer = np.zeros((height, width, 4), dtype=np.uint8)
    buffer[:, :] = bgra
    buffer.flags.writeable = False
    return Frame(seq=0, session_ms=0.0, source_ms=None, width=width, height=height,
                 pixel_format="BGRA8", data=buffer)


PURE_RED_BGRA = (0, 0, 255, 255)
PURE_BLUE_BGRA = (255, 0, 0, 255)


class TestChannelOrder:
    def test_bgra_red_becomes_rgb_red(self) -> None:
        """The whole point. If this is wrong everything downstream still 'works'."""
        rgb = to_rgb(solid(4, 4, PURE_RED_BGRA))
        assert tuple(rgb[0, 0]) == (255, 0, 0)

    def test_bgra_blue_becomes_rgb_blue(self) -> None:
        rgb = to_rgb(solid(4, 4, PURE_BLUE_BGRA))
        assert tuple(rgb[0, 0]) == (0, 0, 255)

    def test_alpha_is_dropped(self) -> None:
        rgb = to_rgb(solid(4, 4, PURE_RED_BGRA))
        assert rgb.shape == (4, 4, 3)

    def test_does_not_mutate_the_source_frame(self) -> None:
        """Capture hands out read-only buffers precisely so two consumers cannot corrupt
        each other. An encoder that writes in place would defeat that."""
        frame = solid(4, 4, PURE_RED_BGRA)
        to_rgb(frame)
        assert tuple(np.asarray(frame.data)[0, 0]) == PURE_RED_BGRA


class TestDownscale:
    def test_preserves_aspect_ratio(self) -> None:
        out = downscale(solid(1600, 900, PURE_RED_BGRA), max_dim=800)
        assert out.width == 800
        assert out.height == 450

    def test_scales_by_the_longest_edge(self) -> None:
        out = downscale(solid(900, 1600, PURE_RED_BGRA), max_dim=800)
        assert out.height == 800
        assert out.width == 450

    def test_never_upscales(self) -> None:
        """Inventing pixels wastes tokens on a model and inflates a dataset with detail
        that was never captured."""
        out = downscale(solid(320, 200, PURE_RED_BGRA), max_dim=4096)
        assert (out.width, out.height) == (320, 200)

    def test_keeps_frame_provenance(self) -> None:
        """A resized frame is still the same observation. Losing its identity or timestamps
        would break alignment with everything else recorded at that instant."""
        original = Frame(seq=7, session_ms=123.5, source_ms=120.0, width=1600, height=900,
                         pixel_format="BGRA8", data=np.zeros((900, 1600, 4), dtype=np.uint8))
        out = downscale(original, max_dim=400)
        assert (out.seq, out.session_ms, out.source_ms) == (7, 123.5, 120.0)

    def test_rejects_a_useless_max_dim(self) -> None:
        for bad in (0, -1):
            with pytest.raises(ValueError):
                downscale(solid(64, 64, PURE_RED_BGRA), max_dim=bad)

    def test_output_is_still_readable_as_pixels(self) -> None:
        out = downscale(solid(800, 600, PURE_RED_BGRA), max_dim=100)
        assert np.asarray(out.data).shape[:2] == (out.height, out.width)


class TestEncoding:
    def test_png_round_trips_colour(self) -> None:
        import cv2

        blob = encode_png(solid(8, 8, PURE_RED_BGRA))
        decoded = cv2.imdecode(np.frombuffer(blob, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert tuple(decoded[0, 0]) == PURE_RED_BGRA[:3]  # cv2 decodes to BGR

    def test_png_is_lossless(self) -> None:
        """The dataset keeps what was actually on screen. Lossy artefacts would become
        features a policy could learn from and then fail on with a different encoder."""
        import cv2

        frame = solid(16, 16, (10, 20, 30, 255))
        decoded = cv2.imdecode(np.frombuffer(encode_png(frame), dtype=np.uint8),
                               cv2.IMREAD_COLOR)
        assert tuple(decoded[5, 5]) == (10, 20, 30)

    def test_jpeg_is_much_smaller_than_png(self) -> None:
        """Which is why the model path uses it: tokens and latency, not fidelity."""
        frame = solid(256, 256, (10, 20, 30, 255))
        assert len(encode_jpeg(frame, quality=70)) < len(encode_png(frame))

    def test_jpeg_quality_is_honoured(self) -> None:
        frame = solid(256, 256, (10, 20, 30, 255))
        assert len(encode_jpeg(frame, quality=20)) < len(encode_jpeg(frame, quality=95))

    def test_encoding_is_deterministic(self) -> None:
        """Recordings must be byte-comparable across runs to be hashable and de-duplicable."""
        frame = solid(32, 32, (1, 2, 3, 255))
        assert encode_png(frame) == encode_png(frame)

    def test_returns_bytes(self) -> None:
        frame = solid(8, 8, PURE_RED_BGRA)
        assert isinstance(encode_png(frame), bytes)
        assert isinstance(encode_jpeg(frame, quality=80), bytes)

    def test_rejects_an_unusable_frame(self) -> None:
        bad = Frame(seq=0, session_ms=0.0, source_ms=None, width=4, height=4,
                    pixel_format="BGRA8", data=b"not pixels")
        with pytest.raises(ValueError):
            encode_png(bad)
