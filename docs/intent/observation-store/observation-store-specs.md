---
design: observation-store-design
prefix: OBS
---

# Observation Store — EARS Specs

Facets: `SCHEMA` (record models and enumerations), `FLAG` (QC flag vocabulary), `FRAME` (Pandera
frame models), `ARCHIVE` (GeoParquet archive layout, writes, reads, and the partition primitives),
`PG` (PostGIS schema and load), `ENV` (configuration, devcontainer, CI).

## Canonical Schemas

- [ ] **OBS-SCHEMA-001**: The observation store shall define the `Source` enumeration with exactly the members `purpleair` and `airnow`, and the `SiteType` enumeration with exactly the members `low_cost_sensor` and `reference_monitor`.
- [ ] **OBS-SCHEMA-002**: The observation store shall define a Pydantic `Site` record with the fields `site_id: str`, `source: Source`, `source_native_id: str`, `site_type: SiteType`, `name: str | None`, `latitude: float`, `longitude: float`, with no other data fields.
- [ ] **OBS-SCHEMA-003**: The observation store shall define a Pydantic `Observation` record with the fields `site_id: str`, `source: Source`, `observed_at: datetime`, `latitude: float`, `longitude: float`, `pm25_raw: float | None`, `pm25_channel_a: float | None`, `pm25_channel_b: float | None`, `humidity: float | None`, `pm25_corrected: float | None`, `qc_flags: list[QcFlag]`, `raw: dict[str, Any]`, with no other data fields.
- [ ] **OBS-SCHEMA-004**: If a `Site` or `Observation` record is constructed with a `latitude` outside [-90, 90] or a `longitude` outside [-180, 180], then the observation store shall reject the record with a validation error.
- [ ] **OBS-SCHEMA-005**: If an `Observation` record is constructed with a naive (timezone-less) `observed_at`, then the observation store shall reject the record with a validation error; when `observed_at` is timezone-aware but not UTC, the observation store shall convert it to UTC on construction.
- [ ] **OBS-SCHEMA-006**: The observation store shall store `Site.latitude`, `Site.longitude`, `Observation.latitude`, and `Observation.longitude` at the precision the ingester supplied, without rounding or truncation.
- [ ] **OBS-SCHEMA-007**: When an `Observation` record is constructed, the observation store shall normalize `qc_flags` to a list with no duplicate members, sorted alphabetically by the flag's serialized name.
- [ ] **OBS-SCHEMA-008**: The observation store shall expose `Observation.is_trusted`, which shall be true if and only if `qc_flags` is empty.
- [ ] **OBS-SCHEMA-009**: The `Site` and `Observation` records shall carry no ingestion timestamp, run identifier, or host detail, so that two records built from identical source data are equal regardless of when or where they were built.
- [ ] **OBS-SCHEMA-010**: The observation store shall define a Pydantic `IngestSummary` record with the fields `source: Source`, `fetched: int`, `rejected: dict[str, int]`, `written: int`, `flagged: dict[QcFlag, int]`, `partitions: list[str]`, `snapshot_at: datetime | None`, `window_start: datetime | None`, `window_end: datetime | None`, where `written` counts every observation handed to the store (including hours an ingester re-emits for lookback) and `window_start`/`window_end` describe a half-open window.
- [ ] **OBS-SCHEMA-011**: The observation store shall define a Pydantic `BoundingBox` record with the fields `nwlng`, `nwlat`, `selng`, `selat` (WGS84 decimal degrees) that parses from the string form `nwlng,nwlat,selng,selat`.
- [ ] **OBS-SCHEMA-012**: If a `BoundingBox` is constructed with any coordinate out of range, with `nwlat <= selat`, or with `nwlng >= selng`, then the observation store shall reject it with a validation error.
- [ ] **OBS-SCHEMA-013**: The observation store shall expose `Observation.from_frame(frame)`, which shall return the `Observation` records for every row of a validated `ObservationsFrame`.
- [ ] **OBS-SCHEMA-014**: Every `IngestSummary` an ingester returns shall satisfy `fetched == written + sum(rejected.values())`, so that every source row is accounted for as either an observation handed to the store or a counted boundary rejection.
- [ ] **OBS-SCHEMA-015**: Every time window the observation store accepts or reports (`read_observations`, `IngestSummary.window_start`/`window_end`, and downstream windows) shall be half-open: `start` is included and `end` is excluded.

## QC Flag Vocabulary

- [ ] **OBS-FLAG-001**: The observation store shall define the `QcFlag` enumeration with exactly the members `missing_value`, `out_of_range`, `channel_missing`, `channel_disagreement`, `flatline`, and `site_id_unresolved`; ingesters shall raise no flag outside this enumeration.
- [ ] **OBS-FLAG-002**: The `QcFlag` enumeration shall serialize each member as its lowercase snake_case name, so that `qc_flags` values in the archive and in PostGIS are readable without project code.
- [ ] **OBS-FLAG-003**: The observation store shall not define a staleness flag nor store a per-site latest-observation timestamp; a site's latest observation and its age shall be computed at query time as `max(observed_at)` over its observations by the consumer.

## Frame Models

- [ ] **OBS-FRAME-001**: The observation store shall define Pandera `DataFrameModel`s `SitesFrame` and `ObservationsFrame` over GeoDataFrames whose data columns are exactly the fields of `Site` and `Observation` respectively, plus a `geometry` column.
- [ ] **OBS-FRAME-002**: The observation store's test suite shall include a conformance test asserting that every field of `Site` and `Observation` appears in `SitesFrame` and `ObservationsFrame` respectively with matching nullability and a compatible dtype, and that neither frame model adds a data column beyond `geometry`.
- [ ] **OBS-FRAME-003**: When validating an `ObservationsFrame`, the observation store shall reject a frame in which any `(site_id, observed_at)` pair occurs more than once.
- [ ] **OBS-FRAME-004**: When validating a `SitesFrame`, the observation store shall reject a frame in which any `site_id` occurs more than once.
- [ ] **OBS-FRAME-005**: When validating a frame read from or about to be written to an archive partition, the observation store shall reject an `ObservationsFrame` not sorted by `(site_id, observed_at)` and a `SitesFrame` not sorted by `site_id`; frames handed to a downstream stage shall not be subject to the sortedness check.
- [ ] **OBS-FRAME-006**: When validating either frame model, the observation store shall reject a frame whose `geometry` column is not `Point` geometry in EPSG:4326, or in which any row's geometry does not equal `Point(longitude, latitude)`.
- [ ] **OBS-FRAME-007**: When validating an `ObservationsFrame`, the observation store shall reject a frame whose `observed_at` column is not timezone-aware UTC.
- [ ] **OBS-FRAME-008**: When validating an `ObservationsFrame`, the observation store shall reject a frame in which any `qc_flags` element is not a member of `QcFlag`.
- [ ] **OBS-FRAME-009**: When validating an `ObservationsFrame`, the observation store shall reject any row in which exactly one of `pm25_channel_a`, `pm25_channel_b` is null and `channel_missing` is not in that row's `qc_flags`.
- [ ] **OBS-FRAME-010**: When validating either frame model, the observation store shall reject a frame with any `latitude` outside [-90, 90] or `longitude` outside [-180, 180].
- [ ] **OBS-FRAME-011**: If a frame fails validation, then the observation store shall raise an error naming the failing check and the offending rows, and shall neither write the frame to the archive nor return it from a read.

## GeoParquet Archive

- [ ] **OBS-ARCHIVE-001**: The observation store shall lay out the archive as `{archive_uri}/source={source}/sites.parquet` for sites and `{archive_uri}/source={source}/date={YYYY-MM-DD}/observations.parquet` for observations, where `date` is the UTC calendar date of `observed_at`; every partition directory shall be Hive-style `key=value`.
- [ ] **OBS-ARCHIVE-002**: The observation store shall accept `archive_uri` as any `fsspec` URI (`s3://bucket/prefix`, `file:///path`, or a bare local path), resolve it to a filesystem object once, and perform every archive read and write through that object so that the code path is identical for S3 and local storage.
- [ ] **OBS-ARCHIVE-003**: The observation store shall write observation and site files as GeoParquet with a `geometry` column of WGS84 (EPSG:4326) points derived from `longitude`/`latitude`, every schema field as a plain column, `qc_flags` as a list of strings, and `raw` as a JSON string column; a product whose frame has no `geometry` column shall be written as plain Parquet.
- [ ] **OBS-ARCHIVE-004**: When serializing `raw` for the archive, the observation store shall encode the dict as JSON with sorted keys and default float formatting, so that identical source records produce identical bytes.
- [ ] **OBS-ARCHIVE-005**: When `write_observations(records, archive_uri)` is called, the observation store shall build an `ObservationsFrame` from the records and validate it (all checks except sortedness) before touching any archive file.
- [ ] **OBS-ARCHIVE-006**: If the records passed to a single `write_observations` call contain more than one record with the same `(site_id, observed_at)`, then the observation store shall fail the call with a validation error and write nothing.
- [ ] **OBS-ARCHIVE-007**: When `write_observations` is called with a validated frame, the observation store shall group the rows by `(source, UTC date of observed_at)` and write each group to its own partition object.
- [ ] **OBS-ARCHIVE-008**: When writing an observations partition that already exists, the observation store shall read the existing partition, merge the incoming rows on `(site_id, observed_at)` with the incoming row replacing any stored row with the same key, sort by `(site_id, observed_at)`, validate the merged frame, and write the whole merged partition as one object.
- [ ] **OBS-ARCHIVE-009**: When writing a partition object, the observation store shall make the write atomic: on a local filesystem by writing a temporary file and renaming it over the target, and on S3 by a single PUT; a crash mid-write shall leave the previous partition contents intact and never expose a partial object.
- [ ] **OBS-ARCHIVE-010**: When `write_observations` or `write_sites` completes, the observation store shall return the list of archive partition URIs it wrote.
- [ ] **OBS-ARCHIVE-011**: When `write_observations` or `write_sites` is called with an empty record set, the observation store shall write nothing, create no files, and return an empty list.
- [ ] **OBS-ARCHIVE-012**: Two `write_observations` calls with equal record sets against archives in the same prior state shall produce byte-identical partition objects.
- [ ] **OBS-ARCHIVE-013**: When `write_sites(records, archive_uri)` is called, the observation store shall build and validate a `SitesFrame` (all checks except sortedness), merge it on `site_id` into the per-source `sites.parquet` with the incoming row replacing any stored row with the same key, sort by `site_id`, validate, and write the whole file as one atomic object.
- [ ] **OBS-ARCHIVE-014**: The observation store shall define `Product(prefix, partition_keys, key_cols, frame_model, filename, table, sql_dir)`, where `partition_keys` is an ordered mapping of key name to a function of the frame returning each row's partition value (rendered to a path segment per OBS-ARCHIVE-022), and shall expose `write_partitioned(frame, archive_uri, product)`, which shall validate `frame` against `product.frame_model` (all checks except sortedness), render the partition keys, group rows by them, perform the read-merge-write of OBS-ARCHIVE-008 and OBS-ARCHIVE-009 on `product.key_cols` for each group at `{archive_uri}/{prefix}/{k1}={v1}/.../{filename}`, and return the partition URIs written.
- [ ] **OBS-ARCHIVE-022**: When rendering a partition key, the observation store shall render a date as `YYYY-MM-DD`, a datetime as `YYYY-MM-DDTHH` (UTC), and a string as itself, so that the same key value always yields the same path.
- [ ] **OBS-ARCHIVE-015**: The observation store shall expose `read_partitioned(archive_uri, product, **filters)`, where each filter is a literal (matched by equality against the rendered key) or a predicate on the rendered key string, which shall read every partition under `{archive_uri}/{product.prefix}/` whose keys satisfy every filter and return a single frame validated against `product.frame_model`.
- [ ] **OBS-ARCHIVE-016**: `write_observations`, `write_sites`, `read_observations`, and `read_sites` shall be implemented as calls to `write_partitioned` and `read_partitioned` with the observation and site `Product` definitions, so that the determinism, atomicity, and validation guarantees have one implementation.
- [ ] **OBS-ARCHIVE-017**: When `read_observations(archive_uri, source=None, start=None, end=None)` is called, the observation store shall read every observations partition whose source matches `source` (all sources when `None`) and whose date intersects the half-open window `[start, end)` (unbounded when `None`), filter rows to `start <= observed_at < end`, and return a validated `ObservationsFrame` GeoDataFrame.
- [ ] **OBS-ARCHIVE-018**: When `read_sites(archive_uri, source=None)` is called, the observation store shall return a validated `SitesFrame` GeoDataFrame of every site whose source matches `source` (all sources when `None`).
- [ ] **OBS-ARCHIVE-019**: When a read matches no partition, the observation store shall log an INFO message naming the archive URI, source, and window requested, and return an empty frame with the model's columns and dtypes rather than raising.
- [ ] **OBS-ARCHIVE-020**: Every archive file the observation store writes shall be readable as a standard GeoParquet file by GeoPandas (`read_parquet`) without importing project code, yielding the same rows and a `geometry` column in EPSG:4326.
- [D] **OBS-ARCHIVE-021**: The observation store shall record a `schema_version` entry in each archive file's Parquet metadata so that a later incompatible `Observation` change can detect and migrate older partitions.

## PostGIS Serving Layer

- [ ] **OBS-PG-001**: The observation store shall keep its PostGIS schema as ordered SQL files under `src/aqdt/observation_store/sql/`, defining a `sites` table (`site_id text primary key`, `source`, `source_native_id`, `site_type`, `name`, `latitude`, `longitude`, `geom geometry(Point, 4326)`) and an `observations` table (`site_id references sites`, `source`, `observed_at timestamptz`, `latitude`, `longitude`, `pm25_raw`, `pm25_channel_a`, `pm25_channel_b`, `humidity`, `pm25_corrected`, `qc_flags text[]`, `raw jsonb`, `geom geometry(Point, 4326)`, primary key `(site_id, observed_at)`), with nullability matching the `Site` and `Observation` records.
- [ ] **OBS-PG-002**: The PostGIS schema shall include a GiST index on `sites.geom`, a GiST index on `observations.geom`, a B-tree index on `observations.observed_at`, and a B-tree index on `observations (source, observed_at)`.
- [ ] **OBS-PG-003**: When `apply_schema(conn)` is called, the observation store shall apply the SQL files of every product in `aqdt.registry.PRODUCTS`, in registry order, using `create ... if not exists` statements, so that calling it against an already-initialized database succeeds and changes nothing.
- [ ] **OBS-PG-012**: The project shall define `aqdt.registry.PRODUCTS`, an ordered list of every archive product (`sites`, `observations`, then each downstream component's products), and the observation store shall consult only this list to learn which tables to create, which partitions to load, and in what order.
- [ ] **OBS-PG-004**: When `load_partitions(conn, archive_uri, partition_uris)` is called, the observation store shall identify each partition's product from its path, read it, and upsert it into the product's table using `insert ... on conflict do update` on the product's `key_cols`, one transaction per partition, processing partitions in registry order so that sites are loaded before any table that references them.
- [ ] **OBS-PG-005**: When `rebuild(conn, archive_uri)` is called, the observation store shall truncate every registered product's table, then load every partition of every registered product from the archive in registry order, so that the database afterwards equals the archive's contents.
- [ ] **OBS-PG-006**: The observation store's upsert shall be internal to `load_partitions` and `rebuild`; no other module in the project shall write to PostGIS.
- [ ] **OBS-PG-007**: When upserting a `SitesFrame` or `ObservationsFrame` into PostGIS, the observation store shall populate `geom` from the frame's `geometry` column as EPSG:4326 points.
- [ ] **OBS-PG-008**: Two consecutive `load_partitions` calls over the same partitions with an unchanged archive shall leave the row count and row contents of every loaded table unchanged after the second call.
- [ ] **OBS-PG-009**: The observation store shall write to PostGIS only rows read from the archive; no code path shall insert a row into any registered table that is not first in the archive.
- [ ] **OBS-PG-010**: The observation store shall access PostGIS through `psycopg` with explicit SQL and shall not depend on an ORM or on GeoPandas' `to_postgis`.
- [ ] **OBS-PG-011**: The observation store shall open its PostGIS connection using the `DATABASE_URL` environment variable.

## Environment

- [ ] **OBS-ENV-001**: The observation store shall read its configuration exclusively from the environment variables `AQDT_ARCHIVE_URI` (archive root) and `DATABASE_URL` (PostGIS connection), with S3 credentials taken from the standard `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_DEFAULT_REGION` variables; no configuration shall be read from a file in the repository.
- [ ] **OBS-ENV-002**: If `AQDT_ARCHIVE_URI` is unset when an archive operation is requested without an explicit `archive_uri`, then the observation store shall fail with an error naming the missing variable.
- [ ] **OBS-ENV-003**: The devcontainer shall be defined by a `docker-compose.yml` with the application container and a `postgis/postgis` service, referenced from `devcontainer.json` via `dockerComposeFile`, with `DATABASE_URL` defaulting to that service.
- [ ] **OBS-ENV-004**: The devcontainer `postCreateCommand` shall run `uv sync --all-groups`, `apply_schema`, and `rebuild`, so that a new Codespace reaches a populated, queryable PostGIS with no manual step.
- [ ] **OBS-ENV-005**: Observation store tests that exercise the S3 code path shall run against an in-process S3 mock (`moto`) and all other archive tests against a temporary local directory, so that the test suite needs no AWS credentials and no network.
- [ ] **OBS-ENV-006**: While `DATABASE_URL` is unset, tests that require a PostGIS connection shall be skipped rather than fail.
- [ ] **OBS-ENV-007**: CI shall provide a `postgis/postgis` service container and set `DATABASE_URL` so that the PostGIS tests run on every push and pull request.
- [ ] **OBS-ENV-008**: The project shall be installed as the import package `aqdt` from a `src/` layout declared in `pyproject.toml`, so that tests run against the installed package.
