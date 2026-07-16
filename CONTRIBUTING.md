# Contributing

Thank you for improving Errorbook MCP. Small, focused changes with clear behavioral tests are easiest
to review.

## Development setup

Install Python 3.11 or newer and [uv](https://docs.astral.sh/uv/), then run:

```powershell
git clone https://github.com/stardustlil/errorbook-mcp.git
Set-Location errorbook-mcp
uv sync --locked --extra dev
```

Windows automatically uses Microsoft Edge for PDF integration tests. On macOS or Linux, set
`ERRORBOOK_BROWSER_PATH` when Chrome or Chromium cannot be discovered automatically.

## Required checks

Run these before opening a pull request:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest
uv build
```

Changes to scheduling, idempotency, database transactions, tool schemas, or PDF generation require a
regression test. PDF layout changes must also be rendered to images and visually inspected.

## Pull requests

- Keep each pull request scoped to one coherent behavior.
- Explain user-visible impact and any data compatibility implications.
- Preserve immutable problem numbers and append-only review history.
- Do not weaken path, Markdown, LaTeX, MathML, or export integrity checks.
- Update `CHANGELOG.md` for user-visible changes.

## Data hygiene

Never commit real problem databases, source images, generated PDFs, API keys, Hermes configuration,
or logs containing question content. Use synthetic examples in tests and redact all personal data
from bug reports.
