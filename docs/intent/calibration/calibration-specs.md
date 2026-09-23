---
design: calibration-design
prefix: CAL
---

# Calibration — EARS Specs

Facets: `IN` (inputs read from the observation store), `HOURLY` (sensor hourly aggregation),
`MATCH` (sensor-to-monitor matching), `FIT` (per-sensor and pooled fits), `APPLY` (calibrated
hourly values), `STORE` (product schemas, archive layout, PostGIS tables), `RUN` (entry points),
`CFG` (settings).

## Inputs

- [x] **CAL-IN-001**: When reading PurpleAir observations for aggregation or fitting, calibration shall use only observations whose `qc_flags` is empty and whose `pm25_corrected` is non-null; a flagged or uncorrected snapshot shall contribute to no `SensorHourly` mean and to no fit.
- [x] **CAL-IN-002**: When reading AirNow observations as reference values, calibration shall use only observations whose `qc_flags` is empty, taking `pm25_raw` as the reference concentration.
- [x] **CAL-IN-003**: Calibration shall read observations and sites from the archive through the observation store's `read_observations` and `read_sites`, and shall not read from PostGIS.
- [x] **CAL-IN-004**: Calibration shall identify sensors as sites with `site_type = low_cost_sensor` and reference monitors as sites with `site_type = reference_monitor`, using the coordinates on the `Site` record for matching.

## Hourly Aggregation

- [x] **CAL-HOURLY-001**: When aggregating a sensor's observations, calibration shall group its trusted, corrected snapshots (CAL-IN-001) by the UTC hour `H` such that `H <= observed_at < H + 1 h`, and produce one `SensorHourly` row per `(site_id, H)` with `pm25_corrected_mean` and `humidity_mean` as the arithmetic means over those snapshots.
- [x] **CAL-HOURLY-002**: When aggregating a sensor's observations, calibration shall set `n_snapshots` to the number of trusted, corrected snapshots in the hour and `n_flagged` to the number of that sensor's snapshots in the hour that carry at least one QC flag; a snapshot excluded only for a null `pm25_corrected` is counted in neither.
- [x] **CAL-HOURLY-003**: When a sensor has no trusted, corrected snapshot in an hour, calibration shall produce no `SensorHourly` row for that `(site_id, hour)`.
- [x] **CAL-HOURLY-004**: Calibration shall impose no minimum `n_snapshots` on a `SensorHourly` row; an hour with a single trusted snapshot is aggregated like any other.
- [x] **CAL-HOURLY-005**: When pairing a sensor hour with a reference monitor, calibration shall pair `SensorHourly` hour `H` with the monitor's observation whose `observed_at == H`, with no offset.

## Matching

- [x] **CAL-MATCH-001**: When fitting for an `as_of`, calibration shall match every `low_cost_sensor` site the archive's sites hold (`read_sites`), whether or not it has `SensorHourly` rows in the fitting window.
- [x] **CAL-MATCH-002**: When matching a sensor, calibration shall consider only `reference_monitor` sites that have at least one trusted observation (CAL-IN-002) with `observed_at` in the fitting window `[as_of − window_days, as_of)`.
- [x] **CAL-MATCH-003**: When matching a sensor, calibration shall select the eligible monitor with the smallest distance to the sensor, computed in metres after projecting both points to EPSG:26918, and record it as `ref_site_id` with that distance as `distance_m`.
- [x] **CAL-MATCH-004**: If no eligible monitor lies within `max_distance_m` (default 10 000) of a sensor, then calibration shall match the sensor to nothing (`ref_site_id` and `distance_m` null) and route it to the pooled fit.
- [x] **CAL-MATCH-005**: Calibration shall compute distances in Python (GeoPandas) and shall not require a PostGIS connection to match, fit, or apply.

## Fitting

- [x] **CAL-FIT-001**: When fitting a matched sensor for `as_of`, calibration shall take as pairs every hour `H` in `[as_of − window_days, as_of)` for which both the sensor's `SensorHourly` row and the matched monitor's trusted observation at `H` exist, and shall set `n_pairs` to their count.
- [x] **CAL-FIT-002**: When fitting a sensor with pairs, calibration shall compute ordinary least squares `ref_pm25 = intercept + slope × pm25_corrected_mean` over the pairs, with the monitor's `pm25_raw` as `ref_pm25`.
- [x] **CAL-FIT-003**: Calibration shall accept a per-sensor fit if and only if `n_pairs >= min_pairs` (default 72), the sensor's `pm25_corrected_mean` is not constant across the pairs, and `slope > 0`; an accepted fit shall be recorded with `status = fitted`.
- [x] **CAL-FIT-004**: When at least one per-sensor fit is accepted for an `as_of`, calibration shall compute the pooled fit as the same ordinary least squares over the union of all accepted sensors' pairs, and shall accept it if and only if its `slope > 0`.
- [x] **CAL-FIT-005**: When a sensor's per-sensor fit is not accepted (including a sensor with no match or no pairs) and the pooled fit is accepted, calibration shall record the pooled fit's `slope`, `intercept`, `r2`, `rmse` for that sensor with `status = pooled`, `ref_site_id` and `distance_m` null, and `n_pairs` equal to the pooled pair count.
- [x] **CAL-FIT-006**: If no per-sensor fit is accepted for an `as_of`, or the pooled fit fails its gate, then calibration shall record every sensor whose per-sensor fit was not accepted with `status = uncalibrated` and null `slope`, `intercept`, `r2`, `rmse`.
- [x] **CAL-FIT-007**: When recording any fit, calibration shall set `r2` to the coefficient of determination and `rmse` to the root-mean-square residual in µg/m³, both computed over the same pairs the fit was computed on.
- [x] **CAL-FIT-008**: When recording any fit, calibration shall set `window_start = as_of − window_days` and `window_end = as_of` (half-open window).
- [x] **CAL-FIT-009**: Calibration shall not gate acceptance on `r2` or `rmse`; they are recorded as diagnostics only.
- [x] **CAL-FIT-010**: When fitting, calibration shall produce exactly one `CalibrationFit` row per `(site_id, as_of)` for every `low_cost_sensor` site (CAL-MATCH-001), with `status` one of `fitted`, `pooled`, `uncalibrated`.

## Applying

- [x] **CAL-APPLY-001**: When applying calibrations over `[start, end)`, calibration shall produce a `CalibratedHourly` row for every `SensorHourly` row with `start <= hour < end`, carrying `site_id`, `hour`, `pm25_corrected_mean`, and `n_snapshots` from it.
- [x] **CAL-APPLY-002**: When applying to a sensor hour `H`, calibration shall select the sensor's `CalibrationFit` with the greatest `as_of` such that `as_of <= H`, with no limit on how far before `H` that `as_of` is, and record it as `fit_as_of` and its status as `fit_status`.
- [x] **CAL-APPLY-003**: When the selected fit has `status` `fitted` or `pooled`, calibration shall set `pm25_calibrated = max(0, intercept + slope × pm25_corrected_mean)`.
- [x] **CAL-APPLY-004**: If the selected fit has `status = uncalibrated`, or no fit with `as_of <= H` exists for the sensor, then calibration shall set `pm25_calibrated` to null and `fit_status = uncalibrated` (with `fit_as_of` null when no fit exists).
- [x] **CAL-APPLY-005**: Calibration shall not produce calibrated values for `reference_monitor` sites; their `pm25_raw` is consumed directly by downstream layers.

## Storage

- [x] **CAL-STORE-001**: Calibration shall define Pydantic records `SensorHourly` (`site_id`, `hour`, `pm25_corrected_mean`, `humidity_mean`, `n_snapshots`, `n_flagged`), `CalibrationFit` (`site_id`, `as_of`, `status`, `ref_site_id`, `distance_m`, `window_start`, `window_end`, `n_pairs`, `slope`, `intercept`, `r2`, `rmse`), and `CalibratedHourly` (`site_id`, `hour`, `pm25_calibrated`, `pm25_corrected_mean`, `n_snapshots`, `fit_as_of`, `fit_status`) under `aqdt.calibration.schemas`, with every datetime field timezone-aware UTC.
- [x] **CAL-STORE-002**: Calibration shall define Pandera frame models for each of the three products under `aqdt.calibration.frames`, and the calibration test suite shall include the same record-to-frame conformance test the observation store applies to its schemas (each record field present in the frame with matching nullability and compatible dtype, no extra data columns).
- [x] **CAL-STORE-003**: When validating a `SensorHourly` or `CalibratedHourly` frame, calibration shall reject a frame in which `(site_id, hour)` is not unique; when validating a `CalibrationFit` frame, it shall reject a frame in which `(site_id, as_of)` is not unique.
- [x] **CAL-STORE-004**: Calibration shall define three observation-store `Product`s and write through `write_partitioned`: `sensor_hourly` (prefix `calibration/sensor_hourly`, partition key `date` = UTC date of `hour`, key `(site_id, hour)`, file `sensor_hourly.parquet`); `fits` (prefix `calibration/fits`, partition key `as_of` rendered `YYYY-MM-DDTHH`, key `(site_id, as_of)`, file `fits.parquet`); and `calibrated_hourly` (prefix `calibration/calibrated_hourly`, partition key `date` = UTC date of `hour`, key `(site_id, hour)`, file `calibrated_hourly.parquet`).
- [x] **CAL-STORE-005**: Calibration shall read its own products through the observation store's `read_partitioned` with the products of CAL-STORE-004, so that `apply_calibrations` finds fits by reading the `fits` product filtered to `as_of` at or before the hours being applied.
- [x] **CAL-STORE-006**: Calibration shall keep ordered SQL files under `src/aqdt/calibration/sql/` defining the tables `sensor_hourly` (primary key `(site_id, hour)`), `calibration_fits` (primary key `(site_id, as_of)`), and `calibrated_hourly` (primary key `(site_id, hour)`), each with `site_id references sites(site_id)` and columns matching the corresponding record models.
- [x] **CAL-STORE-007**: Calibration shall register its three products in `aqdt.registry.PRODUCTS` after `sites` and `observations`, so that the observation store's `apply_schema` creates their tables, `load_partitions` loads them, and `rebuild` reloads them; calibration shall not write to PostGIS by any other path.

## Runs

- [x] **CAL-RUN-001**: Calibration shall expose `fit_calibrations(archive_uri, as_of, settings, conn=None) -> list[CalibrationFit]`, which shall aggregate sensor hours over `[as_of − window_days, as_of)`, write the `SensorHourly` rows, match, fit, write the fits, — when `conn` is given — call the store's `load_partitions` with every partition it wrote, and return the fits.
- [x] **CAL-RUN-002**: Calibration shall expose `apply_calibrations(archive_uri, start, end, settings, conn=None) -> frame`, which shall aggregate sensor hours over `[start, end)`, write the `SensorHourly` rows, apply the applicable fits, write the `CalibratedHourly` rows, — when `conn` is given — call the store's `load_partitions` with every partition it wrote, and return the calibrated rows as a validated frame.
- [x] **CAL-RUN-003**: `fit_calibrations` and `apply_calibrations` shall require `as_of`, `start`, and `end` to be timezone-aware datetimes, shall convert each to UTC and truncate it to the hour before use, and shall fail with a validation error if any is naive or if `end <= start` after truncation.
- [x] **CAL-RUN-004**: Two `fit_calibrations` runs with the same `as_of` and settings against an unchanged archive shall produce equal `CalibrationFit` rows and byte-identical fit partitions; two `apply_calibrations` runs over the same window against an unchanged archive shall likewise produce identical `CalibratedHourly` partitions.
- [x] **CAL-RUN-005**: When `fit_calibrations` is re-run for an `as_of` after the archive has changed, calibration shall replace the previously written fits for that `as_of` (through the store's incoming-wins merge) rather than keeping both.
- [x] **CAL-RUN-006**: When the fitting window contains no trusted, corrected sensor observation at all, `fit_calibrations` shall complete successfully with every sensor `uncalibrated` and shall write fit rows saying so.

## Settings

- [x] **CAL-CFG-001**: Calibration shall define `CalibrationSettings` (Pydantic settings) with `max_distance_m` (default 10 000, from `AQDT_CAL_MAX_DISTANCE_M`), `window_days` (default 30, from `AQDT_CAL_WINDOW_DAYS`), and `min_pairs` (default 72, from `AQDT_CAL_MIN_PAIRS`), each optional in the environment.
- [x] **CAL-CFG-002**: If any calibration setting is set to a non-positive value, then calibration shall fail with a validation error naming the setting.
- [x] **CAL-CFG-003**: Calibration tests shall run against a temporary local archive populated from small synthetic observation frames, and shall require neither a network connection nor a PostGIS connection except for tests of CAL-STORE-006 and CAL-STORE-007, which shall be skipped while `DATABASE_URL` is unset.
