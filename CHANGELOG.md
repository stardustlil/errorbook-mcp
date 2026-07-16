# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-07-16

### Added

- MCP prompt for agent-side image transcription into Markdown and LaTeX.
- Durable SQLite problem storage with immutable public numbers, revisions, tags, and history.
- FSRS 6.3.1 scheduling with decaying error pressure and manual priority boosts.
- Fair weekly review selection with backlog reporting and starvation prevention.
- A4 question-sheet and answer-booklet exports with static MathML rendering.
- Idempotent writes, optimistic content updates, export leases, and frozen review snapshots.
- MCP stdio and streamable HTTP transports, export resources, and structured error responses.
- Security validation for Markdown, LaTeX, generated MathML, stored paths, and PDF integrity.
- Automated tests covering concurrency, scheduling, MCP round trips, failure recovery, and PDF output.

[Unreleased]: https://github.com/stardustlil/errorbook-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/stardustlil/errorbook-mcp/releases/tag/v0.1.0
