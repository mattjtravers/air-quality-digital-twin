# Calibration

Fits are read from `calibration/fits` in the archive. Each fit's window is
`[as_of − 30 d, as_of)`; matching radius 10 km;
a per-sensor fit needs ≥ 72 hourly pairs, a non-constant input and a positive slope.
The model is OLS `monitor = intercept + slope × pm25_corrected_mean` on hourly pairs; applied
values are `max(0, intercept + slope × x)`.

## Fit as of 2026-10-25T00 (window `[2026-09-25T00, 2026-10-25T00)`)

- Sensors: **194** — fitted **99**, pooled **95**,
  uncalibrated **0**.
- Sensors with an eligible monitor within 10 km: **137**
  (38 of them pooled because their own pairs failed the gates).
- Match distance for fitted sensors: min **0.30 km**, median
  **4.19 km**, max **9.75 km**.
- Pooled fit: slope **0.407**, intercept
  **2.800 µg/m³**, r² **0.269**, RMSE
  **2.717 µg/m³**, n **55,797** pairs
  (the union of the fitted sensors' pairs).

Distributions over the 99 fitted sensors (distance km, intercept and RMSE µg/m³):

| quantity | n | min | q1 | median | q3 | max | mean |
|---|---|---|---|---|---|---|---|
| distance_km | 99 | 0.301 | 2.430 | 4.188 | 6.086 | 9.748 | 4.355 |
| slope | 99 | 0.017 | 0.465 | 0.670 | 0.783 | 1.193 | 0.618 |
| intercept | 99 | -0.677 | 1.037 | 1.718 | 2.446 | 4.940 | 1.831 |
| r2 | 99 | 0.001 | 0.277 | 0.461 | 0.568 | 0.750 | 0.416 |
| rmse | 99 | 1.195 | 2.045 | 2.221 | 2.429 | 3.198 | 2.203 |
| n_pairs | 99 | 74.000 | 526.000 | 610.000 | 677.500 | 697.000 | 563.606 |

## All 25 daily fits in the window (2026-10-01 … 2026-10-25, 00 UTC)

Range of the per-fit counts and pooled coefficients across the daily fits:

| quantity | min | median | max |
|---|---|---|---|
| sensors | 185.000 | 191.000 | 194.000 |
| fitted | 89.000 | 98.000 | 99.000 |
| pooled | 91.000 | 93.000 | 96.000 |
| uncalibrated | 0.000 | 0.000 | 0.000 |
| monitor_within_10km | 130.000 | 135.000 | 137.000 |
| pooled_slope | 0.159 | 0.484 | 0.576 |
| pooled_intercept | 2.009 | 2.624 | 2.860 |
| pooled_r2 | 0.034 | 0.339 | 0.471 |
| pooled_n | 14660.000 | 37586.000 | 55801.000 |

Distributions over every fitted sensor-fit in the window
(2422 sensor-fits; a sensor fitted on k days counts k times):

| quantity | n | min | q1 | median | q3 | max | mean |
|---|---|---|---|---|---|---|---|
| distance_km | 2422 | 0.301 | 2.403 | 4.175 | 6.093 | 9.748 | 4.356 |
| slope | 2422 | 0.001 | 0.493 | 0.679 | 0.797 | 3.949 | 0.647 |
| intercept | 2422 | -2.482 | 1.134 | 1.862 | 2.537 | 6.756 | 1.859 |
| r2 | 2422 | 0.000 | 0.291 | 0.544 | 0.637 | 0.909 | 0.472 |
| rmse | 2422 | 0.622 | 1.726 | 1.995 | 2.218 | 3.511 | 1.950 |
| n_pairs | 2422 | 72.000 | 243.000 | 374.000 | 516.000 | 697.000 | 383.091 |

Rebuilding each day's matching and pairs with `aggregate_hourly` + `calibration_pairs`
reproduces the stored `fitted` set on every day: **True**.
Per-day rows: `calibration_daily_fits.csv`.

## Sensors assigned to each monitor (as of 2026-10-25T00)

| ref_site_id | name | sensors_within_10km | fitted | pooled | distance_km_min | distance_km_median | distance_km_max | eligible |
|---|---|---|---|---|---|---|---|---|
| airnow:840110010041 | RIVER_Terrace | 12 | 12 | 0 | 1.55 | 3.68 | 7.93 | True |
| airnow:840110010043 | McMillan Reservoir | 42 | 32 | 10 | 0.99 | 3.14 | 8.60 | True |
| airnow:840110010051 | DCNearRoad | 24 | 19 | 5 | 0.01 | 4.30 | 6.55 | True |
| airnow:840110010053 | King Greenleaf Rec Center | 11 | 8 | 3 | 0.51 | 2.20 | 9.09 | True |
| airnow:840110010055 | Bald Eagle | 5 | 4 | 1 | 0.99 | 2.89 | 8.80 | True |
| airnow:840240330030 | HU-Beltsville | 21 | 8 | 13 | 0.30 | 8.85 | 9.07 | True |
| airnow:840510590030 | FRANCONIA | 6 | 3 | 3 | 4.01 | 6.16 | 9.38 | True |
| airnow:840510590031 | Northern VA Near Road | 9 | 6 | 3 | 0.99 | 5.28 | 9.75 | True |
| airnow:840511071005 | ASHBURN | 7 | 7 | 0 | 0.57 | 6.79 | 9.46 | True |

`eligible` means the monitor had at least one trusted hourly value in the fitting window.
Every sensor's assignment, distance, own pair count and status:
`calibration_sensor_assignments.csv`.

## Files

| File | Contents |
|---|---|
| `calibration_fits_2026-10-25T00.csv` | The stored fit rows for as_of 2026-10-25T00 |
| `calibration_sensor_assignments.csv` | Each sensor's matched monitor, distance, pairs, status |
| `calibration_per_monitor.csv` | Sensors within 10 km, fitted and pooled, per monitor |
| `calibration_distributions.csv` | Distance, slope, intercept, r², RMSE, n_pairs distributions |
| `calibration_daily_fits.csv` | Counts and pooled coefficients for each daily fit |
| `calibration_across_daily_fits.csv` | Min / median / max of those across the daily fits |
