# Changelog

Notable changes to BatchTicket are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - Unreleased

### Added

- Four task-level MCP tools backed by the shared deterministic transformation core.
- Transformation Plan v1, executable schema publication, bounded shape inspection, validation,
  diffing, schema adaptation, and token-aware inline or file results.
- Self-contained local Codex plugin build and runtime probes.

### Changed

- Adopted the BatchTicket product name while retaining the stable `agent-data-transformer`,
  `data-transformer`, `adt`, and `data_*` technical identifiers.
- Renamed transformation runtime observations to `execution_effects` to avoid treating a
  self-produced record as proof of semantic correctness.
- Hardened cumulative limits, path authority, destination preflight, cancellation, response
  bounds, numeric exactness, and multi-input effect accounting.
- Changed the default POSIX library worker from direct `fork` to `forkserver`, avoiding unsafe
  multithreaded forks while preserving direct `python -c` library use.

### Security

- Raised the development test dependency floor to `pytest>=9.0.3` to exclude CVE-2025-71176.

[0.2.0]: https://github.com/tetracoralla/BatchTicket/releases/tag/v0.2.0
