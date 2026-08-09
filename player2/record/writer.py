"""Persist session evidence without making control or capture wait for storage."""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast
from uuid import uuid4

from player2.clock import Clock
from player2.contracts import NEUTRAL, ActionChunk, Frame, PadState, chunk_to_dict
from player2.control.base import IControllerOutput
from player2.record.manifest import build_manifest, empty_counts, write_manifest
from player2.video.encode import downscale, encode_jpeg, encode_png
from player2.video.fingerprint import content_hash


@dataclass(frozen=True)
class RecordedSession:
    """Return every intact row while labelling damage instead of hiding crashed data."""

    manifest: dict[str, Any]
    frames: list[dict[str, Any]]
    pad: list[dict[str, Any]]
    chunks: list[dict[str, Any]]
    events: list[dict[str, Any]]
    truncated: bool


@dataclass(frozen=True)
class _PadRecord:
    """Move an immutable executed state to the storage thread without serialization work."""

    session_ms: float
    state: PadState


@dataclass(frozen=True)
class _ChunkRecord:
    """Keep accepted intent separate from executed reports to prevent false training labels."""

    session_ms: float
    chunk: ActionChunk


@dataclass(frozen=True)
class _EventRecord:
    """Retain lifecycle damage explicitly so missing intervals cannot look uneventful."""

    kind: str
    session_ms: float
    detail: str


@dataclass(frozen=True)
class _FrameRecord:
    """Defer compression because doing it in capture callbacks would stall future frames."""

    frame: Frame


_Record = _PadRecord | _ChunkRecord | _EventRecord | _FrameRecord
_CHECKPOINT_SECONDS = 2.0
_CHECKPOINT_RECORDS = 128
_STOP_JOIN_SECONDS = 2.0


class SessionRecorder:
    """Write bounded, append-only session streams on a dedicated worker.

    Producers use a full-or-drop queue because waiting for disk at a control tick can extend
    a held input, while waiting in a capture callback can create further invisible frame loss.
    Frames default to downscaled JPEG because the policy observes that compact image, while
    lossless full-resolution PNG would preserve discarded detail at an unusable data volume.
    Repeated observations retain their separate timeline rows while sharing pixels, so an
    idle screen cannot turn a useful session into an unbounded pile of identical images.
    """

    _STREAM_NAMES = ("pad.jsonl", "chunks.jsonl", "events.jsonl", "frames.jsonl")

    def __init__(
        self,
        root: str | os.PathLike[str],
        clock: Clock,
        metadata: Mapping[str, object] | None = None,
        queue_size: int = 1024,
        frame_format: str = "jpeg",
        frame_max_dim: int | None = 1280,
        jpeg_quality: int = 85,
        deduplicate: bool = True,
    ) -> None:
        """Prepare one-shot session state without touching disk before ``start``.

        A unique non-time-based identifier avoids consulting a second clock and prevents two
        sessions started at the same clock instant from overwriting irreplaceable evidence.
        """
        if isinstance(queue_size, bool) or not isinstance(queue_size, int) or queue_size < 1:
            raise ValueError("queue_size must be a positive integer")
        if frame_format not in ("jpeg", "png"):
            raise ValueError("frame_format must be 'jpeg' or 'png'")
        if frame_max_dim is not None and (
            isinstance(frame_max_dim, bool)
            or not isinstance(frame_max_dim, int)
            or frame_max_dim <= 0
        ):
            raise ValueError("frame_max_dim must be a positive integer or None")
        if isinstance(jpeg_quality, bool) or not isinstance(jpeg_quality, int):
            raise ValueError("jpeg_quality must be an integer from 1 through 100")
        if not 1 <= jpeg_quality <= 100:
            raise ValueError("jpeg_quality must be an integer from 1 through 100")
        if not isinstance(deduplicate, bool):
            raise ValueError("deduplicate must be a boolean")
        self._root = Path(root)
        self._clock = clock
        self._metadata_source = metadata
        self._metadata: dict[str, object] = {}
        self._frame_format = frame_format
        self._frame_max_dim = frame_max_dim
        self._jpeg_quality = jpeg_quality
        self._deduplicate = deduplicate
        self._session_id = uuid4().hex
        self._directory = self._root / self._session_id
        self._queue: queue.Queue[_Record] = queue.Queue(maxsize=queue_size)
        self._counts = empty_counts()
        self._counts["frames_unique"] = 0
        self._counts_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._starting = False
        self._started = False
        self._stopped = False
        self._accepting = False
        self._failed = False
        self._manifest: dict[str, Any] | None = None

    @property
    def directory(self) -> Path:
        """Expose the immutable session location so callers cannot redirect active writes."""
        return self._directory

    @property
    def session_id(self) -> str:
        """Expose the identifier shared by the directory and its provenance manifest."""
        return self._session_id

    @property
    def is_running(self) -> bool:
        """Report worker liveness so a failed logger is never mistaken for complete evidence."""
        with self._state_lock:
            thread = self._thread
            return self._accepting and thread is not None and thread.is_alive()

    def start(self) -> None:
        """Create the crash-marked manifest before accepting any producer records.

        Writing ``clean_shutdown=false`` first prevents a killed process from leaving a
        plausible-looking complete dataset whose final accounting never ran.
        """
        with self._stop_lock:
            with self._state_lock:
                if self._started or self._starting:
                    return
                self._starting = True
            try:
                self._root.mkdir(parents=True, exist_ok=True)
                self._directory.mkdir()
                (self._directory / "frames").mkdir()
                for name in self._STREAM_NAMES:
                    (self._directory / name).touch()
                # Snapshot nested provenance before the crash marker exists. Otherwise a
                # caller mutation can make the final manifest disagree with that marker.
                self._metadata = deepcopy(dict(self._metadata_source or {}))
                started_wall: object = getattr(self._clock, "epoch_wall", None)
                manifest = build_manifest(
                    session_id=self._session_id,
                    started_wall=started_wall,
                    metadata=self._metadata,
                    frame_encoding={
                        "format": self._frame_format,
                        "max_dim": self._frame_max_dim,
                        "jpeg_quality": self._jpeg_quality,
                    },
                    counts=self._counts,
                )
                write_manifest(self._directory / "manifest.json", manifest)
            except BaseException:
                with self._state_lock:
                    self._starting = False
                raise
            with self._state_lock:
                self._manifest = manifest
                self._stop_event.clear()
                self._accepting = True
                self._failed = False
                self._started = True
                self._starting = False
                self._thread = threading.Thread(
                    target=self._run,
                    name=f"session-recorder-{self._session_id[:8]}",
                    daemon=True,
                )
                self._thread.start()

    def stop(self) -> None:
        """Stop admission without waiting forever on stalled storage.

        A timed-out worker, failed stream, or any drop leaves the manifest dirty: claiming a
        complete timeline in those cases would hide an unlabelled causal hole.
        """
        with self._stop_lock:
            with self._state_lock:
                if not self._started or self._stopped:
                    return
                self._accepting = False
                self._stop_event.set()
                thread = self._thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(_STOP_JOIN_SECONDS)
            with self._counts_lock:
                counts = dict(self._counts)
            manifest = dict(self._manifest or {})
            manifest["counts"] = counts
            worker_finished = thread is None or not thread.is_alive()
            manifest["clean_shutdown"] = (
                worker_finished
                and not self._failed
                and counts["dropped"] == 0
            )
            if worker_finished:
                try:
                    write_manifest(self._directory / "manifest.json", manifest)
                except BaseException:
                    # Shutdown remains bounded even when the filesystem is no longer writable.
                    manifest["clean_shutdown"] = False
            else:
                # The initial crash marker is safer than competing with a stuck disk writer.
                manifest["clean_shutdown"] = False
            with self._state_lock:
                self._manifest = manifest
                self._stopped = True

    def record_pad(self, session_ms: float, state: PadState) -> None:
        """Queue the state that crossed the device boundary, never a proposed substitute."""
        self._enqueue(_PadRecord(session_ms, state))

    def record_chunk(self, session_ms: float, chunk: ActionChunk) -> None:
        """Queue accepted intent separately so preemption cannot masquerade as execution."""
        self._enqueue(_ChunkRecord(session_ms, chunk))

    def record_event(self, kind: str, session_ms: float, detail: str) -> None:
        """Queue labelled lifecycle damage so replay does not infer an uneventful interval."""
        self._enqueue(_EventRecord(kind, session_ms, detail))

    def record_frame(self, frame: Frame) -> None:
        """Queue a captured frame without compression work on the graphics callback."""
        self._enqueue(_FrameRecord(frame))

    def record_invalid_pad_timestamp(self) -> None:
        """Count rejected clock output so a missing control state cannot look intentional."""
        with self._state_lock:
            if self._accepting:
                self._drop_stream("pad")

    def _enqueue(self, record: _Record) -> None:
        """Drop immediately on saturation because backpressure can wedge the motor system."""
        with self._state_lock:
            if not self._accepting:
                return
            try:
                self._queue.put_nowait(record)
            except queue.Full:
                self._drop_record(record)

    def _increment(self, name: str, amount: int = 1) -> None:
        """Keep manifest accounting coherent across producer and writer threads."""
        with self._counts_lock:
            self._counts[name] += amount

    def _drop_record(self, record: _Record) -> None:
        """Attribute a loss to its stream so replay knows which interval is unusable."""
        self._drop_stream(self._stream_for(record))

    def _drop_stream(self, stream: str, amount: int = 1) -> None:
        """Maintain aggregate and stream counters together to prevent ambiguous loss."""
        self._increment("dropped", amount)
        self._increment(f"dropped_{stream}", amount)

    @staticmethod
    def _stream_for(record: _Record) -> str:
        """Map queued evidence to the count that identifies a damaged timeline stream."""
        if isinstance(record, _PadRecord):
            return "pad"
        if isinstance(record, _ChunkRecord):
            return "chunks"
        if isinstance(record, _EventRecord):
            return "events"
        return "frames"

    def _run(self) -> None:
        """Own every disk operation so serialization latency never reaches a producer."""
        try:
            with (
                (self._directory / "pad.jsonl").open("a", encoding="utf-8") as pad,
                (self._directory / "chunks.jsonl").open("a", encoding="utf-8") as chunks,
                (self._directory / "events.jsonl").open("a", encoding="utf-8") as events,
                (self._directory / "frames.jsonl").open("a", encoding="utf-8") as frames,
            ):
                self._consume(pad, chunks, events, frames)
        except BaseException:
            # If storage itself disappears, stop accepting records and count everything that
            # was admitted; allowing the worker to die while the queue fills would hide loss.
            self._mark_failed()
            self._drop_remaining()

    def _consume(
        self,
        pad: TextIO,
        chunks: TextIO,
        events: TextIO,
        frames: TextIO,
    ) -> None:
        """Drain through shutdown so accepted rows are not discarded by an orderly stop."""
        last_frame_seq: int | None = None
        frame_paths: dict[str, str] = {}
        records_since_checkpoint = 0
        checkpoint_at = time.monotonic() + _CHECKPOINT_SECONDS
        while True:
            try:
                record = self._queue.get(timeout=0.05)
            except queue.Empty:
                if time.monotonic() >= checkpoint_at:
                    self._checkpoint_counts()
                    checkpoint_at = time.monotonic() + _CHECKPOINT_SECONDS
                if self._stop_event.is_set():
                    return
                continue
            try:
                if isinstance(record, _PadRecord):
                    self._write_pad(pad, record)
                elif isinstance(record, _ChunkRecord):
                    self._write_chunk(chunks, record)
                elif isinstance(record, _EventRecord):
                    self._write_event(events, record)
                else:
                    last_frame_seq = self._write_frame(
                        frames,
                        events,
                        record.frame,
                        last_frame_seq,
                        frame_paths,
                    )
            except Exception:
                self._drop_record(record)
            finally:
                self._queue.task_done()
            records_since_checkpoint += 1
            if records_since_checkpoint >= _CHECKPOINT_RECORDS:
                self._checkpoint_counts()
                records_since_checkpoint = 0
                checkpoint_at = time.monotonic() + _CHECKPOINT_SECONDS

    def _write_pad(self, handle: TextIO, record: _PadRecord) -> None:
        """Serialize a complete snapshot so omitted releases cannot become stuck controls."""
        state = record.state
        self._append(
            handle,
            {
                "session_ms": record.session_ms,
                "left_stick": list(state.left_stick),
                "right_stick": list(state.right_stick),
                "left_trigger": state.left_trigger,
                "right_trigger": state.right_trigger,
                "buttons": sorted(button.value for button in state.buttons),
            },
        )
        self._increment("pad")

    def _write_chunk(self, handle: TextIO, record: _ChunkRecord) -> None:
        """Reuse the wire serializer so replay cannot reinterpret sparse keyframes."""
        payload = {"session_ms": record.session_ms, **chunk_to_dict(record.chunk)}
        self._append(handle, payload)
        self._increment("chunks")

    def _write_event(self, handle: TextIO, record: _EventRecord) -> None:
        """Persist event detail verbatim because interpreting it would add domain coupling."""
        self._append(
            handle,
            {
                "kind": record.kind,
                "session_ms": record.session_ms,
                "detail": record.detail,
            },
        )
        self._increment("events")

    def _write_frame(
        self,
        frames: TextIO,
        events: TextIO,
        frame: Frame,
        previous_seq: int | None,
        frame_paths: dict[str, str],
    ) -> int:
        """Write pixels before their index and explicitly label every sequence discontinuity.

        Indexing first could leave a crash-time row pointing at a partial image; comparing
        persisted frames also exposes queue drops instead of only gaps seen by the producer.
        Reusing prior paths retains every observation without repeating idle-screen storage.
        """
        frame_hash = content_hash(frame)
        stored = (
            downscale(frame, self._frame_max_dim)
            if self._frame_max_dim is not None
            else frame
        )
        extension = ".jpg" if self._frame_format == "jpeg" else ".png"
        existing_path = frame_paths.get(frame_hash)
        if self._deduplicate and existing_path is not None:
            relative_path = Path(existing_path)
        else:
            relative_path = Path("frames") / f"{frame.seq:012d}{extension}"
            destination = self._directory / relative_path
            temporary = destination.with_suffix(f"{extension}.tmp")
            if self._frame_format == "jpeg":
                encoded = encode_jpeg(stored, quality=self._jpeg_quality)
            else:
                encoded = encode_png(stored)
            temporary.write_bytes(encoded)
            temporary.replace(destination)
        if existing_path is None:
            frame_paths[frame_hash] = relative_path.as_posix()
            self._increment("frames_unique")
        if previous_seq is None and frame.seq > 0:
            detail = f"missing frame seq 0-{frame.seq - 1}"
            self._write_event(events, _EventRecord("frame_gap", frame.session_ms, detail))
        elif previous_seq is not None and frame.seq != previous_seq + 1:
            expected = previous_seq + 1
            if frame.seq > expected:
                detail = f"missing frame seq {expected}-{frame.seq - 1}"
            else:
                detail = f"expected frame seq {expected}, received {frame.seq}"
            self._write_event(events, _EventRecord("frame_gap", frame.session_ms, detail))
        self._append(
            frames,
            {
                "seq": frame.seq,
                "session_ms": frame.session_ms,
                "source_ms": frame.source_ms,
                "width": stored.width,
                "height": stored.height,
                "capture_width": frame.width,
                "capture_height": frame.height,
                "path": relative_path.as_posix(),
                "content_hash": frame_hash,
            },
        )
        self._increment("frames")
        return frame.seq

    def _drop_remaining(self) -> None:
        """Account admitted records after worker failure so loss never looks like silence."""
        dropped: dict[str, int] = {"pad": 0, "chunks": 0, "events": 0, "frames": 0}
        while True:
            try:
                record = self._queue.get_nowait()
            except queue.Empty:
                break
            dropped[self._stream_for(record)] += 1
            self._queue.task_done()
        for stream, amount in dropped.items():
            if amount:
                self._drop_stream(stream, amount)

    def _mark_failed(self) -> None:
        """Close admission after a writer failure so later producer loss is never concealed."""
        with self._state_lock:
            self._failed = True
            self._accepting = False

    def _checkpoint_counts(self) -> None:
        """Persist loss accounting during capture so a crash cannot erase its warning label."""
        with self._counts_lock:
            counts = dict(self._counts)
        manifest = dict(self._manifest or {})
        manifest["counts"] = counts
        manifest["clean_shutdown"] = False
        try:
            write_manifest(self._directory / "manifest.json", manifest)
        except BaseException:
            self._mark_failed()
            return
        with self._state_lock:
            self._manifest = manifest

    @staticmethod
    def _append(handle: TextIO, payload: Mapping[str, object]) -> None:
        """Flush each JSON line so a crash costs at most the line currently being written."""
        line = json.dumps(
            payload,
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=False,
        )
        handle.write(f"{line}\n")
        handle.flush()


class RecordingOutput:
    """Decorate the controller boundary so recordings contain actual executed reports.

    Clock validation lives here because this is the last point before a pad timestamp becomes
    durable evidence. Rejecting bad values labels a missing state instead of writing a timeline
    whose ordering cannot be trusted.
    """

    def __init__(self, inner: IControllerOutput, recorder: SessionRecorder, clock: Clock) -> None:
        """Retain the shared clock so controller and frame evidence align on one timeline."""
        self._inner = inner
        self._recorder = recorder
        self._clock = clock
        self._last_session_ms: float | None = None

    def set_state(self, state: PadState) -> None:
        """Write first, then log without allowing recorder failure to cost the agent its hands."""
        self._inner.set_state(state)
        self._record_state(state)

    def reset(self) -> None:
        """Forward and record release because losing it would make a held input look longer."""
        self._inner.reset()
        self._record_state(NEUTRAL)

    def _record_state(self, state: PadState) -> None:
        """Record only monotonically ordered finite non-negative time from hostile clocks."""
        try:
            value = self._clock.now_ms()
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("clock value must be numeric")
            session_ms = float(value)
            if not math.isfinite(session_ms) or session_ms < 0.0:
                raise ValueError("clock value must be finite and non-negative")
            if self._last_session_ms is not None and session_ms < self._last_session_ms:
                raise ValueError("clock value moved backwards")
        except BaseException:
            self._record_invalid_timestamp()
            return
        self._last_session_ms = session_ms
        try:
            self._recorder.record_pad(session_ms, state)
        except BaseException:
            pass

    def _record_invalid_timestamp(self) -> None:
        """Ask capable recorders to count rejected time without burdening controller output."""
        try:
            invalid = getattr(self._recorder, "record_invalid_pad_timestamp", None)
            if callable(invalid):
                invalid()
        except BaseException:
            pass


def load_session(directory: str | os.PathLike[str]) -> RecordedSession:
    """Load intact prefixes and report torn JSONL instead of discarding crash evidence."""
    path = Path(directory)
    manifest_value: object = json.loads(
        (path / "manifest.json").read_text(encoding="utf-8")
    )
    if not isinstance(manifest_value, dict):
        raise ValueError("manifest must be a JSON object")
    manifest = cast(dict[str, Any], manifest_value)
    frames, frames_truncated = _read_jsonl(path / "frames.jsonl")
    pad, pad_truncated = _read_jsonl(path / "pad.jsonl")
    chunks, chunks_truncated = _read_jsonl(path / "chunks.jsonl")
    events, events_truncated = _read_jsonl(path / "events.jsonl")
    return RecordedSession(
        manifest=manifest,
        frames=frames,
        pad=pad,
        chunks=chunks,
        events=events,
        truncated=(
            frames_truncated or pad_truncated or chunks_truncated or events_truncated
        ),
    )


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Keep every parseable row while marking corruption rather than hiding recovery data."""
    if not path.is_file():
        return [], True
    rows: list[dict[str, Any]] = []
    truncated = False
    with path.open("rb") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                value: object = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                truncated = True
                continue
            if not isinstance(value, dict):
                truncated = True
                continue
            rows.append(cast(dict[str, Any], value))
    return rows, truncated
