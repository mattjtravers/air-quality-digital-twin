---
design: evaluation-design
prefix: EVAL
---

# Evaluation — EARS Specs

Facets: `IN` (inputs read from the archive), `MATCH` (matchups of surface and monitor), `METRIC`
(metric definitions), `SPLIT` (subsets and splits), `AVAIL` (availability report), `CALSE`
(calibration fit covariance), `TOTVAR` (analytic total variance), `MC` (Monte Carlo propagation),
`DECOMP` (residual decomposition), `SENS` (variogram sensitivity), `BUDGET` (error budget), `FIG`
(report figures), `RUN` (entry point and report), `CFG` (settings and tests), `LOMO`
(leave-one-monitor-out), `BASE` (baselines).

## Inputs

- [x] **EVAL-IN-001**: When evaluating a window, evaluation shall read every input from the archive through the observation store's `read_partitioned`, `read_observations`, and `read_sites`, and the surface rasters by their `surface_uri`, and shall not read from PostGIS.
- [x] **EVAL-IN-002**: Evaluation shall write nothing to the archive.
- [x] **EVAL-IN-003**: Evaluation shall require the window's `start` and `end` to be timezone-aware, convert each to UTC and truncate it to the hour, evaluate the hours `H` with `start <= H < end`, and fail with a validation error if either is naive or `end <= start` after truncation.

## Matchups

- [x] **EVAL-MATCH-001**: For every hour `H` in the window whose `surface_fits` row has `status = kriged`, evaluation shall form one matchup for each AirNow observation at `H` with non-null `pm25_raw` whose monitor lies inside the grid and whose raster cell is not NaN.
- [x] **EVAL-MATCH-002**: When forming a matchup, evaluation shall set `surface` and `variance` to bands 1 and 2 of the raster cell containing the monitor's EPSG:26918 coordinates, found by inverting the raster's geotransform, with column `i` covering `[x_min + i·res, x_min + (i+1)·res)` and row `j` covering `(y_max − (j+1)·res, y_max − j·res]`.
- [x] **EVAL-MATCH-003**: When forming a matchup, evaluation shall set `obs` to the monitor's `pm25_raw`, `qc_flags` to the observation's flags, `flagged` to whether `qc_flags` is non-empty, `sd = √variance`, `error = surface − obs`, and `z = error / sd`, with `z` null when `sd = 0`.
- [x] **EVAL-MATCH-004**: When forming the matchups of hour `H`, evaluation shall rebuild the hour's kriging-input sensors from the archive with fusion's `hour_sensors` and `hour_points`, set `n_sensors` to the number of sensor rows, `pooled_share` and `fitted_share` to the fraction of those rows with `fit_status` `pooled` and `fitted`, and `nearest_sensor_km` to the distance in kilometres from the monitor to the nearest kriging point.
- [x] **EVAL-MATCH-005**: When the rebuilt kriging inputs of hour `H` have an `n_sensors` or `n_points` different from the hour's `surface_fits` row, evaluation shall set `inputs_reproduced` false on the hour's matchups and keep them in every accuracy metric.
- [x] **EVAL-MATCH-006**: When reading a kriged hour's raster, evaluation shall compare the SHA-256 of its bytes with the row's `surface_sha256` and, on a mismatch, still score the raster and record the hour as mismatched in the availability report.

## Metrics

- [x] **EVAL-METRIC-001**: For a set of matchups, evaluation shall compute `n`, `bias` (mean `error`), `mae` (mean `|error|`), `rmse` (√ mean `error²`), `r2` (`1 − Σ error² / Σ (obs − mean obs)²`), and `pearson_r` (Pearson correlation of `surface` and `obs`), with `r2` and `pearson_r` null when `n < 2` or either variance is zero.
- [x] **EVAL-METRIC-002**: For a set of matchups, evaluation shall compute `cov1_krig` and `cov2_krig` as the fraction of matchups with non-null `z` having `|z| <= 1` and `|z| <= 2`, and `cov1_total` and `cov2_total` as the fraction of matchups with non-null `z_total` and `inputs_reproduced` having `|z_total| <= 1` and `|z_total| <= 2`, each null when no matchup qualifies.

## Subsets and Splits

- [x] **EVAL-SPLIT-001**: Evaluation shall compute every metric for two subsets — `unflagged` (matchups with `flagged` false) and `all` — and shall report `unflagged` as the headline.
- [x] **EVAL-SPLIT-002**: Within each subset, evaluation shall compute the metrics overall and per group of each split: PM2.5 level of `obs` (`<5` for obs < 5, `5–9` for 5 ≤ obs ≤ 9, `>9` for obs > 9 µg/m³), UTC hour of day, UTC calendar day, and pooled-share band.
- [x] **EVAL-SPLIT-003**: When forming pooled-share bands, evaluation shall cut the hourly `pooled_share` of the window's kriged hours at its tertiles, merge coinciding edges, and label each band with its edges.
- [x] **EVAL-SPLIT-004**: Evaluation shall write the metrics as one table with columns `subset`, `split`, `group`, followed by the metrics of EVAL-METRIC-001 and EVAL-METRIC-002.

## Availability

- [x] **EVAL-AVAIL-001**: Evaluation shall report the number of kriged hours, the number of distinct UTC days with at least one kriged hour, every hour in the window with no `surface_fits` row, and every hour whose row is not `kriged` with its status.
- [x] **EVAL-AVAIL-002**: Evaluation shall report, per AirNow monitor, whether it lies inside the grid, its observations in kriged hours, its matchups, its observations with null `pm25_raw`, its observations at NaN cells, its flagged observations, and the number of the window's kriging-input sensors whose `fitted` fit references it as `ref_site_id`, with their median `distance_m`.
- [x] **EVAL-AVAIL-003**: Evaluation shall report the hours with `inputs_reproduced` false and the hours whose raster does not match its `surface_sha256`.
- [x] **EVAL-AVAIL-004**: When the window has no kriged hour, evaluation shall complete, write the report with empty metric tables, skip the Monte Carlo, the maps, and every figure drawn from matchups, and state in the summary that no hour was kriged.

## Calibration Uncertainty

- [x] **EVAL-CALSE-001**: For each `(site_id, fit_as_of)` with `fit_status = fitted` and each `fit_as_of` with `fit_status = pooled` used by a kriging-input sensor in the window, evaluation shall rebuild the fit's pairs as `fit_calibrations` does — PurpleAir and AirNow observations over `[fit_as_of − window_days, fit_as_of)` from `read_observations`, aggregated with calibration's `aggregate_hourly`, passed to `calibration_pairs` — using the calibration settings from the environment, taking the sensor's own pairs for `fitted` and the pooled pairs for `pooled`.
- [x] **EVAL-CALSE-002**: For rebuilt pairs with `n >= 3`, evaluation shall compute `σ̂² = Σ residual² / (n − 2)` about the OLS line through the pairs, `Var(b) = σ̂² / Sxx`, `Var(a) = σ̂² (1/n + x̄² / Sxx)`, and `Cov(a, b) = −x̄ σ̂² / Sxx`.
- [x] **EVAL-CALSE-003**: Evaluation shall inflate `Var(a)`, `Var(b)`, and `Cov(a, b)` by `n / n_eff`, with `n_eff = n (1 − ρ₁) / (1 + ρ₁)` and `ρ₁` the lag-1 autocorrelation of the residuals over pairs of consecutive hours within one sensor's time-ordered series, clipped to `[0, 0.99]`, and shall report the uncorrected values beside the corrected ones.
- [x] **EVAL-CALSE-004**: When rebuilt pairs number fewer than three, evaluation shall record no covariance for the fit, and the matchups of every hour in which a kriging-input sensor uses that fit shall carry a null `cal_variance`.
- [x] **EVAL-CALSE-005**: Evaluation shall write a fit-uncertainty table with, per fit, the status, `n`, `ρ₁`, `n_eff`, `σ̂²`, the corrected and uncorrected `Var(a)`, `Var(b)`, `Cov(a, b)`, and the stored and rebuilt `n_pairs`, `slope`, and `intercept`, and shall not fail when stored and rebuilt values differ.

## Total Variance

- [x] **EVAL-TOTVAR-001**: When computing a matchup's calibration term, evaluation shall solve the ordinary-kriging system for the weights `λ` at the monitor's coordinates from the hour's kriging points and the hour's fixed variogram — the row's `variogram_model` with PyKrige parameters `[sill − nugget, range_m, nugget]` — using PyKrige's variogram functions, such that `λ` sums to 1 and `λᵀ z` equals the unclamped estimate at the same location of a PyKrige `OrdinaryKriging` built on the same points with those parameters fixed to within a relative tolerance of 1e-6.
- [x] **EVAL-TOTVAR-002**: Evaluation shall build the sensor covariance `Σ_s` of an hour's kriging-input sensors with diagonal `[1, x_i] C_i [1, x_i]ᵀ + σ̂²_i`, off-diagonal `[1, x_i] C_p [1, x_j]ᵀ` for two `pooled` sensors using the same `fit_as_of`, and 0 otherwise, where `x_i` is the sensor's `pm25_corrected_mean`, `C_i` its fit's corrected covariance matrix of `(a, b)`, and `σ̂²_i` its fit's residual variance.
- [x] **EVAL-TOTVAR-003**: Evaluation shall set `cal_variance = λᵀ M Σ_s Mᵀ λ`, where `M` maps sensors to kriging points with weight `1/k` for each of a point's `k` merged sensors, and shall set `total_variance = variance + cal_variance`, `total_sd = √total_variance`, and `z_total = error / total_sd`, with `z_total` null when `total_sd = 0` or `cal_variance` is null; `cal_variance` is computed from the rebuilt inputs whether or not `inputs_reproduced` is true.

## Monte Carlo

- [x] **EVAL-MC-001**: Evaluation shall select `mc_hours` hours with `inputs_reproduced` from the window's kriged hours using a random generator seeded with `seed`, stratified across tertiles of the hour's mean monitor `obs` and preferring hours on distinct days, and shall select every eligible hour when fewer than `mc_hours` are eligible.
- [x] **EVAL-MC-002**: For each selected hour and each of `mc_draws` draws, evaluation shall draw each fitted sensor's `(a, b)` from `N(θ̂_i, C_i)` independently, one `(a, b)` per `fit_as_of` from `N(θ̂_p, C_p)` shared by every pooled sensor using it, and each sensor's residual from `N(0, σ̂²_i)` independently, where `θ̂` are the stored slope and intercept; recalibrate each sensor as `max(0, a + b x + residual)`; merge co-located sensors as fusion does; and krige the monitors' coordinates with a PyKrige `OrdinaryKriging` built with the hour's fixed variogram (EVAL-TOTVAR-001).
- [x] **EVAL-MC-003**: Evaluation shall set each sampled matchup's Monte Carlo `cal_variance` to the variance of its predictions across draws, and shall repeat the propagation with pooled parameters drawn independently per sensor, reporting both beside the analytic `cal_variance`.

## Residual Decomposition

- [x] **EVAL-DECOMP-001**: For each fit with rebuilt pairs, evaluation shall compute the detector-noise term (a) as the mean over the fit's pairs of `(CORRECTION_SLOPE · b)² · mean((A − B)² / 4) / k`, where `A` and `B` are the channel values of the sensor's `k` snapshots in the pair's hour that `aggregate_hourly` aggregates (no QC flags, non-null `pm25_corrected`) and `b` the fit's slope.
- [x] **EVAL-DECOMP-002**: Evaluation shall compute the reference term (b) as `σ_ref²` at each of two hourly precision bounds, 0.5 µg/m³ (Teledyne T640) and 1.8 µg/m³ (Met One BAM-1020), and the remainder (c) as `σ̂² − (a) − (b)`, reporting a negative remainder as 0 and counting the fits for which it was.
- [x] **EVAL-DECOMP-003**: Evaluation shall report the pair-weighted shares of (a), (b), and (c) in `σ̂²` overall, for fitted fits, and for pooled fits, at each reference bound, and shall not add (a) or (b) to any variance used in EVAL-TOTVAR or EVAL-MC.

## Variogram Sensitivity

- [x] **EVAL-SENS-001**: For each kriged hour, evaluation shall fit the candidate variogram model not recorded on the hour's row to the hour's kriging points with fusion's settings (FUS-VAR-001), krige the monitors' coordinates with both the recorded variogram and the alternative, and report the root-mean-square difference of the two predictions, null when the alternative is not eligible (FUS-VAR-003).

## Error Budget

- [x] **EVAL-BUDGET-001**: Evaluation shall write an error budget with columns `source`, `stage`, `type` (`random` or `systematic-shared`), `handling` (`quantified-included`, `quantified-diagnostic`, or `listed-not-modeled`), and `magnitude`, as CSV and as a rendered table figure.
- [x] **EVAL-BUDGET-002**: The error budget shall contain at least the rows: PurpleAir detector noise; Barkjohn correction coefficients; AirNow reference measurement error; AirNow preliminary data, `Value` vs `RawConcentration`, and flatline flags; per-sensor calibration fit parameters; pooled calibration fit parameters; calibration residual (representativeness, siting, model error); regression dilution; kriging interpolation error; variogram fit; and monitors as calibration targets for nearby sensors — each with the stage, type, and handling given in the evaluation design.
- [x] **EVAL-BUDGET-003**: Where a budget row's magnitude is quantified by the run (detector noise, calibration parameters, calibration residual, kriging interpolation, variogram sensitivity, AirNow flag rate, slope distribution), evaluation shall fill it from the run with units; otherwise it shall give the published value or "not quantified".

## Figures

- [x] **EVAL-FIG-001**: Evaluation shall write the figures `timeseries`, `scatter`, `coverage`, `bias_vs_pooled`, `decomposition`, `mc_vs_analytic`, `error_budget`, `maps_pm25`, and `maps_sd` as PNG at 300 dpi, each beside a CSV of the same name holding the data drawn.
- [x] **EVAL-FIG-002**: Every figure shall be between 3.3 and 6.5 inches wide, label every data axis with its quantity and units, and have every text element inside the saved canvas.
- [x] **EVAL-FIG-003**: The `timeseries` figure shall plot per-hour bias and RMSE of the `unflagged` subset, coloured by UTC date on a sequential colormap with a date colorbar, with the interquartile range of the hour's matchup errors; the `scatter` figure shall plot `surface` against `obs` with the 1:1 line and `r2`; the `coverage` figure shall show ±1 SD and ±2 SD coverage with kriging SD and with total SD against 68.3% and 95.4%; the `bias_vs_pooled` figure shall plot per-hour bias against `pooled_share` with a least-squares line.
- [x] **EVAL-FIG-004**: Evaluation shall choose four kriged hours on four distinct days, one from each quartile of the hour's mean monitor `obs` — relaxing the distinct-day rule when too few days qualify, and taking every kriged hour when there are fewer than four — and draw them through the maps component as `maps_pm25` (estimate) and `maps_sd` (standard deviation), each a small-multiples figure on its fixed scale with sensors and monitors overlaid.
- [x] **EVAL-FIG-005**: Evaluation shall report the window's maximum surface value and maximum SD across kriged hours against the maps' `pm25_max` and `sd_max`, and the fraction of cells above each.

## Runs and Report

- [x] **EVAL-RUN-001**: Evaluation shall expose `evaluate(archive_uri, start, end, out_dir, settings) -> EvaluationSummary`, which writes into `out_dir` the files `matchups.csv`, `metrics.csv`, `availability.csv`, `fit_uncertainty.csv`, `montecarlo.csv`, `decomposition.csv`, `variogram_sensitivity.csv`, `error_budget.csv`, the figures of EVAL-FIG-001, `summary.md`, and `run.json`, and returns the headline metrics.
- [x] **EVAL-RUN-002**: `summary.md` shall state the hours and days available and the gaps; the headline (`unflagged`) metrics and their `all` equivalents; coverage with kriging SD and with total SD; the median ratio of Monte Carlo to analytic `cal_variance`; the decomposition shares; the maps' clipping; and the caveats that AirNow data are preliminary, the matchup count of each split group, that weather differs between days, that monitors are calibration targets for nearby sensors, that the variogram nugget and the calibration residual can partly count the same noise, that the residual term overlaps the spatial variability the kriging variance describes, that variogram uncertainty is in neither term, and that residuals of different sensors in the same hour are treated as independent.
- [x] **EVAL-RUN-003**: `run.json` shall record the window, the evaluation and map settings, the seed, the archive URI, the package version, and the git commit when the code runs from a git checkout.
- [x] **EVAL-RUN-004**: Two evaluations of the same window with the same settings and seed against an unchanged archive shall write identical CSV files.

## Settings and Tests

- [x] **EVAL-CFG-001**: Evaluation shall define `EvaluationSettings` (Pydantic settings) with `mc_hours` (`AQDT_EVAL_MC_HOURS`, default 24), `mc_draws` (`AQDT_EVAL_MC_DRAWS`, default 500), and `seed` (`AQDT_EVAL_SEED`, default 0), each optional, failing validation naming the setting when `mc_hours` or `mc_draws` is below 1 or `seed` is negative.
- [x] **EVAL-CFG-002**: Evaluation tests shall run against a temporary local archive populated from small synthetic frames and rasters and shall require neither a network connection nor PostGIS.

## Leave One Monitor Out

- [D] **EVAL-LOMO-001**: For each of one or two chosen kriged hours and each matchup monitor `X` of the hour, evaluation shall refit the hour's `fit_as_of` fits with calibration's `fit_sensors` on the monitor observations excluding `X`, recalibrate the hour's sensors with those fits, krige the hour as fusion would (FUS-VAR, FUS-PTS), predict at `X`, and report that error beside the stored surface's error at `X`.

## Baselines

- [D] **EVAL-BASE-001**: For every matchup, evaluation shall predict the monitor's value by inverse-distance weighting with power 2 of the hour's kriging points.
- [D] **EVAL-BASE-002**: Evaluation shall train a random forest (scikit-learn, seeded with `seed`) on the kriging points of all evaluated hours with features `x`, `y`, UTC hour of day, and the hour's median point value, and predict each matchup's monitor value.
- [D] **EVAL-BASE-003**: Evaluation shall report kriging, IDW, and random forest in one table of the EVAL-METRIC-001 metrics over the same `unflagged` matchups.
