# High-Level Design: Air Quality Digital Twin

## Problem

Regulatory air-quality monitoring in the Washington, D.C. metro area is sparse: an EPA AirNow
bounding-box query over the metro returns on the order of five reference monitors, reporting hourly.
Low-cost crowdsourced PurpleAir sensors are dense — over a hundred in the same box, reporting every
~2 minutes — but individually unreliable: they need humidity correction, a meaningful fraction
exhibit hardware faults (one of the two internal laser channels drifting into the thousands of
µg/m³ while the other reads single digits), and their raw values are not directly comparable to
regulatory measurements.

Neither source alone gives a trustworthy, spatially continuous picture of PM2.5 across the metro.
A digital twin that fuses them — dense-but-noisy sensors calibrated against sparse-but-trusted
monitors, on a surface that can later be driven forward by NOAA HRRR wind and boundary-layer fields
— would.

The project is built incrementally as weekly assignments for GGS 590 (Digital Twins, George Mason
University). Each increment must land as a coherent, tested, reproducible step.

## Approach

### Observational fusion, built up in layers

The twin is a pipeline of data assets, each derived from the ones before it:

1. **Ingestion** pulls raw observations from each external source into an immutable archive.
2. **Quality control** annotates every observation with flags — never silently discarding — so
   downstream stages choose what to trust and the raw record is always recoverable.
3. **Calibration** corrects PurpleAir readings against nearby AirNow reference monitors, with
   distance-aware sensor-to-monitor matching.
4. **Fusion** produces an hourly PM2.5 surface over the metro (regression + kriging over the
   calibrated observations).
5. **Transport** (future) uses HRRR wind and boundary-layer fields to advect the surface forward
   as a short-range forecast.
6. **Evaluation** scores the surface against held-out reference monitors.

The current increment implements layers 1–2 for PurpleAir and AirNow. Layers 3–6 are designed at
this level only so that the ingestion and QC layer does not foreclose them.

### Idempotent, time-windowed batch

Every pipeline step is a pure function of *(inputs, time window)* that can be re-run for the same
window and produce the same result. The pipeline runs on demand or on a schedule; it is not a
resident daemon. This matches the development environment (GitHub Codespaces, which idles out and
is periodically rebuilt) and the way most scientific data pipelines are operated. It also makes
each step a ready-made asset for a workflow orchestrator once there are enough assets to justify
one.

### Domain-standard methods, interoperable formats

Where EPA, NOAA, or the atmospheric-science community has an established method or format, the
project uses it rather than inventing one: EPA's PurpleAir correction and channel-agreement criteria
(Barkjohn et al. 2021), AQS site identifiers, GeoParquet for point observations, PostGIS for spatial
query, CF-convention NetCDF/Zarr for gridded fields. Every stored artifact must be readable by
standard GIS and scientific tooling (QGIS, GeoPandas, xarray) without this project's code.

## Target Users

- **The pipeline author** (a single developer, weekly increments): needs each step to be
  independently testable, re-runnable, and inspectable, so that a week's work is a clean,
  demonstrable slice.
- **A reviewer or collaborator from an air-quality or GIS background**: needs to open the archive
  or the PostGIS tables in familiar tools and see raw values, QC flags, and provenance without
  reading Python.
- **Downstream pipeline stages** (calibration, fusion, transport): need a canonical observation
  schema with precise coordinates, stable source identifiers, timestamps, raw values, and QC flags,
  so that spatial joins and distance-aware matching are straightforward.

## Goals

- Every PurpleAir and AirNow observation for the D.C. metro bounding box is captured into a
  partitioned GeoParquet archive with its raw payload fields preserved.
- Every archived observation carries QC flags produced by domain-standard checks; the flag rate on
  PurpleAir channel disagreement is consistent with the observed hardware-fault rate (a few
  percent), and no AirNow flatline or malformed site identifier reaches downstream stages
  unflagged.
- AirNow site identifiers are normalized so that 100% of sites join to a single canonical site
  record.
- Re-running ingestion for the same time window is idempotent: the archive and the PostGIS tables
  are unchanged.
- The PostGIS serving layer is rebuildable from the archive with one command, and a fresh Codespace
  reaches a working, populated state from `postCreateCommand` alone.
- The observation schema carries everything the calibration layer needs for distance-aware
  matching: WGS84 coordinates at full source precision, source identity, timestamp, humidity,
  raw PM2.5 per channel.

## Non-Goals

- **No physical baseline concentration model.** The fusion layer is purely observational. There is
  no emissions-driven dispersion model (no equivalent of EPISODE in O'Regan et al. 2026) providing
  a prior PM2.5 surface. A simple emissions proxy may be scoped in a later phase; nothing in the
  current design assumes one.
- **No background / boundary PM2.5.** Nothing represents PM2.5 entering the domain from outside it
  (no equivalent of CAMS). This is a known limitation of the fused surface, recorded here so it is
  not mistaken for an oversight.
- **No resident real-time service.** "Real-time" means the latest available observations as of the
  most recent scheduled or on-demand run, not a continuously polling daemon. PurpleAir's ~2-minute
  native cadence is not captured continuously.
- **No workflow orchestrator in the current increment.** Steps are designed to be wrapped by one
  (Dagster is the intended target) but the orchestrator is a later increment.
- **No gridded data in PostGIS.** HRRR and any derived surfaces are stored as CF-convention
  NetCDF/Zarr and handled with xarray; PostGIS holds point observations and site metadata only.
- **No user-facing application.** Outputs are data assets consumable by QGIS, notebooks, and
  downstream stages.

## Tenets

Ordered: when two conflict, the higher one wins.

- **Flag, never drop.** When an observation fails a quality check, annotate it and keep it; the raw
  record stays recoverable and downstream stages decide what to trust.
- **Domain-standard over bespoke.** When EPA, NOAA, or the community has an established method,
  identifier scheme, or format, use it before inventing a project-specific one, even when the
  bespoke version would be simpler here.
- **Interoperable over convenient.** Stored artifacts must open in standard GIS and scientific
  tooling without this project's code; a format that only this pipeline can read is the wrong
  format.
- **Rebuildable over durable.** Any stored state must be reconstructible from the archive and the
  source APIs; treat databases and environments as disposable caches rather than systems of
  record.

## System Design

```mermaid
flowchart LR
    subgraph sources [External sources]
        PA[PurpleAir API v1<br/>/sensors, bbox]
        AN[AirNow API<br/>/aq/data, BBOX, hourly]
        HR[NOAA HRRR<br/>GRIB2 — future]
    end

    subgraph ingest [Ingestion + QC — current increment]
        PAI[purpleair-ingest<br/>fetch, parse, A/B + humidity QC]
        ANI[airnow-ingest<br/>fetch, parse, flatline + site-ID QC]
    end

    subgraph store [Observation store]
        ARC[(GeoParquet archive<br/>partitioned by source/date)]
        PG[(PostGIS<br/>sites, observations)]
    end

    subgraph future [Later increments]
        CAL[Calibration<br/>distance-aware matching]
        FUS[Fusion<br/>regression + kriging]
        TRN[Transport<br/>HRRR-driven]
        EVL[Evaluation<br/>held-out monitors]
        ZR[(Zarr / NetCDF<br/>gridded fields)]
    end

    PA --> PAI --> ARC
    AN --> ANI --> ARC
    ARC -->|load| PG
    ARC --> CAL --> ARC
    CAL --> FUS --> EVL
    HR --> ZR --> TRN
    FUS --> ZR
```

### Components

**Source ingesters** (one per external source). Each owns everything specific to its source: API
client and authentication, bounding-box query, parsing the source's native payload, the source's
domain-standard QC rules, and mapping to the canonical observation schema. Rules that exist because
of one source's quirks live with that source. Current ingesters: `purpleair-ingest`,
`airnow-ingest`.

**Observation store.** Owns the canonical observation and site schemas, the shared QC flag
vocabulary, the GeoParquet archive layout (partitioning, file naming, idempotent writes), and the
load into PostGIS. Ingesters write to the store; nothing else does. The archive, held in S3, is the
system of record; PostGIS, running inside the Codespace, is a rebuildable serving layer for
spatial query and GIS tooling.

**Calibration, fusion, transport, evaluation** — later increments, each a separate component
reading from the archive and writing its products back through the store's primitives, so that
PostGIS serves every layer's output and stays rebuildable from the archive alone. Named here so
the store's schema is designed for them.

### Data flow per run

A run is parameterized by a source and a time window. For AirNow the window maps directly to the
API's date range and a run backfills any hours the archive lacks. For PurpleAir the `/sensors`
endpoint returns each sensor's latest reading only, so a run captures a snapshot; time-series depth
for PurpleAir accumulates through repeated runs, and snapshot polls that return an unchanged reading
(same sensor, same `last_seen`) do not create duplicate observations.

History serves two distinct needs with different depth requirements. Fitting calibration models and
evaluating the fused surface need a deep archive, built up during an initial polling period.
Operating the twin thereafter needs only a bounded recent window (the latest observations plus
enough history for flatline detection and the current calibration fit). The archive keeps
everything; operational stages read a window, so history depth is never a runtime prerequisite.

### Environment

Development and execution happen in a GitHub Codespace defined by the devcontainer. A Codespace
and everything inside it — workspace files, Docker volumes — is deleted after a period of
inactivity, so nothing inside it is durable. The GeoParquet archive therefore lives in an S3
bucket; PostGIS runs as a docker-compose sidecar service and `postCreateCommand` creates the
schema and loads the archive from S3, so a new Codespace comes up populated. API keys, AWS
credentials, and the archive URI are supplied as environment variables (Codespaces secrets),
never files. CI (GitHub Actions) runs lint and tests against a Postgres+PostGIS service container
and an in-process S3 mock, with no cloud credentials.

### Design tree

Depth-2: this HLD over flat leaf LLDs under `docs/intent/`, one folder per leaf, each owning its
EARS prefix.

| Leaf | Prefix | Owns |
|---|---|---|
| `observation-store` | `OBS` | canonical schemas, flag vocabulary, archive layout, PostGIS load |
| `purpleair-ingest` | `PA` | PurpleAir client, parsing, A/B agreement and humidity QC |
| `airnow-ingest` | `AN` | AirNow client, parsing, flatline and site-identifier QC |
| `calibration` | `CAL` | hourly aggregation, distance-aware sensor-to-monitor matching, per-sensor fits (designed; a later increment) |

Later increments add leaves (`fusion`, `transport`, `evaluation`) beside these.

## Key Design Decisions

### Execution model: idempotent time-windowed batch, orchestrator deferred

Each step is a function of *(inputs, time window)*; runs are triggered on demand or by a schedule
and are safe to repeat. A workflow orchestrator (Dagster) is the intended wrapper once the asset
graph has three or four nodes; adopting it in the first increment would spend the increment on
setup before there is a graph to show. A resident asynchronous poller was rejected because the
Codespace environment cannot keep one alive and it adds testing complexity without domain value.
A GitHub-Actions-only schedule that commits data to git was rejected as a primary mechanism
because runner cadence is too coarse and best-effort for PurpleAir, though it remains an acceptable
trigger for hourly AirNow runs.

### Persistence: GeoParquet archive as system of record, PostGIS as serving layer

Point observations are written to a partitioned GeoParquet archive in S3 (durable, reproducible,
readable by GeoPandas/DuckDB/QGIS directly from object storage) and loaded into PostGIS (spatial
SQL for distance-aware matching, native QGIS connectivity). Two stores cost a load step and a sync
to keep tested; the alternative of PostGIS alone was rejected because the Codespace database does
not outlive the Codespace and the archive is what makes the twin reproducible. Keeping the archive
in the workspace or in git was rejected for the same reason plus binary churn on every run. GeoParquet alone was rejected because
sensor-to-monitor matching and site joins are spatial queries best expressed in SQL. DuckDB with
its spatial extension was rejected: its advantage (no service to run) does not apply once
docker-compose sidecars are available, and PostGIS is the interoperability target for GIS tooling.

### QC: source-specific rules, shared flag vocabulary, flag-never-drop

Each ingester applies the checks its source needs, using published criteria where they exist
(Barkjohn et al. 2021 for PurpleAir channel agreement and humidity correction). Results are
recorded as flag columns on the observation; raw values are never modified or removed. The flag
vocabulary is shared across sources so downstream stages filter uniformly. A generic pluggable QC
framework was rejected as premature for two sources; a deferred second-pass QC stage was rejected
because it leaves unflagged faulty data live in the store between passes; filtering invalid rows at
ingestion was rejected because it destroys provenance.

### Fusion is observational; no dispersion baseline

The reference architecture (O'Regan et al. 2026) fuses sensors and monitors on top of a physical
dispersion model's baseline surface. This project has no such model — HRRR supplies transport
fields, not a concentration prior — and the fusion layer is designed to work from observations
alone. Scoping in a simple emissions proxy is a possible later decision, not a design assumption.

### Calibration is distance-aware from the outset

O'Regan et al. calibrated every sensor against a single reference monitor regardless of distance
(up to ~12 km). The D.C. metro is larger and has several monitors, so each PurpleAir sensor is
matched to reference monitors by distance (nearest, or distance-weighted within a radius). The
calibration layer is a later increment; the decision is recorded now because it constrains the
observation schema (full-precision coordinates, stable site identity). Distances are computed
from the archive in GeoPandas rather than in PostGIS so that calibration stays a pure function of
the archive; PostGIS serves the results.

### Schemas: Pydantic for records, Pandera for frames

Validation happens where data crosses a boundary, and the two kinds of boundary get the library
built for them.

- **Pydantic** validates *objects*: each source's parsed API payload, the canonical `Site` and
  `Observation` records an ingester constructs, enumerations such as the QC flag vocabulary, and
  configuration. Pydantic models also yield JSON Schema, which constrains the inputs and outputs of
  any future AI/LLM component in the pipeline (structured outputs against the observation schema
  rather than free text).
- **Pandera** validates *dataframes*: every archive partition on write and read, and every frame
  handed to calibration, fusion, or evaluation. Pandera expresses what a record model cannot —
  uniqueness of `(site_id, observed_at)`, sortedness, cross-column consistency, geometry column
  type — and validates vectorized, which matters once the archive is deep.

The two definitions of the same schema must agree. Where one can be derived from the other, derive
it; where it cannot (frame-level checks have no record-level source), a conformance test asserts
that the Pydantic and Pandera definitions name the same columns with the same nullability and
compatible types. Using Pydantic alone was rejected because per-record validation is the wrong
shape for bulk reads and cannot state collection invariants; Pandera alone was rejected because it
is awkward for nested API payloads and cannot export JSON Schema. Plain dataclasses were rejected
for lacking validation and schema export.

### Gridded fields live outside PostGIS

HRRR inputs and any fused/forecast surfaces are CF-convention NetCDF or Zarr handled with xarray.
Storing rasters in a relational database was rejected: the scientific tooling for gridded
atmospheric data is the xarray ecosystem, and PostGIS raster support is a poor fit for
time-stepped model output.

## Success Metrics

**Current increment (ingestion + QC):**

- A fresh Codespace reaches a populated, queryable PostGIS from `postCreateCommand` alone.
- Two consecutive runs over the same window, with unchanged upstream data, produce byte-identical
  archive partitions and no new rows in PostGIS.
- Every PurpleAir observation whose channels disagree beyond the Barkjohn criteria carries the
  disagreement flag; the flagged fraction is a few percent, matching the observed fault rate.
- Every AirNow site record has a normalized identifier; every AirNow flatline (a run of identical
  values over several consecutive hours) carries the flatline flag.
- Every archived observation can be traced to the raw payload fields it came from.

**Falsification signals:**

- A channel-B hardware fault reaches a downstream stage unflagged.
- A rebuild of PostGIS from the archive differs from the live tables.
- An artifact in the store cannot be opened in QGIS or GeoPandas without project code.

**Later increments:** leave-one-out cross-validation of the fused surface against held-out AirNow
monitors, reported against FAIRMODE model-quality objectives.

## References

- PurpleAir API v1 — `https://api.purpleair.com/v1/sensors` (bounding-box query via
  `nwlng`/`nwlat`/`selng`/`selat`; `X-API-Key` header).
- EPA AirNow API — `https://www.airnowapi.org/aq/data/` (`BBOX`, `dataType=A` hourly averages;
  data labeled preliminary/unvalidated).
- Barkjohn, K. K., Gantt, B., and Clements, A. L. (2021). *Development and application of a United
  States-wide correction for PM2.5 data collected with PurpleAir sensors.* Atmospheric Measurement
  Techniques, 14, 4617–4637.
- O'Regan et al. (2026). Fusion of PurpleAir sensors, regulatory monitors, and the EPISODE
  dispersion model via regression kriging for Cork, Ireland. *Environment International.*
  Reference architecture; deviations recorded under Key Design Decisions.
- NOAA HRRR (High-Resolution Rapid Refresh) — wind and boundary-layer fields for the transport
  layer.
- FAIRMODE model-quality objectives — evaluation standard for the fusion layer.
- GeoParquet specification; CF (Climate and Forecast) metadata conventions.
