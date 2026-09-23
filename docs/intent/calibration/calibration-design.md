---
parent: high-level-design
prefix: CAL
---

# Calibration

## Context and Design Philosophy

PurpleAir readings, even after EPA's nationwide humidity correction, carry sensor-specific bias
and gain: two sensors a block apart can disagree by a stable factor for months. Calibration
removes that per-sensor error by fitting each sensor against the regulatory monitor it can be
compared with, so that the fused surface is built from values on the reference monitors' scale.

The D.C. metro has several reference monitors spread over a large area, so which monitor a sensor
is compared with matters. Each sensor is matched to a reference by distance, the distance is
recorded with the fit, and a sensor with no reference close enough is not force-fit against a
far-away one; it falls back to a pooled network-wide fit and says so. Every calibrated value can
be traced to the fit that produced it, and every fit to the pairs it was fitted on.

Calibration is a batch step in the same idempotent, time-windowed style as ingestion: given the
archive as of an hour, it produces the same fits and the same calibrated values.

## Inputs

All inputs come from the observation store.

- **PurpleAir observations** — trusted only (`qc_flags == []`), with `pm25_corrected` non-null.
  A flagged or uncorrected snapshot contributes nothing to fitting or to the hourly aggregate.
- **AirNow observations** — trusted only, `pm25_raw` as the reference concentration.
- **Sites** — coordinates and `site_type` for matching.

## Hourly Aggregation

AirNow is hourly; PurpleAir observations are snapshots at arbitrary instants. The comparable unit
is the hour. Each PurpleAir sensor's trusted snapshots with `observed_at` in `[H, H + 1 h)` are
aggregated to one row for hour `H`:

`SensorHourly`: `site_id`, `hour`, `pm25_corrected_mean`, `humidity_mean`, `n_snapshots`,
`n_flagged` (snapshots in the hour that were excluded for flags).

An hour with no trusted snapshot has no row — a row with a null mean would poison every
downstream average — so `n_flagged` is only visible for hours that also had a trusted reading. An
hour lost entirely to flags is evaluation's concern, not this component's. `n_snapshots` is
carried so consumers can weight or filter by how well the hour was sampled; the aggregation
itself imposes no minimum, because snapshot density is a property of how often ingestion ran, not
of the sensor.

AirNow rows are already hourly; `observed_at` is the start of the hour the value describes
(AirNow labels each hourly value with the beginning of its averaging period), so a sensor's
aggregate for hour `H` and the monitor's observation at `H` cover the same hour.

## Matching

Every `low_cost_sensor` site in the archive's sites is matched, whether or not it reported
in the window, so every sensor has a fit row for every `as_of`. For each, the reference monitor
is the nearest `reference_monitor` site by distance in metres within `max_distance_m` (default
10 000 m) **among monitors with at least one trusted observation in the fitting window** — a
monitor with no comparable data in the window is not a comparison, however close it is. The match
is recorded on the fit as `ref_site_id` and `distance_m`. A site with no eligible reference within
range is matched to nothing and receives the pooled fit.

Distances are computed in Python (GeoPandas, projected to EPSG:26918 / UTM zone 18N, which covers
the D.C. metro with negligible distortion) so the step is a pure function of the archive and runs
in tests without PostGIS.

## Fitting

For each matched sensor, the pairs are the hours in the fitting window where both the sensor's
`SensorHourly` row and the reference monitor's trusted observation exist. The fit is ordinary
least squares:

```
ref_pm25 = intercept + slope × pm25_corrected_mean
```

over the half-open window `[as_of − window_days, as_of)` (`window_days` default 30) — the
project's windows are always half-open.

A per-sensor fit is **accepted** when `n_pairs ≥ min_pairs` (default 72, three days of hourly
pairs), the sensor's `pm25_corrected_mean` is not constant across the pairs (OLS is undefined on
a constant input), and `slope > 0`. Otherwise the sensor receives the **pooled fit**: the same
OLS over the union of all accepted sensors' pairs, held to the same `slope > 0` gate. If no
sensor's fit is accepted, or the pooled fit fails its gate, every non-accepted sensor is
`uncalibrated` for that `as_of`.

`r2` is the coefficient of determination and `rmse` the root-mean-square residual in µg/m³, both
of the fit over its own pairs — the `n_pairs` pairs for a per-sensor fit, the pooled pairs for
the pooled fit.

`CalibrationFit`: `site_id`, `as_of`, `status` (`fitted` | `pooled` | `uncalibrated`),
`ref_site_id`, `distance_m`, `window_start`, `window_end`, `n_pairs`, `slope`, `intercept`,
`r2`, `rmse`. For a pooled fit, `ref_site_id` and `distance_m` are null and `n_pairs` is the pooled
count. Diagnostics are recorded, not gated on: a low `r2` in a low-variance week is not evidence of
a bad sensor.

## Applying

`CalibratedHourly`: `site_id`, `hour`, `pm25_calibrated`, `pm25_corrected_mean`, `n_snapshots`,
`fit_as_of`, `fit_status`. `pm25_calibrated = intercept + slope × pm25_corrected_mean`, clamped at
0, using the most recent fit for the site with `as_of ≤ hour`; null with `fit_status =
uncalibrated` when no such fit exists (`fit_as_of` is then null too, distinguishing "never
fitted" from "fitted and found uncalibrated"). There is no cap on how old that fit may be: a gap in the
fit archive is served by the last fit before it, and `fit_as_of` on the row is how a consumer
sees that.

Reference monitors are not calibrated; the fusion layer takes their `pm25_raw` directly.

## Runs

- `fit_calibrations(archive_uri, as_of, settings, conn=None) -> list[CalibrationFit]` —
  aggregate the fitting window and write its `SensorHourly` rows, match, fit, write fits.
- `apply_calibrations(archive_uri, start, end, settings, conn=None) -> frame` — aggregate the
  half-open window `[start, end)` and write its `SensorHourly` rows, apply the applicable fits,
  write `CalibratedHourly`.

Both runs write the `SensorHourly` rows they aggregate, so the product is complete for every
window either run has touched. Given a PostGIS connection, each run ends by loading the
partitions it wrote through the store's `load_partitions`; without one it touches the archive
only. `as_of`, `start`, and `end` must be tz-aware; each is converted to UTC and truncated to
the hour before use, and a naive value or `end ≤ start` fails validation. A fitting window with no trusted,
corrected sensor observation is a successful run that writes every sensor as `uncalibrated` —
the absence of a fit is itself a recorded fact.

Fits are refit once per day (`as_of` at 00:00 UTC) in normal operation; any `as_of` hour is valid
for backfill or experiment. Both runs are idempotent for a given archive state. The archive
itself follows upstream revisions (AirNow re-issues preliminary hours), so re-running
`fit_calibrations` for a past `as_of` after a revision produces a different fit and replaces the
earlier one; `fit_as_of` identifies which refit an hour was calibrated with, not a frozen set of
coefficients. The fit is a function of the archive, and the archive is the system of record.

Settings (`CalibrationSettings`, Pydantic settings from the environment): `AQDT_CAL_MAX_DISTANCE_M`
(default 10 000), `AQDT_CAL_WINDOW_DAYS` (default 30), `AQDT_CAL_MIN_PAIRS` (default 72). All are
optional; unlike the ingesters' API keys, nothing here is required to be set. A non-positive
value for any of them fails validation naming the setting.

## Storage

Products are written to the archive under `calibration/` and loaded into PostGIS beside the
observation tables:

```
{archive_uri}/calibration/
  sensor_hourly/date={YYYY-MM-DD}/sensor_hourly.parquet
  fits/as_of={YYYY-MM-DDTHH}/fits.parquet
  calibrated_hourly/date={YYYY-MM-DD}/calibrated_hourly.parquet
```

Each product is an observation-store `Product` (prefix, partition key, merge key, frame model,
file name, table, SQL directory) registered in `aqdt.registry.PRODUCTS` after `sites` and
`observations`. That registration is all the PostGIS layer needs: `apply_schema` creates
`sensor_hourly`, `calibration_fits`, and `calibrated_hourly` from the SQL files under
`src/aqdt/calibration/sql/` (each keyed as the product is, with `site_id` referencing `sites`),
`load_partitions` loads them after a run, and `rebuild` reloads them. Calibration never writes
to PostGIS itself. Each product has a Pydantic record model and a Pandera frame model under
`aqdt.calibration.schemas` / `frames`, held to the same conformance test as the observation
schemas.

## Package Layout

```
src/aqdt/calibration/
  schemas.py    # SensorHourly, CalibrationFit, CalibratedHourly, CalibrationSettings
  frames.py     # Pandera frame models
  products.py   # the three Product definitions
  hourly.py     # aggregate_hourly
  matching.py   # match_references
  fit.py        # fit_calibrations
  apply.py      # apply_calibrations
  sql/          # sensor_hourly, calibration_fits, calibrated_hourly tables
tests/calibration/
```

## Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| Reference matching | Nearest monitor within `max_distance_m`; pooled fit beyond it | Matching by distance keeps each fit interpretable as a comparison between one sensor and one monitor, and states plainly when no comparison is possible — which is what the metro's size demands. |
| Fit form | Per-sensor OLS slope and intercept on the Barkjohn-corrected value | Bias and gain both vary by sensor. Humidity nonlinearity is already handled nationally by Barkjohn, and OLS with the reference as target is the convention in the sensor-calibration literature. |
| Acceptance gate | `n_pairs ≥ 72` and `slope > 0` | A negative slope means the pairing is meaningless. `r2` is deliberately not gated on: it depends on how much the air varied in the window rather than on the sensor, so gating on it would reject good sensors in calm weeks. |
| Fallback | Pooled OLS over accepted sensors' pairs | The pooled fit is the network's average bias and gain — a better estimate than none for a sensor with no usable reference. |
| Reference eligibility | Nearest monitor with at least one trusted observation in the window | A monitor with no data in the window yields zero pairs and would push the sensor to the pooled fit even when a slightly farther monitor has a full month; the match should be between things that can be compared. |
| Sensors that receive a fit row | Every `low_cost_sensor` site in the archive, per `as_of` | "What is this sensor's fit?" always has one answer, and a silent sensor is visibly `pooled` or `uncalibrated` rather than absent. |
| Pooled-fit gate | Same `slope > 0` as per-sensor fits | A negative network-wide slope is as meaningless as a negative per-sensor one; serving it would calibrate every fallback sensor backwards. |
| Fit-age cap when applying | None; `fit_as_of` recorded | A stale fit is a better estimate than none, and the row says how stale it is; a cap would silently drop sensors during any gap in fit runs. |
| Hourly aggregation minimum | None; `n_snapshots` recorded | Snapshot density reflects ingestion cadence, not sensor quality; consumers can filter on the count. |
| Distance computation | GeoPandas in EPSG:26918 | Keeps calibration a pure function of the archive, runnable and testable without a database; PostGIS remains the serving layer for the results. |
| Refit cadence | Daily | Sensor drift is slow; daily refits track it while keeping the fit history readable. |
| Window length | 30 days | Long enough for the pair count and for a range of concentrations; short enough to follow drift. Tunable. |

## Open Questions & Future Decisions

1. Whether `max_distance_m = 10 000` leaves too many D.C. sensors on the pooled fit. Decide from
   the matched-fraction after the first fit run.
2. Whether a sensor with an accepted fit should also be checked for a fit that changed sharply
   between refits (a drift or fault detector). Evaluation-adjacent; belongs there or here.
3. Whether calibration should use AirNow's `RawConcentration` rather than `Value` as the
   reference; depends on what AirNow's processing does to `Value`.
4. Extending the fit with a humidity term per sensor if residuals show humidity structure after
   Barkjohn.

## References

- `docs/high-level-design.md` — Calibration is distance-aware from the outset.
- `docs/intent/observation-store/observation-store-design.md` — inputs, write mechanics.
- `docs/intent/purpleair-ingest/purpleair-ingest-design.md` — `pm25_corrected` semantics.
- O'Regan et al. (2026), calibration section — prior art for sensor-to-monitor calibration.
- Barkjohn et al. (2021) — the correction applied upstream of this fit.
