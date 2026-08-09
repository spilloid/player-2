"""Build durable session manifests without inventing unavailable provenance."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
UNKNOWN_REVISION = "unknown"
COUNT_NAMES = (
    "pad",
    "chunks",
    "events",
    "frames",
    "dropped",
    "dropped_pad",
    "dropped_frames",
    "dropped_chunks",
    "dropped_events",
)


def empty_counts() -> dict[str, int]:
    """Create independent counters so one session cannot corrupt another's accounting."""
    return {name: 0 for name in COUNT_NAMES}


def runtime_revision(repository: Path | None = None) -> str:
    """Return the source revision, or an honest marker when repository metadata is absent.

    Guessing a revision would make two incompatible runtimes appear to have produced the
    same dataset, which is harder to repair than explicitly recording unknown provenance.
    """
    working_directory = repository or Path(__file__).resolve().parents[2]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=working_directory,
            capture_output=True,
            check=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return UNKNOWN_REVISION
    revision = result.stdout.strip()
    if not revision:
        return UNKNOWN_REVISION
    try:
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=working_directory,
            capture_output=True,
            check=True,
            text=True,
            timeout=2.0,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return revision
    return f"{revision}-dirty" if dirty else revision


def build_manifest(
    *,
    session_id: str,
    started_wall: object,
    metadata: Mapping[str, object] | None = None,
    frame_encoding: Mapping[str, object] | None = None,
    counts: Mapping[str, int] | None = None,
    clean_shutdown: bool = False,
    revision: str | None = None,
) -> dict[str, Any]:
    """Construct one schema-labelled manifest while leaving metadata entirely opaque.

    Copying metadata and frame encoding prevents caller mutation from silently changing
    provenance between the initial crash marker and the final clean-shutdown rewrite.
    """
    manifest_counts = empty_counts()
    if counts is not None:
        manifest_counts.update(counts)
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "started_wall": started_wall,
        "runtime_revision": revision if revision is not None else runtime_revision(),
        "metadata": deepcopy(dict(metadata or {})),
        "frame_encoding": deepcopy(dict(frame_encoding or {})),
        "clean_shutdown": clean_shutdown,
        "counts": manifest_counts,
    }


def write_manifest(path: Path, manifest: Mapping[str, object]) -> None:
    """Atomically replace a manifest so interruption cannot erase the prior crash marker.

    A direct rewrite can be torn after truncating the original file, leaving neither the
    initial ``clean_shutdown=false`` record nor a valid final manifest.
    """
    temporary = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
    temporary.write_text(f"{payload}\n", encoding="utf-8")
    temporary.replace(path)
