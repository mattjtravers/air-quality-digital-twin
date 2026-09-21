# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project state

An air quality digital twin for the Washington, D.C. metro: PurpleAir low-cost sensors fused with
EPA AirNow reference monitors, with NOAA HRRR transport fields in a later phase. Built
incrementally as weekly assignments; each increment lands as a coherent, tested step.

The HLD (`docs/high-level-design.md`) and five leaf LLDs under `docs/intent/` —
`observation-store` (`OBS`), `purpleair-ingest` (`PA`), `airnow-ingest` (`AN`), `calibration`
(`CAL`), and `pipeline` (`PIPE`) — are drafted with EARS specs and tests beside each, and all
five segments are implemented under `src/aqdt/` (`observation_store/`, `purpleair/`, `airnow/`,
`calibration/`, `pipeline/`, plus `registry.py` listing every archive product in load order).
The `[x]`/`[ ]`/`[D]` markers in each `*-specs.md` are the authoritative progress record. Read
the HLD first, then the LLD for the segment being touched; the LLDs are the source of truth for
schemas, API contracts, QC rules, and package layout (tests mirror `src/aqdt/` under `tests/`).
Every run is invoked through the `aqdt` CLI (`aqdt ingest purpleair|airnow`, `aqdt calibrate
fit|apply`, `aqdt db schema|rebuild`); the four `.github/workflows/{ingest-*,calibrate-*}.yaml`
cron workflows invoke it on a schedule, gated on the repository variable
`AQDT_SCHEDULES_ENABLED`.

Key architectural facts to keep in mind (rationale in the HLD):

- Runs are idempotent, time-windowed batch, scheduled by GitHub Actions cron — no resident
  daemon, no orchestrator yet. Runners have no PostGIS; they write the S3 archive only.
- The GeoParquet archive in S3 is the system of record; PostGIS (docker-compose sidecar in the
  Codespace) is a rebuildable cache. The Codespace and everything in it is disposable.
- QC flags, never drops. Raw values and the raw source record are always preserved.
- Pydantic validates records and API payloads; Pandera validates dataframes.
- All configuration is environment variables (Codespaces secrets; repository secrets/variables
  on Actions), never files in the repo. The project's bounding box is a code default
  (`DC_METRO`) that `AQDT_BBOX` may override. See the `observation-store` LLD § Environment for
  the variable list.

The devcontainer installs GDAL (`gdal-bin`, `libgdal-dev`) and `libspatialindex-dev`, and VS Code
is configured with the QGIS extension. A PostGIS sidecar runs via `.devcontainer/docker-compose.yml`
(and as a `services:` block in CI) with `DATABASE_URL` preset; PostGIS tests skip when it is unset.
Archive tests use a temp directory or `moto`, so no AWS credentials are needed to run the suite.

## Workflow

The user makes every git commit. Leave changes uncommitted at the end of a task and say what is
ready to commit; committing is the user's code-review step.

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
`pyproject.toml`). No runtime dependencies are declared yet; add them in `pyproject.toml` via
`uv add` as the implementation phases require them.

## LID
- Mode: Full
- Version: 1.3.0

## Linked-Intent Development (MANDATORY)

**Consult the `linked-intent-dev` skill for ALL code changes.** All changes flow through the arrow of intent in one direction:

```
HLD → LLDs → EARS → Tests → Code
```

- **New features and refactors**: full six-phase workflow (HLD check → LLD check/draft → EARS → intent-narrowing edge audit → tests-first → code).
- **Bug fixes**: walk the arrow like any other change — find where behavior diverged from intent and cascade from there. No short-circuit.
- **If unsure**: use the full workflow.

Stop after each phase for user review. **Docs carry current intent, written to be read cold** — write each doc as if authored fresh today, from current intent alone: no narration of how it changed, no meaning that needs the conversation that produced it, no rebuttals to questions only a past discussion raised. Rationale, considered alternatives, and constraints a fresh author would independently write stay; record rejected alternatives and why in the LLD's Decisions & Alternatives table, not as asides in body prose.

**Memory vs. intent.** Before saving durable project knowledge to agent or tool memory, test whether it is project *intent* — would a fresh agent, in any tool, next session, need it to build this system correctly? If yes, record it in the arrow (HLD / LLD / EARS / decision doc), which travels and cascades — not in private, per-tool memory, where intent escapes the arrow. Knowledge about the user or how they like to work stays in memory.

### Navigation

| What you need | Where to look |
|---|---|
| High-level design | `docs/high-level-design.md` |
| Design tree (sub-HLDs, LLDs, their specs) | `docs/intent/` — one folder per node |
| EARS specs | beside each design doc as `{node}-specs.md` in the node's folder under `docs/intent/` |
| Decision docs | `docs/decisions/` (project-level) and `docs/intent/<segment>/decisions/` |

### Terminology

- **HLD**: High-Level Design — single project-level doc at `docs/high-level-design.md`.
- **LLD**: Low-Level Design — detailed component design doc in `docs/intent/`. The design layer is a recursive tree: the root is the HLD, leaf LLDs own EARS, and a component deep enough to outgrow one doc becomes a sub-HLD (HLD-shaped, owns no EARS) with children beneath it. "HLD" and "LLD" are roles by position; depth-2 (one HLD over flat leaf LLDs) is the default.
- **EARS**: Easy Approach to Requirements Syntax — structured one-line requirements beside each design doc as `{node}-specs.md` in the node's folder under `docs/intent/`. IDs are path-concatenated — the root-to-leaf path of the owning segment plus a number — so a prefix grep gathers a subtree. Markers: `[x]` implemented, `[ ]` active gap, `[D]` deferred.
- **Arrow**: the unidirectional chain from vision to code (HLD → LLDs → EARS → Tests → Code). Strictly a DAG of intent.
- **Arrow segment**: the territory owned by one leaf LLD — the LLD itself plus the specs, tests, and code that cite its EARS IDs. The boundary is the leaf prefix. Within-segment cascade is free; across-segment cascade pauses.
- **Cascade**: propagating a change downstream through the arrow so adjacent levels stay coherent.

### Code annotations

Annotate code and tests with `@spec` comments citing EARS IDs:

```
// @spec AUTH-UI-001, AUTH-UI-002
```

Place the annotation at the *entry point of the behavior's implementation graph* — the topmost function or module owning the specified behavior, not every helper. When a behavior spans multiple subsystems (UI + API + database, for example), annotate at the entry point in each subsystem. Tests follow the same rule: annotate the test that directly exercises the spec, not every inner assertion.
