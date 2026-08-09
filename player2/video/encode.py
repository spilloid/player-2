"""Encode capture pixels once so model inputs and recorded evidence cannot drift."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable

import cv2  # type: ignore[import-untyped, unused-ignore]
import numpy as np  # type: ignore[import-untyped, unused-ignore]

from player2.contracts import Frame


class EncodeCache:
    """Share exact encoded bytes so recorded evidence is literally what the model saw.

    Avoiding a second encode is a performance benefit, but the important property is that
    the recorder and model cannot drift apart as codecs or quality settings evolve.
    """

    def __init__(self, capacity: int) -> None:
        """Bound retained byte strings so an idle runtime cannot leak memory indefinitely."""
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self._capacity = capacity
        self._entries: OrderedDict[tuple[str, str, int | None], bytes] = OrderedDict()
        self._lock = threading.Lock()

    def get(
        self,
        frame: Frame,
        fmt: str,
        max_dim: int | None,
        encoder: Callable[[Frame], bytes],
    ) -> bytes:
        """Return cached bytes by pixel content while permitting duplicate work under races."""
        # Import here because fingerprint reuses the conversion helpers in this module.
        from player2.video.fingerprint import content_hash

        key = (content_hash(frame), fmt, max_dim)
        with self._lock:
            cached = self._entries.pop(key, None)
            if cached is not None:
                self._entries[key] = cached
                return cached

        encoded = encoder(frame)
        with self._lock:
            cached = self._entries.pop(key, None)
            if cached is not None:
                self._entries[key] = cached
                return cached
            self._entries[key] = encoded
            if len(self._entries) > self._capacity:
                self._entries.popitem(last=False)
            return encoded

    def __len__(self) -> int:
        """Report retained entries without exposing mutable cache internals to callers."""
        with self._lock:
            return len(self._entries)


def _bgra_pixels(frame: Frame) -> np.ndarray:
    """Validate opaque pixels before wrong dimensions corrupt frame provenance.

    Capture backends use arrays while the deterministic fake uses an owned byte buffer.
    Supporting both keeps tests and production on the same conversion instead of letting a
    second fake-only image path conceal channel-order corruption.
    """
    if frame.pixel_format != "BGRA8":
        raise ValueError(f"unsupported pixel format: {frame.pixel_format!r}")
    expected_shape = (frame.height, frame.width, 4)
    if isinstance(frame.data, np.ndarray):
        pixels = frame.data
        if pixels.dtype != np.uint8 or pixels.shape != expected_shape:
            raise ValueError(
                f"BGRA8 frame data must have dtype uint8 and shape {expected_shape}"
            )
        return pixels

    try:
        raw = memoryview(frame.data)  # type: ignore[arg-type]
    except TypeError:
        raise ValueError("frame data must expose a contiguous byte buffer") from None
    expected_bytes = frame.width * frame.height * 4
    if not raw.c_contiguous or raw.nbytes != expected_bytes:
        raise ValueError(f"BGRA8 frame data must contain exactly {expected_bytes} bytes")
    try:
        buffered: np.ndarray = np.frombuffer(raw, dtype=np.uint8).reshape(expected_shape)
    except (TypeError, ValueError):
        raise ValueError("frame data must expose a contiguous byte buffer") from None
    return buffered


def to_rgb(frame: Frame) -> np.ndarray:
    """Copy BGRA into RGB so silent red/blue swaps cannot poison inputs and recordings."""
    pixels = _bgra_pixels(frame)
    try:
        converted: np.ndarray = cv2.cvtColor(pixels, cv2.COLOR_BGRA2RGB)
    except cv2.error as error:
        raise ValueError(f"could not convert frame pixels: {error}") from error
    return converted


def downscale(frame: Frame, max_dim: int) -> Frame:
    """Shrink the longest edge without inventing pixels or losing timestamp provenance."""
    if isinstance(max_dim, bool) or not isinstance(max_dim, int) or max_dim <= 0:
        raise ValueError("max_dim must be a positive integer")

    pixels = _bgra_pixels(frame)
    if max(frame.width, frame.height) <= max_dim:
        return frame

    if frame.width >= frame.height:
        width = max_dim
        height = max(1, round(frame.height * max_dim / frame.width))
    else:
        height = max_dim
        width = max(1, round(frame.width * max_dim / frame.height))

    try:
        resized: np.ndarray = cv2.resize(
            pixels,
            (width, height),
            interpolation=cv2.INTER_AREA,
        )
    except cv2.error as error:
        raise ValueError(f"could not resize frame pixels: {error}") from error
    resized.flags.writeable = False
    return Frame(
        seq=frame.seq,
        session_ms=frame.session_ms,
        source_ms=frame.source_ms,
        width=width,
        height=height,
        pixel_format=frame.pixel_format,
        data=resized,
    )


def encode_png(frame: Frame) -> bytes:
    """Encode losslessly so compression artefacts cannot become false training features."""
    pixels = _bgra_pixels(frame)
    try:
        success, encoded = cv2.imencode(
            ".png",
            pixels[:, :, :3],
            [cv2.IMWRITE_PNG_COMPRESSION, 0],
        )
    except cv2.error as error:
        raise ValueError(f"could not encode PNG: {error}") from error
    if not success:
        raise ValueError("could not encode PNG")
    return bytes(encoded)


def encode_jpeg(frame: Frame, quality: int = 85) -> bytes:
    """Encode compact model input so stored evidence matches what the policy observed."""
    if isinstance(quality, bool) or not isinstance(quality, int) or not 1 <= quality <= 100:
        raise ValueError("quality must be an integer from 1 through 100")

    pixels = _bgra_pixels(frame)
    try:
        success, encoded = cv2.imencode(
            ".jpg",
            pixels[:, :, :3],
            [
                cv2.IMWRITE_JPEG_QUALITY,
                quality,
                cv2.IMWRITE_JPEG_OPTIMIZE,
                1,
            ],
        )
    except cv2.error as error:
        raise ValueError(f"could not encode JPEG: {error}") from error
    if not success:
        raise ValueError("could not encode JPEG")
    return bytes(encoded)
