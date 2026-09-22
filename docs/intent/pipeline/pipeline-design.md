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
retry requests; the schedule's next occurrence retries the run). A run whose archive write
succeeds but whose PostGIS load fails also exits non-zero: the archive is intact and the next
`aqdt db rebuild` repairs the serving layer, but the run did not do what was asked and must
say so.

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

## Workflows and Cadence

One workflow per run under `.github/workflows/`, each declaring `workflow_dispatch` as its only
trigger, with optional `start`/`end` or `as_of` inputs where the command takes a window (an empty
input means "use the routine window"). Every run reaches its workflow the same way, whether an
EventBridge schedule dispatched it or a person did.

| Workflow | Cadence (UTC) | Command | Concurrency group |
|---|---|---|---|
| `ingest-purpleair.yaml` | every 15 minutes | `aqdt ingest purpleair` | `ingest-purpleair` |
| `ingest-airnow.yaml` | hourly at :20 | `aqdt ingest airnow` | `ingest-airnow` |
| `calibrate-fit.yaml` | daily at 00:30 | `aqdt calibrate fit` | `calibrate` |
| `calibrate-apply.yaml` | hourly at :40 | `aqdt calibrate apply` | `calibrate` |

Cadence rationale: PurpleAir every 15 minutes gives about four snapshots per sensor-hour, enough
for hourly aggregation (which imposes no minimum) at 96 requests a day. AirNow at 20 past the
hour leaves the feed time to publish the hour just ended. Fit at 00:30 runs once the day's last
AirNow hour is archived; apply at :40 follows both ingesters each hour and, on the first hour of
the day, the new fit.

The rationale above is this component's; the cadences themselves are specified once, in the
infrastructure segment, as the cron expressions of the EventBridge schedules that realize them —
together with the switch that turns those schedules on and off (infrastructure LLD § Dispatch
stack). Changing a cadence is a change there, argued from the reasoning here.

Each workflow: checks out the repository, installs `uv` and the project (`uv sync --no-dev`),
and runs the command. Every job has `timeout-minutes` under its cadence interval (PurpleAir 10,
AirNow 20, fit 30, apply 20) so a hung run cannot pile up behind itself. A dispatch names the ref
it runs from, so a workflow change takes effect when it lands on `main`.

**Concurrency.** Correctness under concurrent writers is the observation store's: its partition
writes are compare-and-swap, so a run started by a schedule, by hand, or by anything else can
never lose another writer's rows (observation-store LLD § One writer per partition). The
concurrency groups exist to keep two runs of the same workflow from both spending runner minutes
on overlapping windows and interleaving their logs: each ingester is serialized with itself, and
fit and apply share the `calibrate` group because both aggregate the same sensor hours.
`cancel-in-progress` is false everywhere — a run in progress is never interrupted; a queued run
may be superseded by a newer queued run, which GitHub reports as cancelled rather than failed and
which is harmless because the newer run's window covers it.

**Configuration on runners.** Credentials are repository secrets: `PURPLEAIR_API_KEY`,
`AIRNOW_API_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`. Non-secret configuration is
repository variables: `AQDT_ARCHIVE_URI`, `AWS_DEFAULT_REGION`. The bounding box is the store's
`DC_METRO` default and is not configured on runners. Each workflow maps
only what its command needs into the job environment. Nothing is written to the repository by a
run; the archive is in S3.

**Activation.** A workflow runs whatever dispatch it receives; no job carries an activation
condition. Because every run now arrives as a `workflow_dispatch` event, a condition here could
not tell a scheduled run from a hand-started one, so scheduled runs are turned on and off at their
source instead — by disabling the EventBridge schedules (infrastructure LLD § Dispatch stack).
Running a command by hand from the Actions tab therefore works whatever state the schedules are
in, which is what makes a maintenance window safe to hold.

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

## Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| CLI library | `argparse` (standard library) | Six sub-commands with a handful of options; the standard library covers them with no dependency version to track. |
| Entry point | Console script `aqdt` plus `python -m aqdt` | One name for every trigger to invoke; `-m` costs one file and helps when the script is not on `PATH`. |
| Where the clock enters | Only in resolving a routine window in the CLI | Keeps every component a pure function of explicit bounds and puts the one clock read where it is logged and testable. |
| Trailing window length | 48 h for AirNow and apply | 48 h is AirNow's own revision horizon and covers a missed daily fit for apply, and it needs no record of the last run — state the pipeline deliberately keeps none of. |
| Fit `as_of` | Most recent 00:00 UTC | Daily refits keyed on midnight are idempotent within the day and produce a readable fit history; calibration accepts any hour for backfill. |
| PostGIS loading | Automatic when `DATABASE_URL` is set; `--no-postgis` opts out | The environment already says whether a serving layer exists; a developer's Codespace run refreshes it without remembering a flag, and runners have none to load. |
| Trigger | AWS EventBridge Scheduler → dispatch Lambda → `workflow_dispatch`, one workflow per run | Per-run workflows keep each run's command, secrets, and concurrency group readable in one file. The HLD records why scheduling is external to GitHub Actions. |
| Where the single-writer guarantee lives | The observation store (compare-and-swap); concurrency groups are an efficiency measure | The invariant must hold for every caller — a hand-run command, a dispatch backfill, a future orchestrator — so it is enforced where the write happens. Groups stay because two identical runs in flight waste minutes. |
| Serializing fit and apply | Shared concurrency group `calibrate` | Both aggregate the same sensor hours; a shared group keeps them from doing that work twice at once while keeping distinct schedules. |
| Cadence: PurpleAir 15 min | 15 minutes | Four snapshots an hour comfortably samples each hour, without multiplying runner use and API points for a consumer that needs neither, and without risking an hour with a single snapshot when a run is late. |
| Runner installation | `uv sync --no-dev` | Runs need no test tooling. |
| Output | One JSON line per run on stdout | Machine-readable in logs, grep-able across runs, and parseable by a future orchestrator. |
| Retries | None at the CLI | The clients already retry HTTP; a failed run should fail visibly and be retried by the next scheduled occurrence. |
| Activation | No job-level gate; scheduled runs are enabled and disabled through the EventBridge schedules' own state | Once every run arrives as a `workflow_dispatch` event, a condition on the event type cannot tell a scheduled run from a hand-started one. Turning runs off at their source creates no workflow run to skip and leaves manual dispatch working throughout a maintenance window. |
| Secrets vs. variables | Credentials as secrets, configuration as variables | Variables are visible in the workflow UI, which is what a reader of a failed run wants for the bucket and bounding box; keys stay masked. |

## Open Questions & Future Decisions

1. Runner minutes: the cadences assume a public repository (unlimited minutes). On a private
   repository the PurpleAir cadence alone would exceed the free tier; reduce cadence or move
   execution off GitHub Actions.
2. PurpleAir API points: 96 snapshots a day over ~100 sensors and 9 fields is a measurable share
   of a free-tier points budget; confirm against the account's allocation after the first week.
3. Whether `workflow_dispatch` backfills should also accept a `--hours` input; start with
   explicit bounds only.
4. Whether a Codespace should run `aqdt db rebuild` on resume as well as on create; today a
   developer runs it by hand.
5. Whether the AWS principal used by runners should be scoped to the archive prefix only
   (`s3:GetObject`, `PutObject`, `ListBucket`, `DeleteObject` on `bucket/prefix/*`); verify the
   current user's policy before activation.

## References

- `docs/high-level-design.md` — Execution model; Environment; Data flow per run.
- `docs/intent/airnow-ingest/airnow-ingest-design.md` § Run — the 48-hour revision horizon.
- `docs/intent/calibration/calibration-design.md` § Runs — daily refit cadence; both runs write
  `sensor_hourly`.
- `docs/intent/observation-store/observation-store-design.md` — single writer per partition;
  `load_partitions`, `rebuild`, `DATABASE_URL`, `AQDT_ARCHIVE_URI`.
- `docs/intent/infrastructure/infrastructure-design.md` — the EventBridge schedules that render
  these cadences, the Lambda that dispatches these workflows, and the switch that enables them.
- GitHub Actions: `workflow_dispatch` events and inputs, `concurrency`, repository secrets and
  variables.
