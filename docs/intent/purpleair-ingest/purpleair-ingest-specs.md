---
design: purpleair-ingest-design
prefix: PA
---

# PurpleAir Ingest — EARS Specs

Facets: `API` (HTTP client and request contract), `MODEL` (payload boundary model and rejections),
`QC` (flags), `CORR` (EPA corrected value), `MAP` (mapping to canonical records), `RUN` (the
`ingest_purpleair` entry point and summary), `CFG` (settings).

## API Contract

- [ ] **PA-API-001**: When fetching a snapshot, the PurpleAir ingester shall issue `GET https://api.purpleair.com/v1/sensors` with the `X-API-Key` header set to the configured PurpleAir read key.
- [ ] **PA-API-002**: When fetching a snapshot, the PurpleAir ingester shall pass the configured bounding box as the query parameters `nwlng`, `nwlat`, `selng`, `selat` (the store's `BoundingBox` fields, in the same order and orientation).
- [ ] **PA-API-003**: When fetching a snapshot, the PurpleAir ingester shall pass `location_type=0` so that only outdoor sensors are returned, and `max_age=86400` so that sensors silent for more than 24 hours are omitted by the API.
- [ ] **PA-API-004**: When fetching a snapshot, the PurpleAir ingester shall request exactly the fields `sensor_index, name, latitude, longitude, last_seen, humidity, pm2.5_cf_1, pm2.5_cf_1_a, pm2.5_cf_1_b`.
- [ ] **PA-API-005**: The PurpleAir ingester shall apply a 30-second timeout to each `/v1/sensors` request.
- [ ] **PA-API-006**: If a `/v1/sensors` request returns HTTP 429 or any 5xx status, or fails with a connection or timeout error, then the PurpleAir ingester shall retry with exponential backoff up to a total of 3 attempts, and if all attempts fail, fail the run with an error that includes the last status or exception.
- [ ] **PA-API-007**: If a `/v1/sensors` request returns any 4xx status other than 429, then the PurpleAir ingester shall fail the run immediately without retrying.
- [ ] **PA-API-008**: When a `/v1/sensors` response is received, the PurpleAir ingester shall zip each positional row in `data` with the `fields` array into a dict keyed by field name before any further processing, and shall retain the response's `data_time_stamp` as the snapshot timestamp (epoch seconds UTC).
- [ ] **PA-API-009**: The PurpleAir ingester shall use the `/v1/sensors` snapshot endpoint only; it shall not call the per-sensor `/v1/sensors/:id/history` endpoint.

## Payload Model

- [ ] **PA-MODEL-001**: The PurpleAir ingester shall define a Pydantic `PurpleAirSensorRecord` with the fields `sensor_index: int`, `name: str | None`, `latitude: float`, `longitude: float`, `last_seen: datetime`, `humidity: float | None`, `pm2_5_cf_1: float | None` (alias `pm2.5_cf_1`), `pm2_5_cf_1_a: float | None` (alias `pm2.5_cf_1_a`), `pm2_5_cf_1_b: float | None` (alias `pm2.5_cf_1_b`), and shall validate each zipped row against it.
- [ ] **PA-MODEL-002**: When validating a row, the PurpleAir ingester shall parse `last_seen` from epoch seconds into a timezone-aware UTC datetime.
- [ ] **PA-MODEL-003**: If a zipped row lacks `sensor_index`, lacks or has a non-numeric `latitude` or `longitude`, has a `latitude` outside [-90, 90] or a `longitude` outside [-180, 180], or has a missing or unparseable `last_seen`, then the PurpleAir ingester shall treat the row as a boundary rejection: count it in the run summary under a reason naming the failing field, and produce no `Site` or `Observation` from it.
- [ ] **PA-MODEL-004**: When a zipped row has a null `humidity`, `pm2.5_cf_1`, `pm2.5_cf_1_a`, or `pm2.5_cf_1_b`, the PurpleAir ingester shall accept the row at the boundary (nulls in PM and humidity fields are QC conditions, not rejections).

## Quality Control

- [ ] **PA-QC-001**: When building an observation from a validated PurpleAir row, the PurpleAir ingester shall evaluate every QC check in PA-QC-002 through PA-QC-005 and raise every flag whose condition holds; the checks are not mutually exclusive.
- [ ] **PA-QC-002**: When a PurpleAir row's `pm2.5_cf_1` is null, the PurpleAir ingester shall raise `missing_value` (store flag: the primary PM2.5 value is absent).
- [ ] **PA-QC-003**: When exactly one of a PurpleAir row's `pm2.5_cf_1_a`, `pm2.5_cf_1_b` is null, the PurpleAir ingester shall raise `channel_missing`.
- [ ] **PA-QC-004**: When any present value among a PurpleAir row's `pm2.5_cf_1`, `pm2.5_cf_1_a`, `pm2.5_cf_1_b` is less than 0 or greater than 1000 µg/m³, or a present `humidity` is outside [0, 100], or `last_seen` is more than 5 minutes after the response's `data_time_stamp`, the PurpleAir ingester shall raise `out_of_range`.
- [ ] **PA-QC-005**: When both of a PurpleAir row's channels `pm2.5_cf_1_a` (A) and `pm2.5_cf_1_b` (B) are present, `|A − B| > 5` µg/m³, and `|A − B| / mean(A, B) > 0.61`, the PurpleAir ingester shall raise `channel_disagreement`; when only one of the two conditions holds, it shall not.
- [ ] **PA-QC-006**: When both channels of a PurpleAir row are present and `mean(A, B)` is 0, the PurpleAir ingester shall treat the relative-difference condition of PA-QC-005 as not met, so that two channels reading zero are never flagged for disagreement.
- [ ] **PA-QC-007**: The PurpleAir ingester shall never raise `flatline` or `site_id_unresolved`; those flags belong to sources whose QC defines them.

## Corrected Value

- [ ] **PA-CORR-001**: When a PurpleAir row has both channels A and B and `humidity` present, and none of `channel_missing`, `channel_disagreement`, `out_of_range` is raised for it, the PurpleAir ingester shall set `pm25_corrected = 0.524 × mean(A, B) − 0.0862 × humidity + 5.75` (EPA/Barkjohn 2021 nationwide correction), using the mean of the two `cf_1` channels and not the sensor-level `pm2.5_cf_1`.
- [ ] **PA-CORR-002**: When the correction of PA-CORR-001 evaluates to a negative number, the PurpleAir ingester shall clamp `pm25_corrected` to 0.
- [ ] **PA-CORR-003**: When a PurpleAir row lacks either channel or `humidity`, or carries any of `channel_missing`, `channel_disagreement`, `out_of_range`, the PurpleAir ingester shall set `pm25_corrected` to null.
- [ ] **PA-CORR-004**: The PurpleAir ingester shall compute `pm25_corrected` from a row's own values only; it shall not perform the distance-aware calibration against AirNow monitors, which is a downstream component.

## Mapping to Canonical Records

- [ ] **PA-MAP-001**: When building a `Site` from a validated PurpleAir row, the PurpleAir ingester shall set `site_id = "purpleair:{sensor_index}"`, `source = purpleair`, `source_native_id = str(sensor_index)`, `site_type = low_cost_sensor`, `name` from `name`, `latitude`/`longitude` as reported without rounding, and `last_observed_at = last_seen`.
- [ ] **PA-MAP-002**: When building an `Observation` from a validated PurpleAir row, the PurpleAir ingester shall set `site_id`, `source`, `latitude`, `longitude` as for the `Site`, `observed_at = last_seen`, `pm25_raw` from `pm2.5_cf_1` as reported, `pm25_channel_a` from `pm2.5_cf_1_a`, `pm25_channel_b` from `pm2.5_cf_1_b`, `humidity` from `humidity`, `pm25_corrected` per PA-CORR, and `qc_flags` per PA-QC.
- [ ] **PA-MAP-003**: When building an `Observation`, the PurpleAir ingester shall set `raw` to the zipped row dict exactly as produced by PA-API-008, with no keys added, removed, renamed, or values altered.

## Run

- [ ] **PA-RUN-001**: The PurpleAir ingester shall expose `ingest_purpleair(settings, archive_uri) -> IngestSummary` as its entry point, which shall fetch one snapshot, validate rows, apply QC and correction, build records, write sites then observations to the observation store, and return the summary.
- [ ] **PA-RUN-002**: When a run completes, the PurpleAir ingester shall call the store's `write_sites` before `write_observations`, so that no observation is archived for a site that is not.
- [ ] **PA-RUN-003**: When a run completes, the PurpleAir ingester shall return an `IngestSummary` with `source = purpleair`, `fetched` = number of rows in the response `data`, `rejected` = boundary-rejection reason → count, `written` = number of observations handed to the store, `flagged` = flag → number of observations carrying it, `partitions` = the URIs the store reported, `snapshot_at` = the response's `data_time_stamp` as a UTC datetime, and `window_start`/`window_end` unset.
- [ ] **PA-RUN-004**: When the `/v1/sensors` response contains zero rows, the PurpleAir ingester shall complete the run successfully, write nothing, and return a summary with `fetched = 0`, `written = 0`, and empty `partitions`.
- [ ] **PA-RUN-005**: The PurpleAir ingester shall not de-duplicate sensors within one snapshot; if a response contains the same `sensor_index` twice, then the run shall fail at the observation store's within-batch duplicate check (OBS-ARCHIVE-006) and write nothing.
- [ ] **PA-RUN-006**: Each run shall produce at most one `Observation` per sensor, keyed by `(purpleair:{sensor_index}, last_seen)`, so that two runs in which a sensor has not reported again produce the same key and the store's merge leaves the archive unchanged for that sensor.
- [ ] **PA-RUN-007**: Two runs against identical `/v1/sensors` responses shall produce equal `Site` and `Observation` records and, through the store, byte-identical archive partitions.

## Settings

- [ ] **PA-CFG-001**: The PurpleAir ingester shall define `PurpleAirSettings` (Pydantic settings) that reads `PURPLEAIR_API_KEY` and `AQDT_BBOX` (`nwlng,nwlat,selng,selat`, parsed into the store's `BoundingBox`) from the environment, and exposes the endpoint URL, timeout, retry count, `max_age`, `location_type`, and field list as overridable defaults.
- [ ] **PA-CFG-002**: If `PURPLEAIR_API_KEY` or `AQDT_BBOX` is unset when `PurpleAirSettings` is constructed, then the PurpleAir ingester shall fail with an error naming the missing variable.
- [ ] **PA-CFG-003**: The PurpleAir ingester shall read no configuration from a file in the repository.
- [ ] **PA-CFG-004**: PurpleAir ingester tests shall exercise the client against recorded snapshot fixtures under `tests/fixtures/purpleair/` (including at least one channel-B fault) through a mocked HTTP transport, and shall make no network requests.
