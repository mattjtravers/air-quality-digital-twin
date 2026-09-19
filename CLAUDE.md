# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

This repository is an early-stage scaffold for an air quality digital twin. There is no
application source code yet — only project tooling (dependency management, linting, CI, tests
directory with a placeholder). Do not assume any modules, packages, or architecture exist beyond
what's described below; check the current file tree before making claims about structure.

The devcontainer installs GDAL (`gdal-bin`, `libgdal-dev`) and `libspatialindex-dev`, and VS Code
is configured with the QGIS extension — geospatial/raster-vector processing is intended but not
yet implemented.

## Commands

This project uses `uv` for dependency and environment management.

```bash
# Install/sync dependencies (including dev group)
uv sync --all-groups

# Run the full test suite
uv run pytest

# Run a single test file or test
uv run pytest tests/test_placeholder.py
uv run pytest tests/test_placeholder.py::test_placeholder

# Lint
uv run ruff check .

# Lint with autofix
uv run ruff check --fix .
```

CI (`.github/workflows/ci.yaml`) runs `uv sync --all-groups`, `uv run ruff check .`, and
`uv run pytest` on every push/PR to `main` — match this locally before pushing.

Python requirement: `>=3.11`. Ruff config: line length 100, rule sets `E`, `F`, `I` (see
`pyproject.toml`).

## Development workflow (Linked Intent Development)

This repo has the `linked-intent-dev` and `arrow-maintenance` Claude Code plugins enabled
(`.claude/settings.json`). Per `linked-intent-dev`'s own description, it should be consulted for
**all** code changes, including bug fixes — it walks changes through a mode-aware six-phase
workflow (HLD → LLD → EARS → intent-narrowing edge audit → tests-first → code) with mandatory
stops between phases. Use the `linked-intent-dev` skill rather than jumping straight to
implementation when making non-trivial changes.
