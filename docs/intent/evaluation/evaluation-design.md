---
parent: high-level-design
prefix: EVAL
---

# Evaluation

## Context and Design Philosophy

Fusion writes an hourly PM2.5 surface and its kriging variance, kriged from calibrated PurpleAir
sensors only. The AirNow reference monitors stay out of the surface, so they are an independent
check on it at sites it never saw (HLD § Fusion is observational). Evaluation runs that check over
any window of stored hours, and asks a second question beside "how close is the surface?": is the
surface's stated uncertainty the right size?

The kriging variance answers "how far is this cell from the sensors, given the hour's spatial
structure". It does not know that every sensor value is itself an estimate — a calibration line
fitted to a month of pairs, with its own error, and for the pooled sensors one network-wide line
whose error is shared by all of them at once. Evaluation adds that calibration uncertainty to the
kriging variance, checks the analytic total against a Monte Carlo propagation, decomposes the
calibration residual into its physical sources, and lists every error source the pipeline knows of
in an error budget, so a reader can see what the surface's uncertainty covers and what it does not.

Principles, in order of precedence when they conflict:

1. **Score what is stored.** The surface scored is the raster in the archive, sampled as any
   consumer would sample it. Re-kriging happens only in the counterfactual checks (§ Leave one
   monitor out, § Baselines), which are reported beside the stored surface's scores, never in
   place of them.
2. **Read-only.** Evaluation reads the archive and writes nothing to it. Its outputs are a report
   directory — tables, figures, a summary — regenerated on demand, not a pipeline product.
3. **Flagged rows are reported, not hidden.** Monitor hours with QC flags are excluded from the
   headline numbers and included in a parallel set, so the effect of the exclusion is visible
   (HLD tenet: flag, never drop).
4. **Shared error is shared.** An error that is common to many sensors (the pooled fit) is
   propagated as a covariance, never as independent per-sensor noise.
5. **Deterministic.** Given the archive, the window, and the seed, every table and figure is
   identical.
6. **Every number carries its uncertainty.** Matchups within a day share weather and are not
   independent, so every reported statistic comes with a 95% interval from a bootstrap that
   resamples whole days (§ Confidence intervals), and a claim is stated as supported only when its
   interval excludes the null value.

## Inputs

All inputs come from the archive through the observation store's primitives (`read_partitioned`,
`read_observations`, `read_sites`, and `partition_objects` for provenance) and the surface rasters
by their `surface_uri`:

- **Surface fits** — fusion's `surface_fits` rows for the window; rows with `status = kriged`
  carry the variogram parameters and the raster's URI.
- **Surface rasters** — band 1 (`pm25`) and band 2 (`pm25_variance`) of each kriged hour.
- **AirNow observations** — `pm25_raw` and `qc_flags` per monitor hour, over the window extended
  by one hour each side for the timing-alignment lags.
- **Sites** — coordinates of sensors and monitors.
- **Calibrated sensor hours** — calibration's `calibrated_hourly`: `pm25_corrected_mean`,
  `pm25_calibrated`, `fit_status`, `fit_as_of` per sensor hour.
- **Calibration fits** — calibration's `fits` rows for every `fit_as_of` the window's sensor hours
  use.
- **PurpleAir observations** — trusted snapshots' channel A and B values (`pm25_channel_a`,
  `pm25_channel_b`), for detector noise; and, with AirNow observations, the pairs each fit was
  fitted on (§ Calibration uncertainty).

The window is half-open `[start, end)` over hours, tz-aware, converted to UTC and truncated to the
hour, as everywhere in the project.

## Matchups

A **matchup** is one (kriged hour, monitor) pair. For every kriged hour `H` in the window and
every AirNow observation at `H` whose monitor lies inside the grid:

| Column | Meaning |
|---|---|
| `hour`, `site_id`, `name` | the hour and the monitor |
| `obs` | the monitor's `pm25_raw`, µg/m³ |
| `qc_flags`, `flagged` | the observation's flags; `flagged` when the list is non-empty |
| `surface` | band 1 of the grid cell containing the monitor, µg/m³ |
| `variance`, `sd` | band 2 of that cell, (µg/m³)², and its square root |
| `error`, `z` | `surface − obs`; `error / sd` (null when `sd = 0`) |
| `nearest_sensor_km` | distance from the monitor to the nearest of the hour's kriging points |
| `n_sensors`, `pooled_share`, `fitted_share` | the hour's kriging-input sensors, and the fraction of them with `fit_status` `pooled` and `fitted` |
| `pooled_share_10km` | the fraction of the hour's kriging-input sensors within 10 km of the monitor whose `fit_status` is `pooled`; null when none is within 10 km |
| `cal_variance` | the calibration-uncertainty contribution at the monitor (§ Total uncertainty), (µg/m³)² |
| `total_variance`, `total_sd`, `z_total` | `variance + cal_variance`, its square root, `error / total_sd` |
| `cal_variance_raw`, `z_total_raw` | the same with the fit covariance not inflated by `n / n_eff` (§ Coverage diagnostics) |
| `points_mean` | the mean of the hour's kriging-point values |
| `inputs_reproduced` | whether the hour's kriging inputs rebuilt from the archive match the surface fit's `n_sensors` and `n_points` |

The cell containing the monitor is found by inverting the raster's geotransform at the monitor's
EPSG:26918 coordinates — the value any GIS reports for that location. Cells are half-open: column
`i` covers `[x_min + i·res, x_min + (i+1)·res)` and row `j` covers `(y_max − (j+1)·res, y_max −
j·res]`, so a point exactly on the grid's east or south edge is outside it. A monitor outside the
grid, an observation with a null `pm25_raw`, and a monitor whose cell is NaN form no matchup; each
is counted in the availability report.

Each raster's SHA-256 is compared with its row's `surface_sha256`. A mismatch (two runs over the
hour raced) does not stop the hour being scored — the raster is still what the archive serves — and
is listed in the availability report.

**Kriging inputs.** The hour's kriging-input sensors are rebuilt from the archive with fusion's own
selection (`hour_sensors`, the selected sensor rows with their fit status, and `hour_points`), so `pooled_share`, `nearest_sensor_km`, and the
calibration covariance refer to the sensors the surface was kriged from. Calibration re-applies over
a trailing window, so the archive can have moved since the surface was written; when the rebuilt
`n_sensors` or `n_points` differ from the surface fit's, `inputs_reproduced` is false. Those
matchups stay in every accuracy metric — the surface is still the stored surface — and are excluded
from total-variance coverage, whose calibration term would then describe different inputs.

## Metrics

For any set of matchups:

| Metric | Definition |
|---|---|
| `n` | number of matchups |
| `bias` | mean of `error` |
| `mae` | mean of `|error|` |
| `rmse` | square root of the mean of `error²` |
| `r2` | R², the skill score `1 − Σ error² / Σ (obs − mean obs)²` — the surface as a predictor of the monitor; can be negative |
| `pearson_r` | Pearson correlation of `surface` and `obs` |
| `pearson_r2` | `pearson_r²`, the squared correlation; equals `r2` only when the OLS line of `obs` on `surface` is the 1:1 line |
| `cov1_krig`, `cov2_krig` | fraction of matchups with `|z| ≤ 1`, `≤ 2`, among those with `sd > 0` |
| `cov1_total`, `cov2_total` | fraction with `|z_total| ≤ 1`, `≤ 2`, among those with `total_sd > 0` and `inputs_reproduced` |
| `cov1_total_raw`, `cov2_total_raw` | the same with `z_total_raw` |

`r2`, `pearson_r`, and `pearson_r2` are null for fewer than two matchups or zero variance.
Gaussian errors with a correct variance would give 68.3% and 95.4% coverage. The report labels
`r2` "R² (skill, 1 − SSE/SS_obs)" and `pearson_r2` "r² (squared correlation)" wherever both
appear, since the two are easily confused and differ whenever the surface is biased or its slope
against the monitors is not 1.

**Subsets.** Every metric table is computed twice: `unflagged` (matchups with `flagged` false —
the headline) and `all`.

**Splits.** Within each subset, metrics are reported overall and split by:

- **observed level** (`level`) of `obs`: `<5`, `5–9` (5 ≤ obs ≤ 9), `>9` µg/m³;
- **predicted level** (`pred_level`): tertiles of `surface` over the subset's matchups;
- **hour level from the sensors** (`hour_mean_points`): tertiles of the hour's `points_mean` over
  the window's kriged hours;
- **hour level from the monitors** (`hour_mean_obs`, labelled *observed-conditioned*): tertiles of
  the hour's mean monitor `obs` over the window's kriged hours with a matchup;
- **UTC hour of day**, 0–23;
- **calendar day** (UTC date of `hour`);
- **pooled-share band**: tertiles of the hourly `pooled_share` across the window's kriged hours;
- **spike hour** (`spike_hour`): `affected` or `other` (§ Spikes);
- **spike day** (`spike_day`): `affected` when the UTC day holds an affected hour, else `other`;
- **variogram** (`variogram`): the hour's class, `pure_nugget`, `short_range`, or `regular`
  (§ Variograms);
- **monitor** (`monitor`): the matchup's `site_id`.

Tertile bands are labelled with their edges; edges that coincide (many values the same) are
merged, so there may be fewer than three bands.

A split by observed level conditions on the outcome: a monitor reading is its true value plus
measurement noise, so the matchups in the lowest observed band are, on average, those whose noise
was negative, and the highest those whose noise was positive. Even a perfect, unbiased surface
then shows positive error in the low band and negative in the high band. The predicted-level and
sensor-hour-level splits condition on quantities that do not contain the monitor's own value, so
a level-dependent bias that survives in them is a property of the surface; the observed-level and
observed-conditioned splits are kept beside them for comparison.

One long table holds them all: `subset`, `split`, `group`, then the metrics and their intervals.

### Confidence intervals

Every metric of a metric table row is given a 95% interval, `<metric>_ci_low` and
`<metric>_ci_high`, from a block bootstrap over UTC days: each of 1000 resamples draws the row's
days with replacement (as many as it has) and recomputes the metric over the matchups of the drawn
days; the interval is the 2.5th and 97.5th percentile of the resampled values. The generator is
seeded with `seed`. Resampling days rather than matchups keeps each day's shared weather and
shared calibration error together, so the interval reflects how many independent days the number
rests on. A row whose matchups fall on fewer than two days has null intervals — the `day` split
always does. The resamples are computed from per-day sums (count, error, absolute error, squared
error, `obs`, `surface` and their squares and cross-product, coverage counts), so each resample is
a weighted sum and the whole table's intervals are cheap.

The same day bootstrap, with the same seed, gives the intervals of every other statistic the
report states with one — paired differences, slopes, regression coefficients, bin means — except
leave one monitor out, which resamples its hours (§ Leave one monitor out).

## Conditional bias

Whether the surface reads high in clean air and low in dirty air is answered on the unflagged
matchups from quantities that do not condition on the monitor's value:

- **Reliability.** Matchups are binned into deciles of `surface`; each bin reports its count, its
  mean `surface`, and its mean `obs` with a day-bootstrap 95% interval. A calibrated surface puts
  the bin means on the 1:1 line.
- **Slopes.** The OLS slope of `obs` on `surface` and of `surface` on `obs`, each with a
  day-bootstrap interval. The first is the reliability slope: 1 for a calibrated surface, above 1
  when the surface is **over-smoothed** (its highs too low and its lows too high), below 1 when it
  is **over-dispersed**. The second falls below 1 for any imperfect predictor, by regression to the
  mean, and is shown for comparison with the observed-level split.

The summary states the verdict from these rules: over-smoothed when the reliability slope's
interval lies above 1, over-dispersed when it lies below 1, otherwise not distinguishable from
calibrated; and "high in clean air, low in dirty air" holds only when the lowest `pred_level`
band's bias interval lies above 0 and the highest band's below 0.

## Availability

The report states, for the window:

- kriged hours and distinct days with at least one kriged hour;
- every hour in the window with no `surface_fits` row (a gap), and every hour whose row is not
  `kriged`, with its status;
- per monitor: observations in kriged hours, matchups formed, observations with null `pm25_raw`,
  flagged observations, whether the monitor lies inside the grid, and how many of the window's
  kriging-input sensors have a `fitted` fit referencing it, with their median distance — a measure
  of how far the check at that monitor depends on the monitor itself;
- hours with `inputs_reproduced` false, and hours whose raster does not match its row's
  `surface_sha256`.

A window with no kriged hour still produces the report: the availability section says so, the
metric tables are empty, and the Monte Carlo, LOMO, the baselines, the maps, and every figure
drawn from matchups are skipped.

## Calibration uncertainty

### Fit covariance

Every calibrated sensor hour names the fit it used (`fit_as_of`, `fit_status`). For each fit in
use, evaluation rebuilds the pairs that fit was fitted on — calibration's own pair construction
(`calibration_pairs`) over `[fit_as_of − window_days, fit_as_of)`, from the PurpleAir and AirNow
observations aggregated with `aggregate_hourly` exactly as `fit_calibrations` does, with the
calibration settings in the environment — and computes the OLS covariance of the intercept `a` and slope `b`:

```
σ̂²      = Σ residual² / (n − 2)
Var(b)  = σ̂² / Sxx
Var(a)  = σ̂² (1/n + x̄² / Sxx)
Cov(a,b) = −x̄ σ̂² / Sxx
```

where `n` is the pair count, `x̄` the mean sensor value, and `Sxx` the sum of squared deviations
of the sensor values. A `fitted` sensor uses its own pairs; every `pooled` sensor of an `as_of`
uses the one pooled fit, over the union of accepted sensors' pairs.

Hourly residuals are serially correlated — a sensor that reads high at one hour usually reads high
at the next — so `n` pairs carry less information than `n` independent ones. The covariance is
inflated by `n / n_eff`, with `n_eff = n (1 − ρ₁) / (1 + ρ₁)` and `ρ₁` the lag-1 autocorrelation
of the time-ordered residuals, clipped to `[0, 0.99]`. For the pooled fit, `ρ₁` is computed over
consecutive-hour pairs within each sensor's series. The uncorrected covariance is reported beside
the corrected one.

`σ̂²` is the residual variance of the fit: everything about a calibrated value the line does not
explain — detector noise, reference error, and representativeness alike. It is the residual term
of the uncertainty calculation in full; nothing is added on top of it (§ Residual decomposition is
diagnostic only).

A rebuilt fit with fewer than three pairs has no residual variance, so its sensors' hours have
no calibration term: their matchups carry a null `cal_variance` and are excluded from
total-variance coverage, like matchups whose inputs were not reproduced.

The rebuilt fit is compared with the stored one. The archive can be revised after a fit was
written (AirNow re-issues preliminary hours), so a rebuilt `n_pairs`, slope, or intercept that
differs from the stored row is recorded in the fit-uncertainty table, not treated as a failure;
so is a difference caused by the environment's calibration settings differing from those the fit
was made with.
The stored slope and intercept are the ones the surface used; the covariance comes from the
rebuilt pairs.

### Calibration error at a sensor hour

For a sensor hour with corrected value `x` and fit parameters with covariance `C = [[Var(a),
Cov(a,b)], [Cov(a,b), Var(b)]]`, the calibrated value's error is the parameter error `[1, x] ·
(θ̂ − θ)` plus the residual. Between sensors `i` and `j` of one hour:

- `i = j`: `[1, x_i] C_i [1, x_i]ᵀ + σ̂²_i`;
- `i ≠ j`, both `pooled` under the same `as_of`: `[1, x_i] C_p [1, x_j]ᵀ` — the shared pooled
  parameter error;
- otherwise: 0 — fitted sensors' parameter errors and all residuals are independent across
  sensors.

This is the sensor covariance `Σ_s`. Co-located sensors merged into one kriging point (FUS-PTS-002)
become one row of the aggregation matrix `M` (points × sensors, `1/k` for each of a point's `k`
sensors), so the points' covariance is `M Σ_s Mᵀ`.

## Total uncertainty

### Analytic

With the variogram fixed, the ordinary-kriging estimate at a location is linear in the point
values, `ẑ₀ = λᵀ z`, with weights `λ` that depend only on the locations and the variogram and sum
to one. Evaluation solves the ordinary-kriging system for `λ` at each monitor directly, from the
hour's points and the variogram model, sill, range, and nugget recorded on the surface fit row
(PyKrige parameters `[sill − nugget, range_m, nugget]`, held fixed),
using PyKrige's variogram functions. The calibration contribution at the monitor is

```
cal_variance = λᵀ M Σ_s Mᵀ λ
total_variance = variance + cal_variance
```

where `variance` is the stored kriging variance at the monitor's cell.

Two simplifications are stated with the result: the calibrated value's clamp at 0 is ignored (the
error is treated as linear), and `λ` is computed at the monitor's coordinates while `variance` is
the containing cell's.

### Monte Carlo

On a sample of hours, evaluation propagates the calibration uncertainty by simulation instead,
through an independent path — PyKrige's own predictor rather than the weights above. For each draw:

1. draw each fitted sensor's `(a, b)` from `N(θ̂_i, C_i)`, independently;
2. draw one pooled `(a, b)` from `N(θ̂_p, C_p)` per `as_of`, shared by every pooled sensor that
   uses it;
3. draw each sensor's residual from `N(0, σ̂²_i)`, independently;
4. recalibrate: `max(0, a + b x + residual)`;
5. merge co-located sensors and krige the monitors' locations with the hour's recorded variogram,
   parameters held fixed.

The variance of the predictions across draws is the Monte Carlo `cal_variance`. A second run
draws the pooled parameters independently per sensor, showing how much treating shared error as
independent understates it. The sample is `mc_hours` hours (default 24) chosen with the seed,
stratified into tertiles of the hour's mean monitor `obs` and spread across days, with `mc_draws`
draws (default 500). Only hours with `inputs_reproduced` are sampled; when fewer than
`mc_hours` are eligible, all of them are.

The report compares Monte Carlo with analytic `cal_variance` per matchup (ratio and scatter). The
analytic value is used for every hour; the Monte Carlo is its check.

### Coverage diagnostics

The calibration variance at a monitor, stated in one line in the summary: each sensor's
calibrated value carries its fit's parameter variance `[1, x] C [1, x]ᵀ` plus the fit's residual
variance `σ̂²` (the diagonal of `Σ_s`); two pooled sensors of the same `as_of` share the pooled
fit's parameter error (the off-diagonal); and the kriging weights carry it to the monitor as
`λᵀ M Σ_s Mᵀ λ`. `C` is the OLS covariance inflated by `n / n_eff`.

When total coverage falls short of nominal, the report shows where the shortfall sits:

- **Three coverages.** Kriging variance alone (`z`); total with the uninflated OLS covariance
  (`z_total_raw`, from `cal_variance_raw`, the same propagation with the uncorrected `C`); total
  with the `n / n_eff` inflation (`z_total`). The difference between the last two is the effect of
  the serial-correlation correction.
- **Inflation factor.** For each of `z`, `z_total_raw`, and `z_total`, the ±1 SD and ±2 SD coverage
  with day-bootstrap intervals, and the factor `k` by which the
  variance would have to be multiplied for the observed coverage to be nominal:
  `k₆₈ = q₀.₆₈₃(|z|)²` and `k₉₅ = (q₀.₉₅₄(|z|) / 2)²`, where `q_p` is the empirical quantile, with
  day-bootstrap intervals; and the mean and SD of `z` and `z_total`, which are 0 and 1 for a
  correct, unbiased variance.
- **QQ plot.** `z_total` against standard-normal quantiles, showing whether the shortfall is a
  scale (a straight line steeper than 1:1) or heavy tails (curvature at the ends).
- **Shared hourly offset (diagnostic only).** The candidate the model omits is a calibration error
  common to every sensor in the same hour — humidity or aerosol composition the correction does
  not capture. For each kriged hour, `r_i = pm25_calibrated − obs` over the hour's kriging-input
  sensors with `fit_status` `fitted`, `obs` being the unflagged reading at the hour of the sensor's
  `ref_site_id`; the hour's offset is `s_H = mean r_i`. Its variance across hours, less the part
  the hours' independent residuals contribute, `σ²_shared = var_H(s_H) − mean_H(var_i(r_i) /
  n_H)` (floored at 0, over hours with at least two such sensors), is reported, with the coverage
  `total_variance + σ²_shared` would give — the kriging weights sum to 1, so a common offset passes
  to the monitor whole. It is a diagnostic: the residuals are computed against the same monitors
  the coverage is scored at, so it is not added to the reported total.
- **Shared offset scored out of sample.** The in-sample figure estimates `σ²_shared` and scores
  coverage on the same matchups. The split-day check removes that overlap: `σ²_shared` estimated
  from the residuals of hours on odd UTC days of the month scores the coverage of the matchups on
  even days, and the reverse. Each direction reports its `σ²_shared`, its hour count, and its ±1 SD
  and ±2 SD coverage with day-bootstrap intervals over the scored days. Neighbouring days still
  share weather regimes and the residuals still use the scored monitors on other days, so the
  check removes the reuse of matchups, not every dependence.
- **Coverage by group** comes from the metric table's splits: predicted level, day, spike hour,
  and spike day.

### Caveats stated with the totals

- The variogram's nugget absorbs part of the sensors' measurement scatter, so the kriging variance
  and the residual term can partly count the same noise; total coverage above nominal is the
  symptom.
- The residual term includes representativeness — the difference between a sensor's location and
  its monitor's — which overlaps the spatial variability the kriging variance already describes.
- Variogram uncertainty is not in either term.
- Residuals of different sensors in the same hour are treated as independent; a weather-driven
  error common to many sensors at once (e.g. humidity the Barkjohn correction under-corrects) is
  not modeled, and neither `ρ₁` nor the pooled covariance captures it.

## Residual decomposition

The calibration residual variance is split, for diagnosis, into three parts. Each is expressed in
calibrated units (µg/m³)², per fit, and summarised across the fits in use, pair-weighted:

- **(a) Detector noise.** For a PurpleAir snapshot that hourly aggregation uses (no QC flags,
  non-null corrected value) with channels `A` and `B`, `(A − B)² /
  4` is the variance of the channel mean (each channel's variance estimated as `(A − B)² / 2`).
  The Barkjohn correction multiplies the channel mean by 0.524 (`CORRECTION_SLOPE`) and the fit by
  `b`, so the term in calibrated units is `(0.524 b)² (A − B)² / 4`; for an hour of `k` snapshots
  it is the mean over snapshots divided by `k`. Averaged over the fit's pairs.
- **(b) Reference measurement error.** The hourly precision (1σ) of the monitors' federal
  equivalent methods, which AirNow does not identify per site, so it is reported at two published
  bounds: 0.5 µg/m³ (Teledyne T640, specified 1-hour precision) and 1.8 µg/m³ (Met One BAM-1020,
  median hourly 1σ noise across units tested; specified below 2.4 µg/m³). The regulatory goal for PM2.5 methods — a 10% coefficient of variation (40 CFR Part
  58, Appendix A, § 2.3.1.1) — is cited beside them for scale.
- **(c) Remainder** `= σ̂² − (a) − (b)`: representativeness (distance, siting, micro-environment)
  and model error (regression dilution, humidity nonlinearity beyond Barkjohn). A negative
  remainder is reported as 0 and counted.

Shares of (a), (b), (c) are reported overall and for fitted and pooled fits separately, at both
reference bounds. The decomposition is diagnostic only: the uncertainty calculation uses `σ̂²`
whole, which already contains all three.

## Variogram sensitivity

For each kriged hour, the monitors are also kriged with the candidate variogram fusion did not
select (spherical ↔ exponential, fitted to the same points with fusion's settings). The RMS
difference between the two candidates' predictions at the monitors is reported as a magnitude for
variogram-fit uncertainty in the error budget; it is not added to the total. When the alternative
candidate is not eligible (FUS-VAR-003), the hour's sensitivity is null.

## Error budget

A table of every error source the pipeline knows of, in the order the data meets them:

| Column | Content |
|---|---|
| `source` | the error source |
| `stage` | where it enters: ingest (PurpleAir), ingest (AirNow), calibration, fusion, evaluation |
| `type` | `random` or `systematic-shared` |
| `handling` | `quantified-included`, `quantified-diagnostic`, or `listed-not-modeled` |
| `magnitude` | a number with units from this run where quantified, else the published value or "not quantified" |

Rows, at least:

| Source | Stage | Type | Handling |
|---|---|---|---|
| PurpleAir detector noise (A/B disagreement) | ingest (PurpleAir) | random | quantified-diagnostic |
| Barkjohn correction coefficients | ingest (PurpleAir) | systematic-shared | listed-not-modeled |
| AirNow reference measurement error | ingest (AirNow) | random | quantified-diagnostic |
| AirNow preliminary data (revisions) and `Value` vs `RawConcentration`, flatline flags | ingest (AirNow) | systematic-shared | listed-not-modeled (flag rate reported) |
| Per-sensor calibration fit parameters | calibration | random | quantified-included |
| Pooled calibration fit parameters | calibration | systematic-shared | quantified-included |
| Calibration residual (representativeness, siting, model error) | calibration | random | quantified-included |
| Regression dilution (slopes below 1) | calibration | systematic-shared | listed-not-modeled (slope distribution reported) |
| Kriging interpolation error | fusion | random | quantified-included |
| Variogram fit (binning, range, model choice) | fusion | systematic-shared | quantified-diagnostic |
| Monitors as calibration targets for nearby sensors | evaluation | systematic-shared | quantified-diagnostic |

It is written as CSV and rendered as a figure-ready table.

## Figures

Every figure is matplotlib, saved at 300 dpi, sized to read at 3.3 in (one column) or 6.5 in (full
width), with every axis labelled with units and nothing clipped at the canvas edge; the data behind
each is written as a CSV of the same name.

| Figure | Content |
|---|---|
| `timeseries` | per-hour bias and RMSE over the window, coloured by date on a sequential colormap, with the per-hour spread of monitor errors (interquartile band) |
| `scatter` | surface vs `obs`, all hours, with the 1:1 line and `r2` |
| `coverage` | ±1 SD and ±2 SD coverage with kriging SD and with total SD, against the nominal 68.3% and 95.4% |
| `bias_vs_pooled` | per-hour bias against the hour's `pooled_share`, with a least-squares line |
| `decomposition` | stacked bar of (a), (b), (c) shares, fitted and pooled, at both reference bounds |
| `mc_vs_analytic` | Monte Carlo against analytic `cal_variance`, with the 1:1 line |
| `error_budget` | the error budget as a table |
| `lomo_rmse` | per monitor, the RMSE of the stored, control, and without-monitor errors over its LOMO rows, side by side |
| `baselines_rmse` | RMSE and bias of kriging and each baseline (unflagged, overall) with their intervals |
| `reliability` | mean `obs` per `surface` decile with its interval, against the 1:1 line, bins labelled with their counts |
| `qq_ztotal` | `z_total` quantiles against standard-normal quantiles, with the 1:1 line |
| `maps_pm25`, `maps_sd` | the same four hours, estimate and standard deviation, each on its fixed scale (§ Maps) |
| `maps_pm25_10` | the same four hours' estimate on a 0–10 µg/m³ scale |

Accuracy figures use the `unflagged` subset. `figures.md` lists every figure written, numbered in
the order of the table above, each with a one-sentence caption stating what it shows and the
subset and units.

**Maps.** Four hours are chosen from the window's kriged hours whose variogram class is
`regular` (from all kriged hours when none is) — on four different days, one from each quartile of
the hour's mean monitor `obs`, relaxing the distinct days when too few qualify and taking every
candidate hour when there are fewer than four — and drawn as small multiples through the maps
component, sensors and monitors overlaid, with a shared colorbar; the same four hours are drawn
again as standard deviation on its own fixed scale, and again as the estimate with `pm25_max = 10`
so clean-air hours use more of the colour range. Each figure keeps one fixed scale across its
panels. A pure-nugget hour is a flat surface, so it says nothing about spatial structure and is
not shown. The report states the window's maximum surface value and maximum SD against the maps'
fixed limits, and the fraction of cells above each.

## Report

`evaluate(archive_uri, start, end, out_dir, settings) -> EvaluationSummary` writes into `out_dir`:

```
matchups.csv            one row per matchup
metrics.csv             subset × split × group, every metric
availability.csv        per-monitor counts; gaps and non-kriged hours in summary.md
fit_uncertainty.csv     per (site_id, fit_as_of): status, n, n_eff, ρ₁, Var(a), Var(b), Cov(a,b),
                        corrected and uncorrected; σ̂²; stored vs rebuilt n_pairs, slope, intercept
montecarlo.csv          per sampled matchup: analytic, Monte Carlo, Monte Carlo (pooled independent)
lomo.csv                per (LOMO hour, monitor): stored, control, and without-monitor errors
lomo_by_monitor.csv     per monitor: LOMO rows, median d
baselines.csv           method × subset × split × group: accuracy metrics and intervals
baseline_paired.csv     per baseline and group: mean |kriging error| − |baseline error|, interval
decomposition.csv       (a), (b), (c) and shares, by fit status and reference bound
variogram_sensitivity.csv
variogram.csv           variogram classes, models, range percentiles
coverage_diagnostics.csv  per z kind: coverages, inflation factors, mean and SD; shared offset,
                        in sample and split by odd and even days
anomaly.csv             per method: common-offset and anomaly shares of MSE, anomaly and
                        hourly-mean correlations, anomaly RMSE, with intervals
conditional_bias.csv    reliability and reverse slopes with intervals
spike_points.csv        each spike point
spikes.csv              per (affected hour, monitor): control and without-spike errors
spike_tiers.csv         per spike-value tier: points, hours, hourly bias with and without the tier
pooling.csv             the fixed-effects coefficients; the pooled-share distribution and the
                        raw bias difference between low and high pooled share
per_monitor.csv
lag.csv                 per lag: pairs, correlation, RMSE; RMSE difference from lag 0
error_budget.csv
<figure>.png, <figure>.csv
figures.md              numbered figure captions
summary.md              plain answers, key numbers, and caveats
run.json                provenance (below)
```

`run.json` records the window, the evaluation and map settings, the seed, the archive URI, the
package version, and the provenance a reader needs to reproduce or trust the numbers:

- `git_commit` (HEAD), `git_dirty` (whether the working tree differs from HEAD, untracked files
  included), and, when dirty, `git_diff_sha256` — the SHA-256 of `git diff HEAD` — so a dirty run
  is identifiable; all null when the code is not running from a git checkout;
- `matchups_sha256`, the SHA-256 of `matchups.csv` as written;
- for the AirNow observation objects the run read (through the observation store's
  `partition_objects`): `airnow_objects` (their count), `airnow_last_modified` (the latest object
  modification, the latest time any AirNow value the run scored against could have been revised;
  null when the archive does not report it), and `airnow_manifest_sha256` (SHA-256 over the
  objects' sorted `key`, `etag`, `size`, and `last_modified`, null fields omitted — on a local
  archive it covers key and size only).

It writes nothing to the archive and returns the headline numbers. `aqdt evaluate --start --end
--out DIR [--mc-hours N] [--mc-draws N] [--lomo-hours N] [--seed N]` runs it from the command line (the pipeline
segment owns the CLI; this command is on-demand, has no routine window and no workflow).

**Summary.** `summary.md` opens with **plain answers** — one short paragraph each, stating the
number and its interval and a verdict from the rules in the sections above:

1. Where does kriging's skill come from — how much of its error is the hour's common offset and
   how much the within-hour anomaly, and how well does it place the within-hour pattern against
   IDW, the random forest, and the hourly mean?
2. What does the pooled fraction near a monitor do to the bias: how much leverage the pooled share
   has, the raw and within-day differences, their signs, and the effect per 10 percentage points?
3. What variance inflation would make coverage nominal, how much do the `n / n_eff` correction
   and the shared hourly offset change it, and what does the offset give when estimated on one
   half of the days and scored on the other?
4. Does kriging beat the hourly-mean, IDW, and random-forest baselines, and by how much (the
   paired differences)?
5. How much does leave one monitor out worsen the error at the monitors, and does the effect grow
   with the number of sensors fitted to the monitor?
6. Does the level-dependent bias survive when split by predicted level, at what size against the
   observed-level split (and is the surface over-smoothed or over-dispersed)?
7. How much of the affected days' error do the spikes account for, and which tier of spike value
   carries it?

It then states: hours and days available and the gaps; headline metrics (`unflagged`) and the
`all` equivalents, with the bias and RMSE intervals of `metrics.csv`'s overall rows; the flag rate with both denominators; coverage three ways; the
calibration-variance formula; the Monte Carlo to analytic ratio; the decomposition shares; the
conditional-bias, spike, pooling, variogram, and lag results; the maps' clipping; the LOMO and
baseline comparisons; and the caveats — AirNow data are preliminary, sample sizes per split,
weather differs between days, and the monitors are calibration targets for nearby sensors, so the
check is not fully independent of the surface.

Settings (`EvaluationSettings`, Pydantic settings from the environment): `AQDT_EVAL_MC_HOURS`
(default 24), `AQDT_EVAL_MC_DRAWS` (default 500), `AQDT_EVAL_LOMO_HOURS` (default 6),
`AQDT_EVAL_SEED` (default 0); the CLI options override them. `mc_hours` and `mc_draws` below 1,
and `lomo_hours` or the seed below 0, fail validation naming the setting; `lomo_hours = 0` skips
LOMO.

## Leave one monitor out

Every monitor scored is also a calibration reference for the sensors near it, so the surface has
seen each monitor indirectly, through the fits. Leave one monitor out (LOMO) measures how much that
dependence flatters the score at the monitor.

On `lomo_hours` hours (default 6), chosen with the seed from the kriged hours with
`inputs_reproduced` in the same way as the Monte Carlo sample (tertiles of the hour's mean monitor
`obs`, spread across days; every eligible hour when fewer are eligible), and for each monitor `X`
with a matchup in the hour:

1. For each `fit_as_of` used by the hour's calibrated sensor hours, read the PurpleAir and AirNow
   observations over `[fit_as_of − window_days, fit_as_of)` and aggregate them with
   `aggregate_hourly`, as `fit_calibrations` does, with the calibration settings in the
   environment.
2. **Control.** Refit with calibration's `fit_sensors` on all the monitor observations.
3. **Without `X`.** Refit with `fit_sensors` on the monitor observations with `X`'s removed, so
   `X` is neither a reference for any sensor nor in the pooled fit's pairs; sensors that were
   fitted against `X` match their next-nearest monitor within range, or fall to the pooled fit.
4. For each refit, recalibrate the hour's calibrated sensor hours: each row's `pm25_calibrated` is
   recomputed from the refit fit of its own `(site_id, fit_as_of)` as calibration applies a fit
   (`max(0, intercept + slope × pm25_corrected_mean)`, null when the refit is `uncalibrated`),
   and its `fit_status` becomes the refit's.
5. Build the hour's kriging points with fusion's `hour_points`, select the variogram with fusion's
   `select_variogram` (FUS-VAR), and predict at `X`'s coordinates with the selected variogram,
   clamping the estimate at 0 as fusion stores it (FUS-PRED-003).

The control isolates the monitor's role: the control and the without-`X` prediction share the
point prediction, the variogram refit, and the archive's current state, and differ only in
whether `X` calibrated anything. The stored surface's error at `X` is reported beside both. A
refit that leaves the hour with fewer than fusion's `min_points` points, or with no eligible
variogram, gives a null prediction for that refit; a sensor hour whose `(site_id, fit_as_of)` has
no refit is treated as `uncalibrated`.

`lomo.csv` holds one row per (hour, `X`): `hour`, `site_id`, `obs`, `flagged`, `stored_error`,
`control_error`, `lomo_error` (each `prediction − obs`), `n_fitted_to_x` (the control's `fitted`
sensors referencing `X`), `n_to_pooled`, `n_to_other_monitor`, and `n_uncalibrated` (what those
`n_fitted_to_x` sensors became in the without-`X` refit: `pooled`, `fitted` against another
monitor, or `uncalibrated`), `n_changed` (sensors whose refit status or reference differs between
control and without-`X`), and the matchup's `nearest_sensor_km` and `pooled_share`.

Over the unflagged rows, with `d = |lomo_error| − |control_error|` (positive when removing `X`
made the prediction at `X` worse), the report gives:

- the RMSE of each error column;
- a **sanity check** on the control: the median and maximum of `|control_error − stored_error|`.
  Both should be near zero; a large value means the archive has moved since the surface was
  written (AirNow revisions after the stored fit, a re-applied calibration) or the refitted
  variogram differs from the stored one, and the summary says which rows exceed 0.5 µg/m³;
- the mean and median of `d`, each with a 95% interval from a bootstrap that resamples the LOMO
  hours, and the share of rows with `d > 0`;
- `d` per monitor (`lomo_by_monitor.csv`: rows, median `d`, mean `n_fitted_to_x`);
- `d` by `n_fitted_to_x` band — `0`, `1–5`, `>5` — with median, mean, and hour-bootstrap intervals,
  and a dose-response statement: the dependence the check measures predicts `d ≈ 0` at monitors
  no sensor is fitted to and growing `d` with the count, and the summary says whether the medians
  rise across the bands and whether the intervals of the lowest and highest bands with rows
  separate, naming any band with no rows (a monitor with no sensor fitted to it in an hour leaves
  the `0` band empty).

The median `d` fills the error budget's "monitors as calibration targets" magnitude.

## Baselines

Simple predictors are scored at the same matchups, so kriging's skill reads against something:

- **Hourly mean and median** (zero skill in space). The mean, and the median, of the hour's
  kriging-point values, predicted at every monitor alike. A spatial method that does not beat
  these adds nothing beyond the hour's level.
- **Inverse-distance weighting.** Power 2 over the hour's kriging points at the monitor's
  coordinates; a point at distance 0 gives that point's value (the mean, if several).
- **Random forest.** scikit-learn's `RandomForestRegressor` with `random_state = seed`, 200
  trees, and a minimum of 5 samples per leaf, trained once on the kriging points of every kriged
  hour in the window, with features `x`, `y`, UTC hour of day, and the hour's median point value,
  and the point value as target; it predicts each matchup from the monitor's `x`, `y`, the hour
  of day, and the hour's median point value.

No baseline sees a monitor value: each is built from the kriging points alone, and the forest is
trained on point values only. The forest is trained on the same hours it predicts, so its
comparison is a spatial hold-out, not a hold-out in time; the summary says both.

The predictions are columns `hour_mean`, `hour_median`, `idw`, and `rf` of `matchups.csv`.
`baselines.csv` holds, for `kriging` (the stored surface) and each baseline, the accuracy metrics
(`n`, `bias`, `mae`, `rmse`, `r2`, `pearson_r`, `pearson_r2`) and their day-bootstrap intervals,
for the `unflagged` and `all` subsets, overall and split by observed level and predicted level
(the predicted level always that of the kriging `surface`, so every method is scored on the same
groups).

**Paired comparison.** For each baseline `B` and each matchup, `|kriging error| − |B error|`;
`baseline_paired.csv` gives its mean with a day-bootstrap interval, over the unflagged matchups,
overall and per observed-level and predicted-level group. Negative means kriging is closer to the
monitor; kriging "beats" `B` only when the interval lies below 0.

## Common offset and within-hour anomaly

A surface can score well at the monitors by getting each hour's overall level right (time) or by
placing the differences between monitors within the hour right (space). The two are separated on
the unflagged matchups of hours with at least 5 of them — fewer leaves the hour's mean too noisy to
stand for its level. For each method `m` (`kriging`, `idw`, `rf`, `hour_mean`) with prediction
`p` and error `e = p − obs`:

- **Common offset** `ē_H`, the mean of `e` over the hour's matchups, and **anomaly**
  `a = e − ē_H`. Within each hour the anomalies sum to 0, so the MSE splits exactly:
  `mean(e²) = mean(ē_H²) + mean(a²)` over the matchups. The offset share is
  `Σ ē_H² / Σ e²`, the anomaly share its complement.
- **Anomaly correlation**: Pearson correlation of `p − p̄_H` with `obs − ōbs_H` over the matchups
  — how well the method places the within-hour pattern. Null for `hour_mean`, whose prediction is
  the same at every monitor (its anomaly is 0). **Anomaly RMSE** `√mean(a²)`; for `hour_mean` it
  is the monitors' own within-hour spread, the floor a spatial method must get under.
- **Hourly-mean correlation**: Pearson correlation over hours of `p̄_H` with `ōbs_H` — how well
  the method tracks the hour's level.
- **Against the hourly mean**: the reduction in each MSE component from `hour_mean` to `m`,
  `mean(ē_H²)` and `mean(a²)`, each with an interval.
- **Variance explained.** The monitors' own variance splits the same way, into the across-hour
  part `V_across` (the spread of the hourly monitor means) and the within-hour part `V_within`
  (the spread of the monitors about their hour's mean). A method explains `1 − mean(ē_H²) /
  V_across` of the first and `1 − mean(a²) / V_within` of the second. The hourly mean explains
  none of the within-hour variance by construction, so its across-hour figure is what tracking the
  hour's level alone achieves.

`anomaly.csv` holds per method the matchup and hour counts, the two shares, the anomaly
correlation, anomaly RMSE, hourly-mean correlation, the two reductions, and the two
variance-explained fractions, each with its day-bootstrap interval, and the two parts of the
monitors' variance with their shares. The summary puts the variance-explained table beside its
verdict: kriging's skill comes mainly from the hour's level when the variance it explains across
hours (`V_across` times the fraction) exceeds the variance it explains within the hour, and mainly
from the within-hour pattern otherwise. The comparison is in variance, not in fractions, because
the two parts of the monitors' variance differ in size.

## Spikes

A single sensor can read far above its neighbours with both channels agreeing, pass every QC check,
and set the surface's maximum. Evaluation finds such points with a rule applied to each kriged
hour's kriging points (values `z`, after co-located sensors are merged), names no sensor in
advance, and measures what they cost — a diagnostic only; the stored surfaces and the pipeline are
untouched.

- **Spike point.** A point whose `z` exceeds the hour's median point value by more than
  `5 × 1.4826 × MAD` (MAD the median absolute deviation of the hour's point values, 1.4826 scaling
  it to a normal SD) **and** exceeds 20 µg/m³. The absolute floor keeps clean-air hours, whose MAD
  can be a fraction of a µg/m³, from flagging ordinary points.
- **Affected hour.** A kriged hour with at least one spike point; an **affected day** is a UTC day
  holding an affected hour. They form the `spike_hour` and `spike_day` splits.
- **Re-krige.** For each affected hour, the monitors are predicted twice with `select_variogram`
  and the clamp at 0, as leave one monitor out does: a control on all the hour's points, and once
  with the spike points removed. `Δ = without − control` at each monitor isolates the spikes' effect
  from the refit variogram.

`spike_points.csv` lists each spike point: `hour`, the point's sensor `site_id`s, `z`, the hour's
median and MAD. `spikes.csv` holds one row per (affected hour, monitor): `obs`, `flagged`,
`stored_error`, `control_error`, `without_error`, `delta`. The summary reports, over unflagged
matchups, each affected hour's bias and RMSE with and without the spikes (stored error, and stored
error + Δ); the RMSE of each affected day both ways; and the headline metrics with and without the
affected hours.

**Tiers.** Spike values span an order of magnitude, and a 25 µg/m³ point and a 180 µg/m³ point
are different problems. Spike points are grouped by value into `20–50` (`z ≤ 50`), `50–100`
(`50 < z ≤ 100`), and `>100` µg/m³. For each tier, every affected hour holding a point of that tier
is re-kriged with only that tier's points removed, against the same control, giving `Δ_tier` at
each monitor. `spike_tiers.csv` gives per tier the points, the hours, the unflagged matchups in
those hours, their mean bias with the stored errors and with `stored + Δ_tier`, and the mean
change in bias with a day-bootstrap interval.

## Pooling within the hour

`bias_vs_pooled` relates hourly bias to the hour's pooled share, but pooled share moves with the
day's weather, so the two can be correlated without one causing the other. The matchup-level test
holds the day fixed: OLS of `error` on `pooled_share_10km` with day fixed effects, and again with
day and hour-of-day fixed effects, over the unflagged matchups with a non-null `pooled_share_10km`.

A coefficient is only as informative as the spread of the variable behind it. Over the same
matchups the report gives the distribution of `pooled_share_10km` (mean, quartiles, the share
exactly 0) and the raw difference in mean error between high-share matchups (above the median) and
low-share ones (at or below it), with a day-bootstrap interval. The summary states the raw and
within-day signs and whether they agree, each coefficient per 10 percentage points of pooled share,
and the within-day effect across the interquartile range. Leverage is low when the 75th
percentile is below 10%: most monitors then see almost no pooled sensors nearby, and no
coefficient on the share can move much of the bias. The regression measures an association within
the day; it is not described as a cause.

`pooling.csv` is long: one row per `quantity` — each model's coefficient (µg/m³ per unit pooled
fraction) with its day-cluster bootstrap interval (resampling days, refitting), the raw high − low
difference with its interval, and the distribution's mean, quartiles, and zero share — with
`estimate`, `ci_low`, `ci_high`, and `n`.

## Per monitor

`per_monitor.csv` gives, per monitor over its unflagged matchups, `n`, `bias`, `mae`, `rmse`,
`pearson_r`, the four coverages, and, over all its matchups, the flagged share; with the count of
calibration-target sensors and their median distance (from the availability report) and the mean
`nearest_sensor_km`.

## Variograms

A kriged hour's variogram is `pure_nugget` when its partial sill `sill − nugget` is below 1% of
`sill` (no spatial structure: the surface is the hour's weighted mean everywhere and the SD is flat
at the nugget), `short_range` when it is exponential with `range_m` below 1000 m (structure finer
than the grid), and `regular` otherwise. `variogram.csv` gives the count and share of each class,
the count of each `variogram_model`, and the 5th, 25th, 50th, 75th, and 95th percentile of
`range_m`; the metric table's `variogram` split scores each class.

## Timing alignment

Both sides of a matchup are labelled by the start of their hour. AirNow's hour `H` is the
monitor's average over `[H, H + 1 h)` (hour-beginning). The surface's hour `H` is kriged from each
sensor's PurpleAir snapshots with `observed_at` in `[H, H + 1 h)`; with hourly collection at
half past the hour, that is in practice one near-instantaneous reading near `H:30`, the middle of
the monitor's averaging hour. Matchups pair the two at the same `H`, with no shift.

An hour misaligned between the surface and AirNow would show as a better match at a lag.
`lag.csv` pairs each matchup's `surface` with the same monitor's unflagged `obs` at `hour + lag`
for lags −1, 0, and +1 h (from the AirNow observations the run reads, extended by an hour each
side), giving `n`, `pearson_r`, and `rmse` per lag, and, for lags ±1, the RMSE at the lag minus
the RMSE at lag 0 over the matchups with an `obs` at both, with a day-bootstrap interval. The hour
labels follow the convention above by construction, so a lag that fits better (its difference
interval wholly below 0) is not a labelling error; the summary gives its size, also as a share of
the lag-0 RMSE, so a reader can judge whether it matters. The slow diurnal rise and fall of PM2.5
makes neighbouring hours similar, and the instruments' response times differ, so small
differences at ±1 h are expected.

## Flag rates

The share of flagged AirNow readings is stated with both denominators, each named: flagged
observations over all AirNow observations in kriged hours (the error budget's flag rate), and
flagged matchups over all matchups. They differ because observations at monitors outside the grid
or with no value form no matchup.

## Package Layout

```
src/aqdt/evaluation/
  schemas.py      # EvaluationSettings, EvaluationSummary
  matchups.py     # cell_index: the raster cell containing a monitor
  metrics.py      # metric functions, subsets, splits
  calse.py        # fit covariance, n_eff; rebuilding the fits in use
  totvar.py       # OK weights, analytic cal_variance
  montecarlo.py   # Monte Carlo propagation, hour sampling
  decompose.py    # residual decomposition
  sensitivity.py  # variogram sensitivity
  budget.py       # error budget
  figures.py      # every report figure but the maps
  report.py       # summary.md, run.json
  run.py          # evaluate: reads, per-hour scoring, maps, the report
  lomo.py         # leave one monitor out
  baselines.py    # hourly mean and median, IDW, and random-forest baselines; paired comparison
  bootstrap.py    # day and hour block bootstraps, metric intervals
  diagnostics.py  # conditional bias, coverage diagnostics, pooling, per monitor, variograms, lag
  spikes.py       # spike rule, re-krige, value tiers
  anomaly.py      # common offset and within-hour anomaly
tests/evaluation/
```

Dependencies: `matplotlib` and `scikit-learn`, in the `analysis` dependency group — scheduled runs
never evaluate, so their environments do not install them.

## Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| What is scored | The stored raster's cell at each monitor | Evaluation describes the product consumers use; the cell value is what QGIS or rioxarray returns for that location. |
| Evaluation's outputs | A report directory outside the archive | An assessment of the pipeline's products, regenerated on demand as the archive grows; it is not served from PostGIS and nothing downstream reads it. |
| Reference value | AirNow `pm25_raw` | The same value calibration fits against, so the check is on the scale the surface claims to be on. |
| Flagged monitor hours | Excluded from the headline, reported in the `all` subset | A flagged value is untrusted by the project's own QC, but hiding it would hide how much the exclusion matters. |
| Fit covariance source | Rebuilt from the archive with calibration's own pair construction | Keeps evaluation read-only and the fits product unchanged while the uncertainty model is being validated. |
| Serial correlation | Covariance inflated by `n / n_eff` from the lag-1 autocorrelation | Consecutive hourly residuals are strongly correlated; textbook OLS errors would understate the parameter uncertainty several-fold. |
| Pooled-fit error | Shared covariance across pooled sensors | One line calibrates every pooled sensor; its error moves them together, and kriging averages independent errors away but not shared ones. |
| Residual term | The fit's full residual variance, nothing added | The residual already contains detector noise, reference error, and representativeness; adding the decomposed parts again would count them twice. |
| Analytic propagation | `λᵀ M Σ_s Mᵀ λ` with the hour's recorded variogram | Kriging is linear in the data for a fixed variogram, so the propagation is exact apart from the clamp, and cheap enough for every hour. |
| Monte Carlo | A seeded, stratified sample of hours, PyKrige predictor | An independent code path checks the analytic algebra, including the shared covariance and the clamp, at a cost bounded by the sample. |
| Reference precision | Two published hourly bounds (T640, BAM-1020) | AirNow does not report the method per site; reporting both bounds shows how much the decomposition depends on it. |
| Variogram uncertainty | Diagnostic magnitude from the alternative candidate | Model choice is the largest visible variogram effect per hour; adding it to the total would need a variogram posterior this design does not have. |
| LOMO comparison | A re-kriged control with the full fits, beside the stored surface | The stored cell and a point prediction from a refitted variogram differ for reasons unrelated to calibration; only the control holds everything but the monitor's role fixed. |
| Baselines | Hourly mean and median, IDW (power 2), and a random forest on location, hour, and the hour's level | The hourly mean and median are the zero-skill floor in space, IDW the simplest spatial interpolator, and the forest a common learned one; all use only the sensor points, as kriging does, so the comparison is on equal inputs. |
| Intervals | Block bootstrap over UTC days, 1000 resamples, seeded | Matchups within a day share weather and calibration error; resampling days keeps that dependence, so intervals reflect the number of independent days rather than the number of matchups. |
| Level-dependent bias | Judged on predicted level and a reliability slope, with the observed-level split beside them | Splitting by the observed value conditions on the monitor's noise and manufactures the pattern for any predictor; the predicted value does not contain that noise. |
| Spike detection | A robust rule on each hour's points (median + 5 × 1.4826 × MAD, and above 20 µg/m³) | A rule applied to the data finds every such point, including ones not yet noticed; the robust scale ignores the spike itself, and the absolute floor keeps quiet hours from flagging ordinary scatter. |
| Shared hourly offset | Diagnosed from same-hour calibration residuals, not added to the total | It is estimated against the monitors coverage is scored at, so adding it would fit the uncertainty to the test; reported beside the total, it shows how much of the shortfall it could explain. |
| Pooling and bias | Matchup-level regression on pooled share within 10 km, day fixed effects, read beside the share's distribution and the raw difference | Hourly pooled share moves with the day's weather; holding the day fixed separates the sensors' calibration route from what the day was like, and the distribution shows how much of the bias any coefficient on the share could carry. |
| Space and time | Exact split of MSE into the hour's common offset and the within-hour anomaly, over hours with at least 5 matchups | A surface interpolated from sensors can be right about the hour's level and wrong about where it is higher or lower, or the reverse; the split says which one its skill rests on, and the hourly mean baseline anchors the anomaly floor. |
| Shared-offset check | Estimated on odd UTC days, scored on even days, and the reverse | Scoring on matchups the estimate never used removes the reuse of the same matchups; alternating days keeps both halves spread across the window's weather. |
| Figure format | matplotlib, 300 dpi, CSV beside each | Print-ready for papers and reports, and every number in a figure is recoverable without rerunning. |

## Open Questions & Future Decisions

1. Storing the fit covariance on calibration's `fits` rows and writing the total variance as a
   third band of each surface raster, once the Monte Carlo confirms the analytic total — the
   surface would then carry its full uncertainty for every consumer.
2. Scoring against FAIRMODE model-quality objectives, and leave-one-out cross-validation over the
   sensors.
3. A variogram-uncertainty term in the total (e.g. by bootstrap of the points), if variogram
   sensitivity proves comparable to the kriging variance.
4. A shared hourly calibration-error term in the total, estimated without the scored monitors
   (e.g. from monitors held out of the coverage check), if the coverage diagnostics show the
   shared offset explains the shortfall.

## References

- `docs/high-level-design.md` — Evaluation; Fusion is observational.
- `docs/intent/fusion/fusion-design.md` — the surface raster, `SurfaceFit`, kriging points.
- `docs/intent/calibration/calibration-design.md` — fits, pairs, `CalibratedHourly`.
- `docs/intent/maps/maps-design.md` — the map renderer.
- 40 CFR Part 58, Appendix A, § 2.3.1.1 — PM2.5 measurement uncertainty goals (10% CV precision,
  ±10% bias).
- Teledyne API, *Model T640 PM Mass Monitor* specification — 1-hour precision ±0.5 µg/m³.
- Met One Instruments, *BAM-1020 Detection Limit* technical bulletin — hourly 1σ noise specified
  below 2.4 µg/m³, median about 1.8 µg/m³ over units tested.
- Barkjohn, K. K., Gantt, B., and Clements, A. L. (2021) — the correction and its 0.524 slope.
- Kitanidis, P. K. (1997). *Introduction to Geostatistics* — ordinary-kriging system and weights.
- Bretherton, C. S. et al. (1999). The effective number of spatial degrees of freedom of a
  time-varying field. *Journal of Climate* 12 — effective sample size under lag-1 autocorrelation.
