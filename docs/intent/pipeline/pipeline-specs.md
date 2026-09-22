---
design: pipeline-design
prefix: PIPE
---

# Pipeline — EARS Specs

Facets: `CLI` (the `aqdt` command and its sub-commands), `WIN` (routine-window resolution),
`OUT` (output and exit status), `PG` (PostGIS loading from the command line), `SCHED` (the GitHub
Actions workflows a run executes in; what dispatches them, and on what cadence, is the
infrastructure segment's `INFRA-SCHED`), `CFG` (configuration on runners and tests).

## Command-Line Interface

- [x] **PIPE-CLI-001**: The pipeline shall expose a console script `aqdt` (declared under `[project.scripts]` in `pyproject.toml`) and the alias `python -m aqdt`, both dispatching to `aqdt.pipeline.cli:main`.
- [x] **PIPE-CLI-002**: The pipeline shall provide the sub-commands `ingest purpleair`, `ingest airnow`, `calibrate fit`, `calibrate apply`, `db schema`, and `db rebuild`, and no others.
- [x] **PIPE-CLI-003**: When `aqdt ingest purpleair` is invoked, the pipeline shall call `ingest_purpleair(PurpleAirSettings(), archive_uri, conn)` exactly once.
- [x] **PIPE-CLI-004**: When `aqdt ingest airnow` is invoked, the pipeline shall resolve the window per PIPE-WIN and call `ingest_airnow(AirNowSettings(), archive_uri, start, end, conn)` exactly once with the resolved bounds.
- [x] **PIPE-CLI-005**: When `aqdt calibrate fit` is invoked, the pipeline shall resolve `as_of` per PIPE-WIN-004 and call `fit_calibrations(archive_uri, as_of, CalibrationSettings(), conn)` exactly once.
- [x] **PIPE-CLI-006**: When `aqdt calibrate apply` is invoked, the pipeline shall resolve the window per PIPE-WIN and call `apply_calibrations(archive_uri, start, end, CalibrationSettings(), conn)` exactly once with the resolved bounds.
- [x] **PIPE-CLI-007**: When `aqdt db schema` or `aqdt db rebuild` is invoked, the pipeline shall open the connection named by `DATABASE_URL` and call the store's `apply_schema(conn)` or `rebuild(conn, archive_uri)` respectively, failing with an error naming `DATABASE_URL` when it is unset.
- [x] **PIPE-CLI-008**: The pipeline shall accept the global option `--archive-uri` and shall resolve the archive through the store's `resolve_archive_uri` (the option, else `AQDT_ARCHIVE_URI`) before dispatching any run, so that a run receives a concrete URI and an unset archive fails before any request or write with an error naming `AQDT_ARCHIVE_URI`.
- [x] **PIPE-CLI-009**: The pipeline shall accept `--log-level` (default `INFO`) and configure Python `logging` to stderr at that level before any run starts.
- [x] **PIPE-CLI-010**: When a sub-command is invoked, the pipeline shall construct only the settings that sub-command needs, so that a missing `PURPLEAIR_API_KEY` does not prevent `aqdt ingest airnow`, `aqdt calibrate fit`, or `aqdt calibrate apply` from running.
- [x] **PIPE-CLI-011**: When `--start` is given without `--end`, `--end` without `--start`, `--hours` together with either, or `--hours` with a non-positive value, the pipeline shall reject the invocation as a usage error before resolving a window.
- [x] **PIPE-CLI-012**: When a timestamp option (`--start`, `--end`, `--as-of`) is given, the pipeline shall parse it as ISO 8601 and treat a value without a UTC offset as UTC.

## Routine Windows

- [x] **PIPE-WIN-001**: The pipeline shall expose `resolve_window(now, start, end, hours, default_hours=48) -> (start, end)` and `resolve_as_of(now, as_of) -> as_of` as pure functions of their arguments that require a timezone-aware `now`, and shall read the clock (`datetime.now(UTC)`) only to supply `now` from `main`.
- [x] **PIPE-WIN-002**: When `resolve_window` is called with neither bounds nor `hours`, the pipeline shall return the half-open window `[now_h − default_hours, now_h)` where `now_h` is `now` truncated to the hour in UTC, so that the hour in progress is excluded.
- [x] **PIPE-WIN-003**: When `resolve_window` is called with `hours`, the pipeline shall return `[now_h − hours, now_h)`; when called with `start` and `end`, it shall return each converted to UTC and truncated to the hour, so that the logged bounds equal the bounds the run uses (the run still validates `end > start`).
- [x] **PIPE-WIN-004**: When `resolve_as_of` is called without `as_of`, the pipeline shall return the most recent 00:00 UTC at or before `now`; when called with `as_of`, it shall return it converted to UTC and truncated to the hour.
- [x] **PIPE-WIN-005**: When a window or `as_of` has been resolved, the pipeline shall log the resolved bounds at `INFO` before the run starts.

## Output and Exit Status

- [x] **PIPE-OUT-001**: When an ingest run completes, the pipeline shall print its `IngestSummary` as a single JSON object on one line of stdout, with datetimes as ISO 8601 UTC strings.
- [x] **PIPE-OUT-002**: When `aqdt calibrate fit` completes, the pipeline shall print a single JSON object with `as_of`, `window_start`, `window_end`, and `fits` (a mapping of status → count).
- [x] **PIPE-OUT-003**: When `aqdt calibrate apply` completes, the pipeline shall print a single JSON object with `window_start`, `window_end`, and `rows` (the number of calibrated rows).
- [x] **PIPE-OUT-004**: When a run completes without error, the pipeline shall exit with status 0; when the run raises, it shall log the exception to stderr, print nothing to stdout, and exit with a non-zero status.
- [x] **PIPE-OUT-005**: The pipeline shall not retry a failed run; retrying is the next scheduled occurrence's job.

## PostGIS Loading

- [x] **PIPE-PG-001**: When `DATABASE_URL` is set and `--no-postgis` is not given, the pipeline shall open the connection, pass it as `conn` to the run so that the run loads what it wrote into PostGIS, and close it after the run returns or raises.
- [x] **PIPE-PG-002**: When `DATABASE_URL` is unset, or `--no-postgis` is given, the pipeline shall pass `conn=None` so that the run touches the archive only.
- [x] **PIPE-PG-003**: If a run's archive writes succeed but its PostGIS load raises, then the pipeline shall exit non-zero (the archive is intact; `aqdt db rebuild` repairs the serving layer).

## Workflows

- [x] **PIPE-SCHED-001**: The repository shall contain the workflows `.github/workflows/ingest-purpleair.yaml`, `ingest-airnow.yaml`, `calibrate-fit.yaml`, and `calibrate-apply.yaml`, each declaring `workflow_dispatch` as its only trigger, so that every run — scheduled or by hand — reaches the workflow by the same path.
- [x] **PIPE-SCHED-003**: Each workflow shall run exactly one `aqdt` command — `ingest purpleair`, `ingest airnow`, `calibrate fit`, `calibrate apply` respectively — via `uv run` after `uv sync --no-dev`.
- [x] **PIPE-SCHED-004**: Each workflow shall declare a `concurrency` group with `cancel-in-progress: false`: `ingest-purpleair`, `ingest-airnow`, and `calibrate` for both calibration workflows, so that two runs doing the same work over overlapping windows are queued rather than run at once (correctness under concurrent writers is the observation store's, OBS-ARCHIVE-026).
- [x] **PIPE-SCHED-005**: Each workflow's job shall declare `timeout-minutes` of 10 (PurpleAir), 20 (AirNow), 30 (fit), and 20 (apply).
- [x] **PIPE-SCHED-006**: When a workflow is started by `workflow_dispatch` with non-empty `start`/`end` (ingest airnow, calibrate apply) or `as_of` (calibrate fit) inputs, the workflow shall pass them as the corresponding command options; empty inputs shall pass nothing, so the routine window applies.
- [x] **PIPE-SCHED-007**: No workflow shall set `DATABASE_URL`, so that scheduled runs write the archive only.
- [x] **PIPE-SCHED-008**: No workflow shall commit, push, or upload artifacts to the repository; the archive is the only output.
- [x] **PIPE-SCHED-009**: No workflow shall gate its job on an activation condition; scheduled runs are turned off at their source by disabling the EventBridge schedules that dispatch them (`INFRA-OPS-006`), so a workflow that receives a dispatch always runs it.

## Configuration

- [x] **PIPE-CFG-001**: Each workflow shall take `PURPLEAIR_API_KEY`, `AIRNOW_API_KEY`, `AWS_ACCESS_KEY_ID`, and `AWS_SECRET_ACCESS_KEY` from repository secrets and `AQDT_ARCHIVE_URI` and `AWS_DEFAULT_REGION` from repository variables, mapping into the job environment only the names its command needs; no workflow shall set `AQDT_BBOX`.
- [x] **PIPE-CFG-002**: The pipeline shall read no configuration from a file in the repository.
- [x] **PIPE-CFG-003**: Pipeline tests shall exercise window resolution as pure functions, the CLI with every run function replaced by a recording stub, and the workflow files by parsing them; they shall make no network requests and require no database.
