---
parent: high-level-design
prefix: PIPE
---

# Pipeline

## Context and Design Philosophy

Every component of the twin exposes its run as a Python function of *(archive, window,
settings)*: `ingest_purpleair`, `ingest_airnow`, `fit_calibrations`, `apply_calibrations`.
Nothing in those components knows what time it is, where it is running, or how often it should
run — that is what keeps them pure and their archives deterministic. This component is the one
place those questions are answered. It owns the command-line entry point through which every run
is invoked, the *routine window* each run covers when invoked with no window (the only place the
wall clock enters the project), and the GitHub Actions schedules that invoke it.

Three principles:

1. **One entry point, any trigger.** A schedule, a developer at a terminal, and a future
   orchestrator all invoke the same `aqdt` command with the same arguments. Changing the trigger
   changes nothing else.
2. **The wall clock is a caller convenience, not an input.** A routine window is resolved from the
   current time into explicit bounds *before* a run starts, logged, and passed down as if the
   caller had typed it. Any window can be given explicitly for backfill.
3. **Late is fine; overlapping is not.** Schedules are best-effort and windows overlap their
   predecessors, so a delayed run costs latency only. Two runs writing the same archive partition
   concurrently would lose rows, so runs that can share a partition are serialized.

## Command-Line Interface

The console script `aqdt` (declared in `pyproject.toml`; `python -m aqdt` is an alias) has one
sub-command per run plus the serving-layer maintenance commands:

| Command | Runs | Routine window when no bounds given |
|---|---|---|
| `aqdt ingest purpleair` | `ingest_purpleair` | a snapshot (the run has no window) |
| `aqdt ingest airnow [--start T --end T \| --hours N]` | `ingest_airnow` | trailing 48 h: `[now_h − 48 h, now_h)` |
| `aqdt calibrate fit [--as-of T]` | `fit_calibrations` | the most recent 00:00 UTC at or before now |
| `aqdt calibrate apply [--start T --end T \| --hours N]` | `apply_calibrations` | trailing 48 h: `[now_h − 48 h, now_h)` |
| `aqdt db schema` | `apply_schema` | — |
| `aqdt db rebuild` | `rebuild` | — |

`now_h` is the current UTC time truncated to the hour, so the hour in progress is never
requested: an AirNow hour is published after it ends, and a PurpleAir hour's snapshots are still
arriving. Timestamps are ISO 8601; a naive value is read as UTC; every bound is converted to UTC
and truncated to the hour during resolution, so the bounds logged are the bounds the run uses. `--hours N` sets the trailing window's length; `--start`/`--end`
set it exactly and must be given together; combining them with `--hours` is an error. Bounds
are echoed in the JSON output as ISO 8601 UTC strings.

Global options: `--archive-uri` (default `AQDT_ARCHIVE_URI`; resolved through the store before
any run is dispatched, so an unset archive fails before a request is made), `--no-postgis` (see
below), `--log-level` (default `INFO`).

**PostGIS loading.** A run loads what it wrote into PostGIS when `DATABASE_URL` is set and
`--no-postgis` is not given. In the Codespace the variable is preset by docker-compose, so a
developer's run refreshes the serving layer; on an Actions runner it is unset, so scheduled runs
touch the archive only, as the HLD's environment split requires. The CLI opens the connection,
hands it to the run, and closes it when the run returns or raises; runs commit their own loads
and never close it. `aqdt db rebuild` is how a Codespace catches up with what the schedules
archived while it was idle.

**Output.** Each run prints one JSON object to stdout on completion — an ingester's
`IngestSummary`, a fit run's count of fits by status, an apply run's row count and window — so
the workflow log for every scheduled run states what it did. Diagnostics go to stderr through
`logging` at `--log-level`, including the resolved window before the run starts. A run that
raises exits non-zero with the exception; nothing is retried at this level (the HTTP clients
retry requests; the schedule's next occurrence retries the run).

**Settings.** Each command constructs only the settings it needs: `aqdt calibrate fit` does not
require `PURPLEAIR_API_KEY`. Missing configuration fails before any request is made.

## Routine Windows

`resolve_window(now, start, end, hours, default_hours=48) -> (start, end)` and
`resolve_as_of(now, as_of) -> as_of` are pure functions of their arguments; the CLI passes
`datetime.now(UTC)`. They are the whole of the project's dependence on the clock.

| Run | Why this window |
|---|---|
| AirNow: trailing 48 h | AirNow re-issues the preceding 48 hours on every hourly update, so a 48 h window collects every revision of preliminary data (AirNow LLD § Run). |
| Apply: trailing 48 h | Re-applies the hours whose sensor aggregates were still accumulating on the previous run, and repairs any hours calibrated with a stale fit because the daily fit ran late or was skipped. 48 h covers one missed daily fit with margin. |
| Fit: most recent 00:00 UTC | Refits are daily (calibration LLD § Runs). Keying on midnight rather than "now" makes the run idempotent within the day: however late the schedule fires, the same `as_of` is refit and the same partition replaced. |

## Schedules

One workflow per run under `.github/workflows/`, each with a `schedule` cron trigger and a
`workflow_dispatch` trigger (with optional `start`/`end` or `as_of` inputs; an empty input means
"use the routine window") for manual runs and backfill:

| Workflow | Cron (UTC) | Command | Concurrency group |
|---|---|---|---|
| `ingest-purpleair.yaml` | `*/15 * * * *` | `aqdt ingest purpleair` | `ingest-purpleair` |
| `ingest-airnow.yaml` | `20 * * * *` | `aqdt ingest airnow` | `ingest-airnow` |
| `calibrate-fit.yaml` | `30 0 * * *` | `aqdt calibrate fit` | `calibrate` |
| `calibrate-apply.yaml` | `40 * * * *` | `aqdt calibrate apply` | `calibrate` |

Cadence rationale: PurpleAir every 15 minutes gives about four snapshots per sensor-hour, enough
for hourly aggregation (which imposes no minimum) at 96 requests a day. AirNow at 20 past the
hour leaves the feed time to publish the hour just ended. Fit at 00:30 runs once the day's last
AirNow hour is archived; apply at :40 follows both ingesters each hour and, on the first hour of
the day, the new fit.

Each workflow: checks out the repository, installs `uv` and the project (`uv sync --no-dev`),
and runs the command. Every job has `timeout-minutes` under its cron interval (PurpleAir 10,
AirNow 20, fit 30, apply 20) so a hung run cannot pile up behind itself. GitHub runs `schedule`
triggers from the default branch only, so a schedule change takes effect when it lands on `main`.

**Concurrency.** Correctness under concurrent writers is the observation store's: its partition
writes are compare-and-swap, so a run started by a schedule, by hand, or by anything else can
never lose another writer's rows (observation-store LLD § One writer per partition). The
concurrency groups exist to keep two runs of the same workflow from both spending runner minutes
on overlapping windows and interleaving their logs: each ingester is serialized with itself, and
fit and apply share the `calibrate` group because both aggregate the same sensor hours.
`cancel-in-progress` is false everywhere — a run in progress is never interrupted; a queued run
may be superseded by a newer queued run, which is harmless because windows overlap.

**Configuration on runners.** Credentials are repository secrets: `PURPLEAIR_API_KEY`,
`AIRNOW_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`. Non-secret configuration is
repository variables: `AQDT_ARCHIVE_URI`, `AWS_DEFAULT_REGION`. The bounding box is the store's
`DC_METRO` default and is not configured on runners. Each workflow maps
only what its command needs into the job environment. Nothing is written to the repository by a
run; the archive is in S3.

**Activation.** Every scheduled job is gated on the repository variable
`AQDT_SCHEDULES_ENABLED` being `true`; a cron occurrence while it is anything else is skipped
(reported as skipped, costing no minutes). `workflow_dispatch` runs are never gated. So the
workflows can land on `main`, secrets and variables can be set, and each run can be exercised by
hand from the Actions tab before one variable change turns the schedules on — and the same
change turns them off for a maintenance window without a commit.

**Failure visibility.** A non-zero exit fails the workflow run, which GitHub surfaces in the
Actions tab and by notification. There is no separate alerting; the HLD's falsification signal
(a stale archive without a failed run) is what this design must not allow.

## Package Layout

```
src/aqdt/
  __main__.py            # python -m aqdt → aqdt.pipeline.cli:main
  pipeline/
    cli.py               # argument parser, sub-commands, main()
    windows.py           # resolve_window, resolve_as_of
.github/workflows/
  ingest-purpleair.yaml
  ingest-airnow.yaml
  calibrate-fit.yaml
  calibrate-apply.yaml
tests/pipeline/          # window resolution, CLI dispatch (runs mocked), workflow files
```

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| CLI library | `argparse` (standard library) | `typer`; `click` | Six sub-commands with a handful of options; a dependency buys nothing here and the standard library has no version to track. |
| Entry point | Console script `aqdt` plus `python -m aqdt` | `python -m` only; per-component scripts | One name for every trigger to invoke; `-m` costs one file and helps when the script is not on `PATH`. |
| Where the clock enters | Only in resolving a routine window in the CLI | Components default their own windows; workflows compute windows in shell | Keeps every component a pure function of explicit bounds and puts the one clock read where it is logged and testable. Computing in shell would duplicate date arithmetic in YAML. |
| Trailing window length | 48 h for AirNow and apply | 24 h; since the last successful run | 48 h is AirNow's own revision horizon and covers a missed daily fit for apply; "since last run" needs state the pipeline deliberately has none of. |
| Fit `as_of` | Most recent 00:00 UTC | The current hour | Daily refits keyed on midnight are idempotent within the day and produce a readable fit history; calibration accepts any hour for backfill. |
| PostGIS loading | Automatic when `DATABASE_URL` is set; `--no-postgis` opts out | Explicit `--postgis` flag; never from the CLI | The environment already says whether a serving layer exists; a developer's Codespace run should refresh it without remembering a flag, and runners cannot load one. |
| Trigger | GitHub Actions cron, one workflow per run | One workflow with several crons branching on `github.event.schedule`; AWS EventBridge | Per-run workflows keep each schedule, its secrets, and its concurrency group readable in one file. The HLD records why Actions over AWS. |
| Where the single-writer guarantee lives | The observation store (compare-and-swap); concurrency groups are an efficiency measure | Concurrency groups as the guarantee; a lock service | A property of one trigger cannot protect every caller (a hand-run command, a dispatch backfill, a future orchestrator); the invariant is the store's and is enforced where the write happens. Groups stay because two identical runs in flight waste minutes. |
| Serializing fit and apply | Shared concurrency group `calibrate` | Separate groups; fit and apply as one job | Both aggregate the same sensor hours; a shared group keeps them from doing that work twice at once while keeping distinct schedules. |
| Cadence: PurpleAir 15 min | 15 minutes | 5; 30; 60 | Four snapshots an hour comfortably samples each hour; 5 minutes quadruples runner use and API points for no consumer that needs it; hourly risks an hour with a single snapshot when a run is late. |
| Runner installation | `uv sync --no-dev` | Full sync; a prebuilt container image | Runs need no test tooling; a container image is the AWS alternative's cost, not this one's. |
| Output | One JSON line per run on stdout | Human-readable summary; nothing | Machine-readable in logs, grep-able across runs, and a future orchestrator can parse it. |
| Retries | None at the CLI | Retry the run on failure | The clients already retry HTTP; a failed run should fail visibly and be retried by the next scheduled occurrence. |
| Activation | Job-level gate on the repository variable `AQDT_SCHEDULES_ENABLED`; dispatch runs ungated | Commit the `schedule` trigger only when ready; disable workflows in the UI | Landing and activating are different decisions with different reviewers (code review vs. "are the secrets right?"); a variable flips without a commit, is visible in the UI, and leaves the dispatch path open for manual verification. Disabling in the UI is per-workflow, undocumented in the repo, and silently reset by some workflow edits. |
| Secrets vs. variables | Credentials as secrets, configuration as variables | Everything as secrets | Variables are visible in the workflow UI, which is what a reader of a failed run wants for the bucket and bounding box; keys stay masked. |

## Open Questions & Future Decisions

### Resolved

1. ✅ The hour in progress is never part of a routine window; it is requested by the next run.
2. ✅ Apply does not wait for fit: an apply run that precedes a late fit is repaired by the next
   apply, whose trailing window covers the affected hours.
3. ✅ A run whose archive write succeeds but whose PostGIS load fails exits non-zero: the archive
   is intact and the next `aqdt db rebuild` repairs the serving layer, but the run did not do
   what was asked and must say so.
4. ✅ A queued scheduled run superseded by a newer queued run is reported by GitHub as
   cancelled, not failed; the newer run covers its window.
5. ✅ A scheduled run and a hand-started run may write the same partition at once; the store's
   compare-and-swap makes that safe, so no trigger-level guard is needed for correctness.

### Deferred

1. GitHub disables scheduled workflows on a repository with no commits for 60 days. A
   long-running collection period without code changes needs either periodic activity or a
   re-enable; decide when the project reaches steady state.
2. Runner minutes: the cadences assume a public repository (unlimited minutes). On a private
   repository the PurpleAir schedule alone would exceed the free tier; reduce cadence or
   revisit the AWS alternative.
3. PurpleAir API points: 96 snapshots a day over ~100 sensors and 9 fields is a measurable share
   of a free-tier points budget; confirm against the account's allocation after the first week.
4. Whether `workflow_dispatch` backfills should also accept a `--hours` input; start with
   explicit bounds only.
5. Whether a Codespace should run `aqdt db rebuild` on resume as well as on create; today a
   developer runs it by hand.
6. Whether the AWS principal used by runners should be scoped to the archive prefix only
   (`s3:GetObject`, `PutObject`, `ListBucket`, `DeleteObject` on `bucket/prefix/*`); verify the
   current user's policy before activation.

## References

- `docs/high-level-design.md` — Execution model; Environment; Data flow per run.
- `docs/intent/airnow-ingest/airnow-ingest-design.md` § Run — the 48-hour revision horizon.
- `docs/intent/calibration/calibration-design.md` § Runs — daily refit cadence; both runs write
  `sensor_hourly`.
- `docs/intent/observation-store/observation-store-design.md` — single writer per partition;
  `load_partitions`, `rebuild`, `DATABASE_URL`, `AQDT_ARCHIVE_URI`.
- GitHub Actions: `schedule` events, `concurrency`, repository secrets and variables.
