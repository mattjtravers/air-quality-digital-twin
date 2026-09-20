---
parent: high-level-design
prefix: OBS
---

# Observation Store

## Context and Design Philosophy

The observation store is the one place where point observations from every source converge. It
owns the canonical `Site` and `Observation` schemas, the QC flag vocabulary all ingesters share, the
GeoParquet archive that is the system of record, and the load of that archive into PostGIS for
spatial query and GIS tooling. Source ingesters produce validated `Observation` and `Site` records
and hand them to the store; nothing else writes observations or sites.

The store also owns the *mechanics* of partitioned GeoParquet writing and PostGIS upserting, and
exposes them as primitives so that downstream components (calibration, fusion) write their derived
products with the same determinism, atomicity, and validation guarantees under their own archive
prefixes and tables. Those components own their products' schemas; the store owns how any product
lands on disk and in the database.

Guiding principles, in order of precedence when they conflict:

1. **The archive is truth; PostGIS is a cache.** The archive lives in durable object storage (S3)
   that outlives any Codespace. Anything in PostGIS can be regenerated from the archive with one
   command. Nothing is ever written to PostGIS that is not first in the archive.
2. **Deterministic writes.** Given the same input records, a write produces the same bytes on
   disk. Re-running a window is safe; nothing in a stored row depends on when it was written.
3. **Raw stays raw.** Every observation keeps its source's raw values and the raw source record it
   was parsed from. QC adds flags; it never edits or removes values.
4. **Open in anything.** GeoParquet with a standard geometry column and PostGIS with EPSG:4326
   point geometry — both readable by QGIS, GeoPandas, and DuckDB without this project's code.

## Canonical Schemas

The canonical schema exists in two forms that describe the same columns:

- **Record models** (Pydantic, `aqdt.observation_store.schemas`): `Site` and `Observation`, plus
  the enumerations. Validation runs when an ingester constructs a record from a source payload;
  the store trusts constructed records.
- **Frame models** (Pandera, `aqdt.observation_store.frames`): `SitesFrame` and
  `ObservationsFrame`, the GeoDataFrame shape of the same records. Validation runs on every
  archive partition written or read, and on every frame the store hands to a downstream stage.

The record models are the source of truth for column names, types, and nullability. The frame
models restate those columns and add what only a frame can express (see "Frame Models"). A
conformance test in the store's test suite asserts that every field of `Site` and `Observation`
appears in the corresponding frame model with matching nullability and a compatible dtype, and
that the frame models add no data columns beyond `geometry`. Pandera's checks can be generated
from Pydantic field constraints where a one-to-one mapping exists (numeric bounds, enum
membership); this is done where it removes duplication and not otherwise.

### Enumerations

```
Source     = purpleair | airnow
SiteType   = low_cost_sensor | reference_monitor
QcFlag     = see "QC Flag Vocabulary"
```

### Site

A fixed measurement location. One row per `site_id`; holds the site's most recently written
metadata. The site's latest observation time is not stored here — it is `max(observed_at)` over
the site's observations, computed by whoever needs it — so that re-writing a site from an older
window cannot move it backwards.

| Field | Type | Notes |
|---|---|---|
| `site_id` | `str` | Canonical, namespaced: `purpleair:{sensor_index}` or `airnow:{normalized AQS code}`. Primary key. |
| `source` | `Source` | |
| `source_native_id` | `str` | Identifier exactly as the source returned it (e.g. the un-normalized AQS code). |
| `site_type` | `SiteType` | `low_cost_sensor` for PurpleAir, `reference_monitor` for AirNow. |
| `name` | `str \| None` | Source-provided display name. |
| `latitude` | `float` | WGS84 decimal degrees, full source precision, never rounded. |
| `longitude` | `float` | WGS84 decimal degrees, full source precision, never rounded. |

### Observation

One measurement of PM2.5 at one site at one instant.

| Field | Type | Notes |
|---|---|---|
| `site_id` | `str` | Foreign key to `Site`. |
| `source` | `Source` | Denormalized for partition routing and filtering. |
| `observed_at` | `datetime` | UTC, tz-aware. PurpleAir: `last_seen`. AirNow: the UTC hour start. |
| `latitude` | `float` | Coordinates at the time of observation (sites can be relocated). |
| `longitude` | `float` | |
| `pm25_raw` | `float \| None` | The source's primary PM2.5 value, uncorrected, µg/m³. |
| `pm25_channel_a` | `float \| None` | PurpleAir only. |
| `pm25_channel_b` | `float \| None` | PurpleAir only. |
| `humidity` | `float \| None` | Percent relative humidity, where the source reports it. |
| `pm25_corrected` | `float \| None` | Source-standard corrected value where one exists (PurpleAir: EPA/Barkjohn). Null where no correction applies or the ingester withheld it. |
| `qc_flags` | `list[QcFlag]` | Empty list means every check passed. Order-independent; stored de-duplicated and sorted alphabetically by flag name. |
| `raw` | `dict[str, Any]` | The source record this observation was parsed from, unmodified. |

Composite key: `(site_id, observed_at)`. `Observation.is_trusted` is `qc_flags == []`.

### IngestSummary

What every ingester returns from a run, so callers and the future orchestrator see one shape:

| Field | Type | Notes |
|---|---|---|
| `source` | `Source` | |
| `fetched` | `int` | Rows in the source payload(s). |
| `rejected` | `dict[str, int]` | Boundary-rejection reason → count. |
| `written` | `int` | Observations handed to the store, including any hours re-emitted for lookback. |
| `flagged` | `dict[QcFlag, int]` | Flag → number of observations carrying it. |
| `partitions` | `list[str]` | Archive partition URIs touched. |
| `snapshot_at` | `datetime \| None` | Snapshot sources: the payload's own timestamp. |
| `window_start`, `window_end` | `datetime \| None` | Windowed sources: the requested window, half-open. |

Invariant: `fetched == written + sum(rejected.values())` — every source row is either an
observation handed to the store or a counted rejection. The model enforces it, and
`window_start < window_end`, on construction, so every ingester inherits the check.

### BoundingBox

The spatial extent every ingester queries, parsed once from `AQDT_BBOX`
(`nwlng,nwlat,selng,selat`, WGS84) by `BoundingBox.parse`. Validation: each coordinate in range, `nwlat > selat`,
`nwlng < selng`. Lives here because it is part of the canonical geospatial vocabulary rather than
any one source's contract; each ingester renders it into its own API's parameter names.

### Time windows

Every time window in the project — archive reads, ingestion windows, summary fields, and the
windows downstream components aggregate over — is half-open: `[start, end)` includes `start` and
excludes `end`. Consecutive windows therefore partition time without sharing a boundary instant.
An ingester whose source API takes an inclusive end renders it as `end − (one source interval)`.

Not in the schema, by design: any ingestion timestamp, run identifier, or host detail. Their
absence is what makes archive writes deterministic.

### QC Flag Vocabulary

A single enumeration shared by every ingester. Ingesters raise only the flags that apply to their
source; the vocabulary is the registry of what a flag means.

| Flag | Meaning |
|---|---|
| `missing_value` | The primary PM2.5 value is absent. |
| `out_of_range` | A value lies outside physically plausible bounds for its field. |
| `channel_missing` | A dual-channel sensor reported only one channel. |
| `channel_disagreement` | Dual-channel readings differ beyond the source's published agreement criteria. |
| `flatline` | The value is identical across a run of consecutive observations at least as long as the source's threshold. |
| `site_id_unresolved` | The source's site identifier could not be normalized to a canonical `site_id`. |

Every flag is a function of the observation and its neighbours in the archive, never of when the
run happened. Staleness (how old the latest observation is) is a query-time property computed
from `observed_at` by whoever needs it; it is not a stored flag.

Adding a flag is backward compatible (existing rows simply do not carry it). Renaming or removing
one is a schema change that requires an archive rewrite.

### Frame Models

`ObservationsFrame` and `SitesFrame` are Pandera `DataFrameModel`s over GeoDataFrames. Beyond
restating the record columns and dtypes, they carry the invariants a partition or a downstream
frame must satisfy:

| Invariant | Applies to |
|---|---|
| `(site_id, observed_at)` is unique | `ObservationsFrame` |
| `site_id` is unique | `SitesFrame` |
| Rows are sorted by `(site_id, observed_at)` / `site_id` | both (archive partitions only; downstream frames may be reordered) |
| `geometry` is a `Point` in EPSG:4326 equal to `(longitude, latitude)` | both |
| `observed_at` is tz-aware UTC | `ObservationsFrame` |
| `qc_flags` elements are members of `QcFlag` | `ObservationsFrame` |
| `pm25_channel_a` and `pm25_channel_b` are both null or both non-null, or `channel_missing` is in `qc_flags` | `ObservationsFrame` |
| `latitude` in [-90, 90], `longitude` in [-180, 180] | both |

A frame that fails validation is never written to the archive and never returned from a read;
the error names the failing check and rows.

`frames.py` also holds the record↔frame bridge: `observations_to_frame(records)` and
`sites_to_frame(records)` build the GeoDataFrame (deriving `geometry`, keeping `raw` as a dict
column — JSON serialization happens only at the archive boundary), and `Observation.from_frame`
is the inverse. The frame models never check row order; `validate_partition(frame, model)` runs
the model and then the sortedness check, and is what the archive calls on the way in and out.
That split is how "sortedness applies to partitions only" is expressed in code.

## GeoParquet Archive

### Layout

```
{archive_uri}/
  source={source}/
    sites.parquet
    date={YYYY-MM-DD}/
      observations.parquet
  calibration/...          # derived products, laid out by their own components
```

- `archive_uri` comes from the `AQDT_ARCHIVE_URI` environment variable and is any `fsspec` URI.
  The canonical archive is `s3://<bucket>/<prefix>`; a local path (`file://` or bare) is used for
  tests and offline development. The store resolves the URI to a filesystem once and every
  read/write goes through it, so the code path is identical for both.
- Every partition directory is Hive-style `key=value`, so pyarrow and DuckDB discover the
  partitioning without hints. `source` is the record's `source`; `date` is the UTC calendar date
  of `observed_at`. Datetime-valued keys elsewhere in the archive render as `YYYY-MM-DDTHH`.
- Observation and site files are GeoParquet: a `geometry` column of WGS84 points derived from
  `longitude`/`latitude`, plus every schema field as a plain column. `qc_flags` is a list of
  strings; `raw` is a JSON string column. Derived products that carry no coordinates (they
  reference sites by `site_id`) are plain Parquet.
- Rows are sorted by `(site_id, observed_at)` before writing.

### Write semantics

`write_observations(records, archive_uri)`:

1. Build a frame from the incoming records and validate it against `ObservationsFrame` (minus the
   sortedness check). Duplicate `(site_id, observed_at)` keys within one call fail validation —
   incoming-wins merging is defined across runs, not within a batch, and a duplicate inside a batch
   is an ingester bug.
2. Group by `(source, date)`.
3. For each partition: read the existing object if present, merge on `(site_id, observed_at)` with
   the incoming row winning, sort, validate the merged frame, and write the whole partition as one
   object. On a local filesystem this is a temporary file renamed over the target; on S3 a single
   PUT is atomic (a partial object is never visible). Either way a crash mid-write leaves the
   previous partition intact.
4. Return the list of partition URIs touched.

A re-emitted row may change derived fields (`qc_flags`, `pm25_corrected`) — a later run can learn
something about an earlier hour, as flatline detection does — but the raw fields
(`pm25_raw`, channels, `humidity`, coordinates, `raw`) of a re-emitted row are expected to equal
the stored ones when upstream is unchanged. `raw` is serialized with sorted keys and default float
formatting so identical upstream records produce identical bytes.

`write_sites(records, archive_uri)`: same merge-and-replace on `site_id`, one file per source.

Incoming-wins merging is what makes the archive follow upstream revisions: AirNow marks its data
preliminary and may revise an hour on a later query, and the archive should carry the revised
value. Two runs that receive identical upstream data write identical bytes.

The archive assumes a single writer per partition at a time; object storage offers no lock
around read-merge-write, so two concurrent runs on the same partition would lose one run's rows.
Serializing runs is the caller's responsibility (today: one process; later: the orchestrator).

An empty record set writes nothing and touches no files.

### Partition primitives for derived products

Every archive product — observations, sites, and each downstream component's outputs — is
described by one `Product` definition and written and read by two primitives.

`Product(prefix, partition_keys, key_cols, frame_model, filename, table, sql_dir)`:

- `prefix` — path under `archive_uri` (`""` for observations and sites, `calibration/fits` for
  calibration fits).
- `partition_keys` — an ordered mapping of key name → function of the frame returning each
  row's partition value (`{"source": lambda f: f.source, "date": lambda f:
  f.observed_at.dt.date}`); the store renders values to path segments (dates as `YYYY-MM-DD`,
  datetimes as `YYYY-MM-DDTHH`, strings and enumerations as their value). Keys are derived, not
  stored as columns, so a frame model never has to carry them.
- `key_cols` — the merge key within a partition.
- `frame_model` — the Pandera model every partition is validated against.
- `filename` — the object name inside a partition directory.
- `table`, `sql_dir` — the PostGIS table the product loads into and the directory of ordered SQL
  files that create it.

`write_partitioned(frame, archive_uri, product)` validates `frame` against `product.frame_model`
(all checks except sortedness), renders the partition keys, groups rows by them, and for each
group performs the read-merge-write described above on `key_cols`, writing one object at
`{archive_uri}/{prefix}/{k1}={v1}/.../{filename}`. Returns the partition URIs written.
`read_partitioned(archive_uri, product, **filters)` reads every partition whose rendered keys
satisfy the filters — each filter is a literal (equality) or a predicate on the rendered value —
and returns one frame validated against `product.frame_model`, or an empty typed frame when
nothing matches. `write_observations`, `write_sites`, `read_observations`, and `read_sites` are
thin wrappers over these with the observation and site products.

Both primitives carry the same guarantees as the observation writes: deterministic bytes for
identical input, one atomic object per partition, single writer per partition, validation on the
way in and out. A product whose frame has a `geometry` column is written as GeoParquet; one
without is plain Parquet.

The registry `aqdt.registry.PRODUCTS` lists every product in the project in load order —
`sites`, `observations`, then each downstream component's products. It is the one place the
PostGIS layer looks to learn what to create and load; a component adds its products there and
nowhere else.

### Read semantics

`read_observations(archive_uri, source=None, start=None, end=None)` returns a validated
`ObservationsFrame` GeoDataFrame from every partition whose date intersects `[start, end)`,
filtered to `start <= observed_at < end`. `read_sites(archive_uri, source=None)` likewise returns
a `SitesFrame`. A read that matches no partition is not an error — a fresh archive or an
unpopulated window is a normal state — so it logs the request at INFO and returns an empty frame
with the model's columns and dtypes. Frames are the read type; callers needing record models
(rare — re-emitting rows with updated flags) convert with `Observation.from_frame(frame)`.

## PostGIS Serving Layer

### Schema

```sql
sites (
  site_id            text primary key,
  source             text not null,
  source_native_id   text not null,
  site_type          text not null,
  name               text,
  latitude           double precision not null,
  longitude          double precision not null,
  geom               geometry(Point, 4326) not null
)
-- index: gist(geom)

observations (
  site_id            text not null references sites(site_id),
  source             text not null,
  observed_at        timestamptz not null,
  latitude           double precision not null,
  longitude          double precision not null,
  pm25_raw           double precision,
  pm25_channel_a     double precision,
  pm25_channel_b     double precision,
  humidity           double precision,
  pm25_corrected     double precision,
  qc_flags           text[] not null,
  raw                jsonb not null,
  geom               geometry(Point, 4326) not null,
  primary key (site_id, observed_at)
)
-- indexes: gist(geom), btree(observed_at), btree(source, observed_at)
```

The schema lives as ordered SQL files under `src/aqdt/observation_store/sql/` and is applied by
`apply_schema(conn)`, which is idempotent (`create ... if not exists`).

### Load

Data flows one way, archive → PostGIS, through three functions driven by the product registry:

- `apply_schema(conn)` applies every registered product's SQL files, in registry order, with
  `create ... if not exists`.
- `load_partitions(conn, archive_uri, partition_uris)` reads each named partition, identifies its
  product from its path, and upserts it (`insert ... on conflict do update` on the product's
  `key_cols`) in one transaction per partition, in registry order so that sites land before
  anything that references them. This is how a run refreshes PostGIS: every run entry point —
  the ingesters' and the downstream components' — accepts an optional `conn` and, when one is
  given, ends by loading the partitions it just wrote. Without a `conn` a run touches the archive
  only.
- `rebuild(conn, archive_uri)` truncates every registered table and loads every partition of
  every product. This is the one-command recovery the HLD requires and what `postCreateCommand`
  runs, so a brand-new Codespace comes up populated from S3.

Upserting is internal to the store; no component writes to PostGIS by any other path. Connection
is `DATABASE_URL` from the environment (docker-compose default in the Codespace).

### Environment

- Configuration is environment variables only, never files in the repo:

  | Variable | Purpose | Where set |
  |---|---|---|
  | `AQDT_ARCHIVE_URI` | Archive root (`s3://bucket/prefix`) | Codespaces secret |
  | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_DEFAULT_REGION` | S3 credentials | Codespaces secrets |
  | `DATABASE_URL` | PostGIS connection | docker-compose default |

  An archive operation invoked without an explicit `archive_uri` reads `AQDT_ARCHIVE_URI`; if it
  is unset the operation fails immediately with an error naming the variable, rather than falling
  back to a local path that would silently split the archive.

- Devcontainer: `docker-compose.yml` with the app container and a `postgis/postgis` service;
  `devcontainer.json` references it via `dockerComposeFile`. `postCreateCommand` runs
  `uv sync --all-groups`, `apply_schema`, and `rebuild`.
- CI: GitHub Actions `services:` block running the same `postgis/postgis` image. Archive tests run
  against a temporary local directory; the S3 code path is exercised with `moto` (in-process S3
  mock), so CI needs no AWS credentials. Tests that need a database use `DATABASE_URL` and are
  skipped when it is unset.

## Package Layout

```
src/aqdt/
  observation_store/
    schemas.py     # Source, SiteType, QcFlag, Site, Observation, BoundingBox, IngestSummary (Pydantic)
    frames.py      # SitesFrame, ObservationsFrame (Pandera); *_to_frame builders; validate_partition
    products.py    # Product definition; the observation and site products
    archive.py     # write_partitioned / read_partitioned and the observation/site wrappers
    postgis.py     # apply_schema, load_partitions, rebuild
    sql/           # ordered schema files
  registry.py      # PRODUCTS: every product in the project, in load order
  purpleair/       # purpleair-ingest segment
  airnow/          # airnow-ingest segment
  calibration/     # calibration segment (a later increment)
tests/
  observation_store/
  purpleair/
  airnow/
  calibration/
```

`aqdt` is the import package for the whole project (`src/` layout, declared in `pyproject.toml`).

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| Where site coordinates live | On both `Site` (latest) and every `Observation` (as observed) | Site only; observation only | Sensors get relocated; the observation must record where it was measured, while spatial queries against "the site" want one current point. Denormalization is cheap at this volume. |
| Provenance of raw payload | Full source record stored as `raw` JSON on every observation | Selected extra fields only; separate raw dump keyed by observation | One column satisfies "traceable to raw payload" with no join and no second store; row volume is small (hundreds of sites, hourly-ish). |
| Ingestion timestamp / run id | Not stored on rows | `ingested_at` column; `run_id` column | Any per-run value makes re-runs non-identical and breaks the deterministic-write guarantee. Run bookkeeping belongs to the orchestrator when one exists. |
| Merge policy on re-write | Incoming record wins on `(site_id, observed_at)` | First-seen wins; append with version column | Upstream revisions (AirNow preliminary data) should replace earlier values; versioning every row adds a dimension nothing downstream needs yet. |
| Partition key | `{source}/date={UTC date}` | Hourly partitions; single file per source; partition by site | Daily files stay small enough to rewrite atomically and large enough to avoid file sprawl; source-first lets a run touch only its own tree. |
| Archive location | S3 bucket, addressed by `fsspec` URI | Workspace directory; git-committed data branch; MinIO sidecar | A Codespace and its volumes are deleted after inactivity, so nothing inside it can be the system of record. S3 is the durable, sector-standard home for cloud-native GeoParquet and is read natively by GeoPandas/pyarrow. Git churns binary Parquet on every run; MinIO lives inside the disposable Codespace and only adds an S3 API to local storage. |
| Archive addressing | `fsspec` URI resolved once to a filesystem object | Hard-coded S3 client; separate local and S3 code paths | One code path for `s3://` and local paths keeps tests fast and offline while production writes to S3; pyarrow and GeoPandas accept fsspec filesystems directly. |
| S3 in tests | Local temp directory for most tests; `moto` for the S3-specific path | Real bucket in CI; MinIO service container | No credentials in CI and no network; `moto` runs in-process and exercises the boto3 calls the store actually makes. |
| Test fixtures | Small committed source-payload fixtures under `tests/fixtures/` | Live API calls in tests; recorded HTTP cassettes | Determinism without credentials; fixtures double as documentation of each source's payload shape. |
| Record vs. frame schema | Pydantic record models are canonical; Pandera frame models restate them plus frame-level invariants; a conformance test keeps them aligned | Pydantic only; Pandera only; generate one entirely from the other | Frame invariants (uniqueness, sortedness, geometry) have no record-level source, so full generation is impossible in that direction; generating Pydantic from Pandera loses nested-payload validation and JSON Schema. A test is the cheapest thing that catches drift. |
| Read return type | Validated GeoDataFrame | `list[Observation]`; both via a flag | Every consumer (PostGIS load, calibration, fusion) works on frames; record round-trips on bulk reads are the slow, wrong shape. |
| Duplicate keys within one write batch | Fail validation | Last-in-batch wins | A within-batch duplicate is an ingester defect; silently resolving it hides the bug. Cross-run merging is a different, intended case. |
| Latest observation per site | Computed at query time as `max(observed_at)` over the site's observations | `last_observed_at` column on `Site`, maintained by the ingesters | Sites are merged incoming-wins, so a stored timestamp regresses whenever an older window is backfilled; keeping it right would need a per-column merge rule in the generic writer or a read-before-write in every ingester. At this volume the aggregate is free. |
| Trusted-observation definition | `qc_flags == []` | Boolean `is_valid` column; severity levels per flag | One rule, no second field to keep consistent; downstream stages that want to tolerate specific flags filter on the list. |
| Boundary validation failures | Counted and reported by the ingester; not stored as observations | Store as observations with a flag; store in a `rejected/` partition | A record that cannot be parsed into the schema is not an observation. Flag-never-drop governs observations that exist. Whether to persist rejects for audit is open (see below). |
| PostGIS access | `psycopg` with explicit SQL | GeoPandas `to_postgis` (SQLAlchemy + GeoAlchemy2); an ORM | Upsert semantics need `on conflict`; explicit SQL keeps the dependency set to one driver and the schema in plain files a DBA can read. |
| Schema migrations | Ordered SQL files, idempotent `create if not exists` | Alembic | Two tables and no ORM; a migration framework is more tooling than schema. Revisit when a destructive change is needed. |
| GeoParquet writer | GeoPandas `to_parquet` | PyArrow directly with hand-written GeoParquet metadata | GeoPandas writes standards-compliant GeoParquet metadata and is already the read tool of choice. |
| Write mechanics for derived products | Store exposes `write_partitioned` / `read_partitioned` over a `Product` definition; products own their schemas and tables | Each downstream component implements its own writer; store owns every product's schema | One implementation of determinism, atomicity, and validation is easier to get right and keep right than one per component; owning downstream schemas here would make the store a bottleneck for every later layer's design. |
| Partition keys | Derived by functions on the frame, rendered to strings | Partition columns stored in the frame | The observation key `date` is a function of `observed_at`, and frame models forbid extra columns; deriving keeps the archive schema equal to the record schema. |
| Partition directory style | Hive `key=value` for every key, including `source` | Bare `{source}/` directory | Uniform Hive layout lets pyarrow and DuckDB discover partitions without hints. |
| How PostGIS learns about products | A registry module listing every product in load order | Each component calls `upsert_frame` itself; the store imports every component | A single ordered list gives `rebuild` and `load_partitions` one source of truth and keeps the store from importing its own consumers; a component that loads itself would be a second write path into PostGIS. |
| When PostGIS is refreshed | Each run loads the partitions it wrote, when given a connection | Separate load step; runs always load | Runs return their touched partitions for exactly this; an optional connection keeps tests and offline work archive-only while letting a routine run leave PostGIS current. |
| Import package name | `aqdt`, `src/` layout | `air_quality_digital_twin`; flat layout | Short name for a package imported everywhere; `src/` layout keeps tests running against the installed package, not the working directory. |

## Open Questions & Future Decisions

### Resolved

1. ✅ Datetimes are tz-aware UTC everywhere; naive datetimes fail validation at construction, and
   tz-aware non-UTC datetimes are converted to UTC on construction.
2. ✅ Concurrent writers to one partition are unsupported; serialization is the caller's job.
3. ✅ An observation without coordinates cannot exist (schema requires them); such source records
   are boundary validation failures.

### Deferred

1. Whether to persist boundary-rejected source records to a `rejected/` partition (raw JSON plus
   reason) for audit, or only count and log them. Leaning: persist, since it costs one extra
   writer and preserves provenance for the records the schema cannot hold.
2. Schema versioning of archive files (a `schema_version` entry in Parquet file metadata) so that a
   future `Observation` change can detect and migrate old partitions. Not needed until the first
   incompatible change.
3. Retention or compaction of old partitions once the archive is deep. Not needed at current
   volumes.
4. A source renumbering an existing site (a new native identifier for the same physical location)
   would leave earlier observations under the old `site_id`. No evidence either source does this;
   revisit if it appears.

## References

- `docs/high-level-design.md` — Persistence and QC decisions this component implements.
- GeoParquet specification (geometry column encoding and file metadata).
- PostGIS `geometry(Point, 4326)`, GiST indexing.
- Pydantic v2 — model validation and JSON Schema export.
