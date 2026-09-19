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

An hour with no trusted snapshot has no row. `n_snapshots` is carried so consumers can weight or
filter by how well the hour was sampled; the aggregation itself imposes no minimum, because
snapshot density is a property of how often ingestion ran, not of the sensor.

AirNow rows are already hourly; `observed_at` is taken as the start of the hour the value
describes.

## Matching

For each PurpleAir site, the reference monitor is the nearest AirNow site by great-circle
distance within `max_distance_m` (default 10 000 m). The match is recorded on the fit as
`ref_site_id` and `distance_m`. A site with no reference within range is matched to nothing and
receives the pooled fit.

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

over a rolling window of `window_days` (default 30) ending at the calibration's `as_of` hour.

A per-sensor fit is **accepted** when `n_pairs ≥ min_pairs` (default 72, three days of hourly
pairs) and `slope > 0`. Otherwise the sensor receives the **pooled fit**: the same OLS over the
union of all accepted sensors' pairs. If no sensor's fit is accepted, the pooled fit is undefined
and every sensor is `uncalibrated` for that `as_of`.

`CalibrationFit`: `site_id`, `as_of`, `status` (`fitted` | `pooled` | `uncalibrated`),
`ref_site_id`, `distance_m`, `window_start`, `window_end`, `n_pairs`, `slope`, `intercept`,
`r2`, `rmse`. For a pooled fit, `ref_site_id` and `distance_m` are null and `n_pairs` is the pooled
count. Diagnostics are recorded, not gated on: a low `r2` in a low-variance week is not evidence of
a bad sensor.

## Applying

`CalibratedHourly`: `site_id`, `hour`, `pm25_calibrated`, `pm25_corrected_mean`, `n_snapshots`,
`fit_as_of`, `fit_status`. `pm25_calibrated = intercept + slope × pm25_corrected_mean`, clamped at
0, using the most recent fit for the site with `as_of ≤ hour`; null with `fit_status =
uncalibrated` when no such fit exists.

Reference monitors are not calibrated; the fusion layer takes their `pm25_raw` directly.

## Runs

- `fit_calibrations(archive_uri, as_of, settings) -> list[CalibrationFit]` — aggregate, match,
  fit, write fits.
- `apply_calibrations(archive_uri, start, end) -> frame` — aggregate the window, apply the
  applicable fits, write `CalibratedHourly`.

Fits are refit once per day (`as_of` at 00:00 UTC) in normal operation; any `as_of` hour is valid
for backfill or experiment. Both runs are idempotent for a given archive state.

## Storage

Products are written to the archive under `calibration/` and loaded into PostGIS beside the
observation tables:

```
{archive_uri}/calibration/
  sensor_hourly/date={YYYY-MM-DD}/sensor_hourly.parquet
  fits/as_of={YYYY-MM-DDTHH}/fits.parquet
  calibrated_hourly/date={YYYY-MM-DD}/calibrated_hourly.parquet
```

PostGIS: `sensor_hourly`, `calibration_fits`, `calibrated_hourly`, keyed as the products are.
Writes use the observation store's partition-write and upsert mechanics. Each product has a
Pydantic record model and a Pandera frame model under `aqdt.calibration.schemas` / `frames`,
held to the same conformance test as the observation schemas.

## Package Layout

```
src/aqdt/calibration/
  schemas.py    # SensorHourly, CalibrationFit, CalibratedHourly, CalibrationSettings
  frames.py     # Pandera frame models
  hourly.py     # aggregate_hourly
  matching.py   # match_references
  fit.py        # fit_calibrations
  apply.py      # apply_calibrations
tests/calibration/
```

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| Reference matching | Nearest monitor within `max_distance_m`; pooled fit beyond it | Single network-wide reference; inverse-distance blend of all monitors in range; distance as a covariate in one regional model | A single reference ignores the metro's size — the weakness this design exists to avoid. A blended reference is not a measurement anyone made and muddies the diagnostics. Nearest-within-radius keeps each fit interpretable as a sensor-vs-monitor comparison and states plainly when no comparison is possible. |
| Fit form | Per-sensor OLS slope and intercept on the Barkjohn-corrected value | Intercept only; slope only; refit humidity from scratch; Deming regression | Bias and gain both vary by sensor. Humidity nonlinearity is already handled nationally by Barkjohn; refitting it per sensor with a few hundred pairs overfits. Deming is defensible (both sides have error) but its variance-ratio input is unknown here; OLS with the reference as target is the convention in the sensor-calibration literature. |
| Acceptance gate | `n_pairs ≥ 72` and `slope > 0` | Also gate on `r2`; no gate | A negative slope means the pairing is meaningless. `r2` depends on how much the air varied in the window, not only on the sensor, so gating on it rejects good sensors in calm weeks. |
| Fallback | Pooled OLS over accepted sensors' pairs | Pass through uncorrected; nearest-sensor's fit; no value | The pooled fit is the network's average bias and gain — a better estimate than none. Borrowing a neighbour's fit assumes neighbours share faults, which the data does not support. |
| Hourly aggregation minimum | None; `n_snapshots` recorded | Require ≥ N snapshots per hour | Snapshot density reflects ingestion cadence, not sensor quality; consumers can filter on the count. |
| Distance computation | GeoPandas in EPSG:26918 | PostGIS `ST_DWithin` on geography | Keeps calibration a pure function of the archive, runnable and testable without a database; PostGIS remains the serving layer for the results. |
| Refit cadence | Daily | Hourly; weekly | Sensor drift is slow; daily refits track it while keeping the fit history readable. |
| Window length | 30 days | 7; 14; 60 | Long enough for the pair count and for a range of concentrations; short enough to follow drift. Tunable. |

## Open Questions & Future Decisions

### Resolved

1. ✅ Flagged observations are excluded from fitting and from hourly aggregation; nothing is
   calibrated that was not trusted.

### Deferred

1. Confirm AirNow's hour convention (whether `UTC` labels the start or the end of the averaging
   hour) against the API documentation before the alignment is coded; the aggregation window
   shifts by one hour if it is the end.
2. Whether `max_distance_m = 10 000` leaves too many D.C. sensors on the pooled fit. Decide from
   the matched-fraction after the first fit run.
3. Whether a sensor with an accepted fit should also be checked for a fit that changed sharply
   between refits (a drift or fault detector). Evaluation-adjacent; belongs there or here.
4. Whether calibration should use AirNow's `RawConcentration` rather than `Value` as the
   reference; depends on what AirNow's processing does to `Value`.
5. Extending the fit with a humidity term per sensor if residuals show humidity structure after
   Barkjohn.

## References

- `docs/high-level-design.md` — Calibration is distance-aware from the outset.
- `docs/intent/observation-store/observation-store-design.md` — inputs, write mechanics.
- `docs/intent/purpleair-ingest/purpleair-ingest-design.md` — `pm25_corrected` semantics.
- O'Regan et al. (2026), calibration section — the single-reference approach this design departs
  from.
- Barkjohn et al. (2021) — the correction applied upstream of this fit.
