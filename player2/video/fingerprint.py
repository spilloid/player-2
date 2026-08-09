"""Fast frame fingerprints for exact storage and model-decision gating."""

from __future__ import annotations

import hashlib

import cv2  # type: ignore[import-untyped, unused-ignore]
import numpy as np  # type: ignore[import-untyped, unused-ignore]

from player2.contracts import Frame
from player2.video.encode import downscale, to_rgb


def _pixel_array(frame: Frame) -> np.ndarray:
    """Return validated array pixels; byte buffers are not a fingerprint input."""
    if not isinstance(frame.data, np.ndarray):
        raise ValueError("frame data must be a numpy pixel array")
    # to_rgb owns the BGRA validation and keeps this module consistent with encoding.
    to_rgb(frame)
    return frame.data


def content_hash(frame: Frame) -> str:
    """Hash only the exact pixels, so capture metadata cannot defeat deduplication."""
    pixels = _pixel_array(frame)
    contiguous = np.ascontiguousarray(pixels)
    return hashlib.blake2b(contiguous.tobytes(), digest_size=16).hexdigest()


def perceptual_hash(frame: Frame) -> int:
    """Return a cheap dHash; a too-sensitive gate spends real remote-model money."""
    # Limiting the source size bounds work for large captures while retaining scene detail.
    small = downscale(frame, 64)
    rgb = to_rgb(small)
    try:
        resized: np.ndarray = cv2.resize(rgb, (9, 8), interpolation=cv2.INTER_AREA)
        grey: np.ndarray = cv2.cvtColor(resized, cv2.COLOR_RGB2GRAY)
    except cv2.error as error:
        raise ValueError(f"could not compute perceptual hash: {error}") from error
    comparisons = grey[:, :-1] > grey[:, 1:]
    bits = np.packbits(comparisons, axis=1, bitorder="big")
    return int.from_bytes(bits.tobytes(), byteorder="big")


def distance(a: int, b: int) -> int:
    """Return the Hamming distance between two 64-bit perceptual hashes."""
    return (a ^ b).bit_count()


def has_changed(previous: int | None, current: int, threshold: int = 10) -> bool:
    """Decide whether a frame merits a remote decision under the chosen sensitivity."""
    if previous is None:
        return True
    return distance(previous, current) > threshold
