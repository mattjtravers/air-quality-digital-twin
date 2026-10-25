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
   consumer would sample it; evaluation never re-kriges a surface to score it.
2. **Read-only.** Evaluation reads the archive and writes nothing to it. Its outputs are a report
   directory — tables, figures, a summary — regenerated on demand, not a pipeline product.
3. **Flagged rows are reported, not hidden.** Monitor hours with QC flags are excluded from the
   headline numbers and included in a parallel set, so the effect of the exclusion is visible
   (HLD tenet: flag, never drop).
4. **Shared error is shared.** An error that is common to many sensors (the pooled fit) is
   propagated as a covariance, never as independent per-sensor noise.
5. **Deterministic.** Given the archive, the window, and the seed, every table and figure is
   identical.

## Inputs

All inputs come from the archive through the observation store's primitives (`read_partitioned`,
`read_observations`, `read_sites`) and the surface rasters by their `surface_uri`:

- **Surface fits** — fusion's `surface_fits` rows for the window; rows with `status = kriged`
  carry the variogram parameters and the raster's URI.
- **Surface rasters** — band 1 (`pm25`) and band 2 (`pm25_variance`) of each kriged hour.
- **AirNow observations** — `pm25_raw` and `qc_flags` per monitor hour.
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
| `cal_variance` | the calibration-uncertainty contribution at the monitor (§ Total uncertainty), (µg/m³)² |
| `total_variance`, `total_sd`, `z_total` | `variance + cal_variance`, its square root, `error / total_sd` |
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
| `r2` | `1 − Σ error² / Σ (obs − mean obs)²` — the surface as a predictor of the monitor; can be negative |
| `pearson_r` | Pearson correlation of `surface` and `obs` |
| `cov1_krig`, `cov2_krig` | fraction of matchups with `|z| ≤ 1`, `≤ 2`, among those with `sd > 0` |
| `cov1_total`, `cov2_total` | fraction with `|z_total| ≤ 1`, `≤ 2`, among those with `total_sd > 0` and `inputs_reproduced` |

`r2` and `pearson_r` are null for fewer than two matchups or zero variance. Gaussian errors with a
correct variance would give 68.3% and 95.4% coverage.

**Subsets.** Every metric table is computed twice: `unflagged` (matchups with `flagged` false —
the headline) and `all`.

**Splits.** Within each subset, metrics are reported overall and split by:

- **PM2.5 level** of `obs`: `<5`, `5–9` (5 ≤ obs ≤ 9), `>9` µg/m³;
- **UTC hour of day**, 0–23;
- **calendar day** (UTC date of `hour`);
- **pooled-share band**: tertiles of the hourly `pooled_share` across the window's kriged hours,
  each labelled with its edges; edges that coincide (many hours with the same share) are merged,
  so there may be fewer than three bands.

One long table holds them all: `subset`, `split`, `group`, then the metrics.

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
metric tables are empty, and the Monte Carlo, the maps, and every figure drawn from matchups
are skipped.

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
| Monitors as calibration targets for nearby sensors | evaluation | systematic-shared | listed-not-modeled |

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
| `maps_pm25`, `maps_sd` | the same four hours, estimate and standard deviation, each on its fixed scale (§ Maps) |

Accuracy figures use the `unflagged` subset.

**Maps.** Four hours are chosen from the window's kriged hours — on four different days, one from
each quartile of the hour's mean monitor `obs`, relaxing the distinct days when too few qualify
and taking every kriged hour when there are fewer than four — and drawn as small multiples through the maps
component, sensors and monitors overlaid, with a shared colorbar; the same four hours are drawn
again as standard deviation on its own fixed scale. The report states the window's
maximum surface value and maximum SD against the maps' fixed limits, and the fraction of cells
above each.

## Report

`evaluate(archive_uri, start, end, out_dir, settings) -> EvaluationSummary` writes into `out_dir`:

```
matchups.csv            one row per matchup
metrics.csv             subset × split × group, every metric
availability.csv        per-monitor counts; gaps and non-kriged hours in summary.md
fit_uncertainty.csv     per (site_id, fit_as_of): status, n, n_eff, ρ₁, Var(a), Var(b), Cov(a,b),
                        corrected and uncorrected; σ̂²; stored vs rebuilt n_pairs, slope, intercept
montecarlo.csv          per sampled matchup: analytic, Monte Carlo, Monte Carlo (pooled independent)
decomposition.csv       (a), (b), (c) and shares, by fit status and reference bound
variogram_sensitivity.csv
error_budget.csv
<figure>.png, <figure>.csv
summary.md              key numbers and caveats
run.json                window, settings, seed, archive URI, package version, git commit if known
```

It writes nothing to the archive and returns the headline numbers. `aqdt evaluate --start --end
--out DIR [--mc-hours N] [--mc-draws N] [--seed N]` runs it from the command line (the pipeline
segment owns the CLI; this command is on-demand, has no routine window and no workflow).

**Summary.** `summary.md` states: hours and days available and the gaps; headline metrics
(`unflagged`) and the `all` equivalents; coverage with kriging SD and with total SD; the Monte
Carlo to analytic ratio; the decomposition shares; the maps' clipping; and the caveats — AirNow
data are preliminary, sample sizes per split, weather differs between days, and the monitors are
calibration targets for nearby sensors, so the check is not fully independent of the surface.

Settings (`EvaluationSettings`, Pydantic settings from the environment): `AQDT_EVAL_MC_HOURS`
(default 24), `AQDT_EVAL_MC_DRAWS` (default 500), `AQDT_EVAL_SEED` (default 0); the CLI options
override them. Values below 1 (below 0 for the seed) fail validation naming the setting.

## Further checks

Two checks run after the report above, each in its own section of the report:

- **Leave one monitor out.** For one or two hours, each monitor `X` in turn is removed from the
  calibration references: the hour's fits are refit without `X` (calibration's `fit_sensors` on
  the monitor observations minus `X`), the hour's sensors are recalibrated with those fits, the
  hour is kriged as fusion would krige it, and the result is scored at `X`. Compared with the
  stored surface's error at `X`, this measures how much the monitor's role as a calibration target
  improves the surface's apparent accuracy there.
- **Baselines.** On the same matchups and metrics: inverse-distance weighting (power 2) of the
  hour's kriging points, and a random forest trained on the sensor points of all evaluated hours
  with features `x`, `y`, UTC hour of day, and the hour's median sensor value, scored at the
  monitors. One comparison table: kriging, IDW, random forest.

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
  further.py      # leave-one-monitor-out, baselines
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
| Figure format | matplotlib, 300 dpi, CSV beside each | Print-ready for papers and reports, and every number in a figure is recoverable without rerunning. |

## Open Questions & Future Decisions

1. Storing the fit covariance on calibration's `fits` rows and writing the total variance as a
   third band of each surface raster, once the Monte Carlo confirms the analytic total — the
   surface would then carry its full uncertainty for every consumer.
2. Scoring against FAIRMODE model-quality objectives, and leave-one-out cross-validation over the
   sensors.
3. A variogram-uncertainty term in the total (e.g. by bootstrap of the points), if variogram
   sensitivity proves comparable to the kriging variance.

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
