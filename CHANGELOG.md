# Changelog

All notable changes to player-2 are documented in this file, reconstructed
from `git log` and `git tag` (no prior changelog existed). This is an
internal R&D project developed in units/milestones rather than a
release cadence, so pre-tag history below is summarized by milestone
rather than itemized commit-by-commit; tagged versions match
`pyproject.toml`.

## [Unreleased]

- Added a live overlay window for commentary, `--warmup` macro chaining,
  and a Factorio tooltip fix (2026-08-15).
- Added an optional `commentary` field to `ActionChunk`, with a
  schema-level required/optional toggle (2026-08-15).
- Adopted company PR and version-freshness documentation standards
  (2026-08-23).

## [0.2.1] - 2026-08-14

- Added live decision visibility, an explicit `--goal` override, and a
  fix for a real context-overflow bug.

## [0.2.0] - 2026-08-14

- Added a direct-SDK inference transport driven from a LAN Ollama box,
  with a config layer and two accompanying safety fixes.
- Wrote up the product site and process log through live verification,
  plus a machine-handoff doc.

## Pre-release — building to Milestone 1 (2026-08-09 – 2026-08-13)

- Project scaffold, the time-base/action-chunk contracts, and the motor
  system (scheduler + virtual controller, verified on real hardware).
- Moved game-specific facts (window titles, pause chords, chat-box
  restrictions) into per-game profile config, out of the runtime.
- Added screen capture ("eyes") and synchronized demonstration recording.
- Added diagnostic tooling: an XInput readback check, a single-axis
  `hold` test, a focus countdown, and a session-held virtual pad.
- **Milestone 1 (2026-08-09):** an agent process drove a real commercial
  game (Factorio 2.0.7) through a standard controller abstraction, using
  action chunks authored the way a model will author them.
- Added the agent seam (Observation/policy/loop), direct-SDK transport
  scaffolding, and a budget governor; captured real gameplay and
  measured storage/spend tradeoffs (dataset dedup via hashing).
