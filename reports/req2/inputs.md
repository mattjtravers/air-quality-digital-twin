# Inputs and QC

Window `[2026-10-01T00, 2026-10-25T17)` UTC (593 hours), read from the archive.
Flags are counted per record; a record may carry several flags, so per-flag shares do not sum
to the any-flag share. QC flags, never drops: every flagged record is still in the archive.

## PurpleAir

- Snapshots: **97,589** from **193** distinct sensors.
- Snapshots per sensor-hour: median **1**, IQR **1–1** (IQR 0);
  over 97,386 sensor-hours with any snapshot; only
  200 sensor-hours hold more than one snapshot (one snapshot per
  hourly ingest run).
- Null `pm25_corrected`: 16,841 snapshots — the 11,599 flagged ones
  (every PurpleAir flag raised here withholds the correction) plus 5,242 unflagged
  snapshots missing an input (all of them
  missing humidity).

| flag | count | share |
|---|---|---|
| missing_value | 0 | 0.00% |
| channel_missing | 1062 | 1.09% |
| out_of_range | 6477 | 6.64% |
| channel_disagreement | 10537 | 10.80% |
| any_flag | 11599 | 11.89% |
| null_pm25_corrected | 16841 | 17.26% |

Flag combinations (`inputs_purpleair_flag_combinations.csv`): every `out_of_range` snapshot
also carries `channel_disagreement`.

| flags | count | share |
|---|---|---|
| (none) | 85990 | 88.11% |
| channel_disagreement+out_of_range | 6477 | 6.64% |
| channel_disagreement | 4060 | 4.16% |
| channel_missing | 1062 | 1.09% |

**Boundary rejections** are not recorded in the archive. Each ingest run counts them in its
`IngestSummary.rejected`, which is printed in the run's GitHub Actions log; rejected rows are
never written, so the archive cannot count them.

## AirNow

- Observations: **5,282** from **9** sites
  (9 registered reference monitors).
- Site-hours: expected **5,337** (9 sites × 593 h), present
  **5,282** (99.0%); trusted (no flag, non-null value)
  **4,809** (90.1%).

| flag | count | share |
|---|---|---|
| missing_value | 7 | 0.13% |
| out_of_range | 0 | 0.00% |
| flatline | 466 | 8.82% |
| site_id_unresolved | 0 | 0.00% |
| any_flag | 473 | 8.95% |

Per site (`inputs_airnow_by_site.csv`):

| site_id | name | present | trusted | flatline | missing_value | expected | present_share |
|---|---|---|---|---|---|---|---|
| airnow:840110010041 | RIVER_Terrace | 592 | 565 | 26 | 1 | 593 | 99.8% |
| airnow:840110010043 | McMillan Reservoir | 593 | 578 | 15 | 0 | 593 | 100.0% |
| airnow:840110010051 | DCNearRoad | 552 | 540 | 8 | 4 | 593 | 93.1% |
| airnow:840110010053 | King Greenleaf Rec Center | 593 | 590 | 3 | 0 | 593 | 100.0% |
| airnow:840110010055 | Bald Eagle | 581 | 480 | 100 | 1 | 593 | 98.0% |
| airnow:840240330030 | HU-Beltsville | 592 | 474 | 117 | 1 | 593 | 99.8% |
| airnow:840510590030 | FRANCONIA | 593 | 520 | 73 | 0 | 593 | 100.0% |
| airnow:840510590031 | Northern VA Near Road | 593 | 530 | 63 | 0 | 593 | 100.0% |
| airnow:840511071005 | ASHBURN | 593 | 532 | 61 | 0 | 593 | 100.0% |

## Hourly aggregation (`calibration/sensor_hourly`)

- Sensor-hours produced: **80,616** from 174
  sensors (a row needs at least one trusted, corrected snapshot;
  16,770 sensor-hours had snapshots but none trusted).
- Sensor-hours with `n_flagged > 0`: **0**
  (0.0%). With one
  snapshot per sensor-hour, a flagged snapshot leaves its hour with no trusted snapshot, so the
  hour produces no row at all rather than a row with `n_flagged > 0`; flagged snapshots show up
  in the 16,770 sensor-hours without a row.
- Sensors per hour: median **138**, range **0–154**
  (3 hours with none: 2026-10-01T17, 2026-10-05T19, 2026-10-05T20).
- Re-running `aggregate_hourly` on the window's observations reproduces the stored product
  exactly: **True**.

## Files

| File | Contents |
|---|---|
| `inputs_purpleair_summary.csv` | PurpleAir snapshot, sensor and snapshots-per-sensor-hour counts |
| `inputs_airnow_summary.csv` | AirNow observation, site and site-hour counts |
| `inputs_qc_flags.csv` | Count and share of each QC flag, by source |
| `inputs_purpleair_flag_combinations.csv` | PurpleAir snapshots by exact flag combination |
| `inputs_airnow_by_site.csv` | AirNow present / trusted / flagged hours per site |
| `inputs_hourly_summary.csv` | Sensor-hour aggregation counts |
| `inputs_sensors_per_hour.csv` | Sensors with a sensor-hour row, per window hour |
