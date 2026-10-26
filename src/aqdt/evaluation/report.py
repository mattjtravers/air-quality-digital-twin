"""The report's prose and provenance: ``summary.md`` and ``run.json``."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pandas as pd

from aqdt.evaluation.diagnostics import clean_dirty_holds, smoothing_verdict


def _iso(value) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%dT%H")


def _num(value, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    if isinstance(value, int):
        return str(value)
    return f"{value:.{digits}f}"


def _pct(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "—"
    return f"{100 * value:.1f}%"


def _finite(value) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(value)


def _ci(value, lo, hi, digits: int = 2) -> str:
    """``value [lo, hi]``, or the value alone when the interval is not defined."""
    if not _finite(lo) or not _finite(hi):
        return _num(value, digits)
    return f"{_num(value, digits)} [{_num(lo, digits)}, {_num(hi, digits)}]"


def _metrics_row(label: str, m: dict) -> str:
    return (
        f"| {label} | {m['n']} | {_ci(m['bias'], m.get('bias_ci_low'), m.get('bias_ci_high'))} | "
        f"{_num(m['mae'])} | {_ci(m['rmse'], m.get('rmse_ci_low'), m.get('rmse_ci_high'))} | "
        f"{_num(m['r2'])} | {_num(m['pearson_r'])} | {_num(m.get('pearson_r2'))} | "
        f"{_pct(m['cov1_krig'])} | {_pct(m['cov2_krig'])} | {_pct(m['cov1_total'])} | "
        f"{_pct(m['cov2_total'])} |"
    )


_HEADER = (
    "| Subset | n | Bias [95% CI] | MAE | RMSE [95% CI] | R² skill | r | r² | ±1 SD krig | "
    "±2 SD krig | ±1 SD total | ±2 SD total |\n|---|---|---|---|---|---|---|---|---|---|---|---|"
)
R2_LABELS = (
    "R² skill is R² (skill, 1 − SSE/SS_obs): how much of the monitors' variance the surface "
    "explains, negative when the surface does worse than the monitors' own mean. r² is "
    "r² (squared correlation): how well the surface ranks the monitors, blind to bias and "
    "scale. They agree only when the regression of the monitors on the surface is the 1:1 line."
)
SPLIT_TITLES = {
    "level": "observed level (conditions on the monitor's value)",
    "pred_level": "predicted level (terciles of the surface)",
    "hour_mean_points": "hour level from the sensors (terciles of the hour's point mean)",
    "hour_mean_obs": "hour level from the monitors (observed-conditioned)",
    "pooled_band": "pooled band",
    "spike_hour": "spike hour",
    "spike_day": "spike day",
    "variogram": "variogram class",
    "monitor": "monitor",
    "day": "day",
    "hour_of_day": "hour of day",
}


def _split_table(metrics: pd.DataFrame, split: str) -> list[str]:
    rows = metrics[(metrics["subset"] == "unflagged") & (metrics["split"] == split)]
    lines = [_HEADER.replace("Subset", split.replace("_", " ").capitalize())]
    for row in rows.itertuples(index=False):
        lines.append(_metrics_row(str(row.group), row._asdict()))
    return lines


def _group(metrics: pd.DataFrame, split: str, which: str) -> dict | None:
    """The first or last group of an unflagged split (bands sort by their lower edge)."""
    rows = metrics[(metrics["subset"] == "unflagged") & (metrics["split"] == split)]
    if not len(rows):
        return None
    rows = rows.assign(_lo=rows["group"].map(_lower_edge)).sort_values("_lo")
    return rows.iloc[0 if which == "first" else -1].to_dict()


def _lower_edge(label: str) -> float:
    try:
        return float(str(label).strip("[(").split(",")[0])
    except ValueError:
        return {"<5": 0.0, "5–9": 5.0, ">9": 9.0}.get(str(label), math.nan)


def _flagged(matchups: pd.DataFrame) -> int:
    return int(matchups["flagged"].sum()) if len(matchups) else 0


def _hours(values) -> str:
    values = list(values)
    if not values:
        return "none"
    shown = ", ".join(_iso(v) for v in values[:30])
    return shown + (f" … ({len(values)} in all)" if len(values) > 30 else "")


# @spec EVAL-RUN-002, EVAL-AVAIL-001, EVAL-AVAIL-003, EVAL-AVAIL-004, EVAL-FIG-005
# @spec EVAL-SPLIT-005, EVAL-FLAG-001
def write_summary(path: Path, c: dict) -> None:
    lines = [
        "# Surface evaluation",
        "",
        f"Window `[{_iso(c['start'])}, {_iso(c['end'])})` UTC. Archive `{c['archive_uri']}`.",
        "",
    ]
    lines += _plain_answers(c)
    lines += ["## Availability", ""]
    if c["kriged_hours"] == 0:
        lines += ["No hour was kriged in the window: there is nothing to score.", ""]
    lines += [
        f"- Kriged hours: **{c['kriged_hours']}** over **{c['days']}** UTC days.",
        f"- Hours with no `surface_fits` row (gaps): {_hours(c['gaps'])}.",
        "- Hours not kriged: "
        + (
            ", ".join(f"{_iso(r.hour)} ({r.status})" for r in c["not_kriged"].itertuples())
            or "none"
        )
        + ".",
        f"- Hours whose kriging inputs could not be reproduced from the archive: "
        f"{_hours(c['not_reproduced'])}.",
        f"- Hours whose raster does not match its row's `surface_sha256`: "
        f"{_hours(c['sha_mismatch'])}.",
        f"- Matchups: {len(c['matchups'])} ({_flagged(c['matchups'])} on flagged monitor hours).",
        *_flag_lines(c["flag_rates"]),
        "",
        "Per monitor: `availability.csv`.",
        "",
        "## Accuracy and coverage",
        "",
        "Headline numbers use `unflagged` matchups (monitor hours without QC flags); `all` "
        "includes the flagged ones. Errors are surface − monitor, µg/m³. Coverage is the "
        "share of matchups within ±1 and ±2 standard deviations, with the kriging SD alone and "
        "with the total SD (kriging plus calibration); Gaussian errors with the right variance "
        "would give 68.3% and 95.4%. Intervals are 95% block-bootstrap intervals over UTC days "
        "(1000 resamples); they are omitted where a row spans a single day.",
        "",
        R2_LABELS,
        "",
        _HEADER,
        _metrics_row("unflagged", c["headline"]),
        _metrics_row("all", c["all"]),
        "",
    ]
    if len(c["matchups"]):
        for split in ("level", "pred_level", "hour_mean_points", "hour_mean_obs", "pooled_band",
                      "spike_hour", "spike_day", "variogram", "monitor", "day", "hour_of_day"):
            lines += [f"### By {SPLIT_TITLES[split]} (unflagged)", ""]
            lines += _split_table(c["metrics"], split) + [""]
    mc = c["montecarlo"]
    lines += ["## Calibration uncertainty", ""]
    fits = c["fit_table"]
    if len(fits):
        fitted, pooled = fits[fits["status"] == "fitted"], fits[fits["status"] == "pooled"]
        lines += [
            f"- Fits rebuilt: {len(fitted)} per-sensor, {len(pooled)} pooled. Median lag-1 "
            f"residual autocorrelation {_num(fits['rho1'].median())}; median `n/n_eff` "
            f"{_num((fits['n'] / fits['n_eff']).median(), 1)}.",
            f"- Rebuilt fits whose pair count differs from the stored fit: "
            f"{int((fits['n'] != fits['stored_n_pairs']).sum())}.",
        ]
    if len(mc):
        ratio = (mc["monte_carlo"] / mc["analytic"]).median()
        ratio_ind = (mc["monte_carlo_independent"] / mc["analytic"]).median()
        lines += [
            f"- Monte Carlo check ({mc['hour'].nunique()} hours, "
            f"{c['settings'].mc_draws} draws): median Monte Carlo / analytic calibration "
            f"variance **{_num(ratio)}**; treating the pooled fit's error as independent per "
            f"sensor gives {_num(ratio_ind)}.",
        ]
    elif c["kriged_hours"] == 0:
        lines += ["- Monte Carlo skipped: no kriged hour."]
    if len(c["matchups"]):
        m = c["matchups"]
        lines += [
            f"- Median calibration variance at monitors {_num(m['cal_variance'].median(), 3)} "
            f"(µg/m³)² against median kriging variance {_num(m['variance'].median(), 3)} (µg/m³)².",
        ]
    lines += [""]
    d = c["decomposition"]
    if len(d) and d["fits"].fillna(0).sum() > 0:
        lines += [
            "### Calibration residual decomposition (diagnostic only)",
            "",
            "| Fits | Reference bound | (a) detector | (b) reference | (c) remainder "
            "| clamped fits |",
            "|---|---|---|---|---|---|",
        ]
        for r in d.itertuples(index=False):
            lines.append(
                f"| {r.group} | {r.reference} | {_pct(r.share_a)} | {_pct(r.share_b)} | "
                f"{_pct(r.share_c)} | {_num(r.clamped, 0) if r.clamped == r.clamped else '—'} |"
            )
        lines += [""]
    lines += _diagnostic_sections(c)
    lines += _further(c)
    ms = c["map_settings"]
    lines += [
        "## Maps",
        "",
        f"Fixed colour limits: `pm25_max` = {_num(ms.pm25_max, 1)} µg/m³, `sd_max` = "
        f"{_num(ms.sd_max, 1)} µg/m³. Window maximum surface value {_num(c['max_pm25'])} µg/m³ "
        f"({_pct(c['frac_above_pm25'])} of cells above the limit); maximum SD "
        f"{_num(c['max_sd'])} µg/m³ ({_pct(c['frac_above_sd'])} of cells above). Cells above a "
        "limit are drawn in the top colour and the colorbar carries an arrow. The map hours are "
        "drawn from hours with a regular variogram; `maps_pm25_10` repeats the estimate on a "
        "0–10 µg/m³ scale. Figure captions: `figures.md`.",
        "",
    ]
    if c["kriged_hours"] == 0:
        lines += ["Maps skipped: no kriged hour.", ""]
    lines += [
        "## Caveats",
        "",
        "- AirNow data are preliminary: values are revised for days after the hour, and "
        "`Value` is not always the measured concentration (flatline flags mark some of it).",
        "- Sample sizes differ by split; each table gives the matchup count per group, and "
        "small groups are noisy.",
        "- The weather differs between days, so per-day and per-hour results mix model skill "
        "with how hard each day was to map.",
        "- The monitors are calibration targets for nearby sensors, so the check at a monitor "
        "is not fully independent of the surface there (`availability.csv`, "
        "`calibration_targets`).",
        "- The variogram nugget and the calibration residual can partly count the same "
        "measurement noise, so the total variance may overstate the uncertainty.",
        "- The calibration residual includes representativeness, which overlaps the spatial "
        "variability the kriging variance describes.",
        "- Variogram uncertainty is in neither term; `variogram_sensitivity.csv` gives its "
        "size at the monitors.",
        "- Residuals of different sensors in the same hour are treated as independent; a "
        "weather-driven error common to many sensors at once is not modeled.",
        "",
    ]
    path.write_text("\n".join(lines))


# @spec EVAL-RUN-002, EVAL-LOMO-004, EVAL-LOMO-005, EVAL-BASE-003, EVAL-BASE-006
def _further(c: dict) -> list[str]:
    """The leave-one-monitor-out and baseline sections."""
    s = c["lomo"]
    lines = ["## Leave one monitor out", ""]
    if c["kriged_hours"] == 0:
        lines += ["Skipped: no kriged hour.", ""]
    elif c["settings"].lomo_hours == 0:
        lines += ["Skipped: `lomo_hours` is 0.", ""]
    else:
        lines += [
            f"Over {c['lomo_hours']} hours, each scored monitor in turn was removed from the "
            "calibration references, the hour recalibrated and re-kriged, and the result scored "
            "at that monitor; a control refit on every monitor went through the same path "
            "(`lomo.csv`). RMSE over the unflagged rows, µg/m³:",
            "",
            "| Stored surface | Control | Without the monitor |",
            "|---|---|---|",
            f"| {_num(s['stored_error'])} | {_num(s['control_error'])} | "
            f"{_num(s['lomo_error'])} |",
            "",
            f"Median of |error without the monitor| − |control error|: "
            f"**{_num(s['median_difference'])} µg/m³** over {s['n']} monitor-hours. A positive "
            "value means removing the monitor made the prediction there worse — its role as a "
            "calibration target flatters the score at that monitor by about that much; zero or "
            "negative means it does not.",
            "",
            f"Sanity check on the control: |control error − stored error| has median "
            f"{_num(s['sanity_median'])} and maximum {_num(s['sanity_max'])} µg/m³; "
            + (
                "no row exceeds 0.5 µg/m³."
                if not s["sanity_rows"]
                else f"{len(s['sanity_rows'])} rows exceed 0.5 µg/m³ ("
                + ", ".join(f"{_iso(h)} {site}" for h, site in s["sanity_rows"][:10])
                + "), where the archive has moved since the surface was written (AirNow "
                "revisions, a re-applied calibration) or the refitted variogram differs from "
                "the stored one."
            ),
            "",
            "By sensors fitted to the monitor (`n_fitted_to_x`), d with hour-bootstrap "
            "intervals:",
            "",
            "| Band | Rows | Median d | Mean d |",
            "|---|---|---|---|",
            *[
                f"| {r.band} | {r.rows} | {_ci(r.median, r.median_ci_low, r.median_ci_high)} | "
                f"{_ci(r.mean, r.mean_ci_low, r.mean_ci_high)} |"
                for r in s["bands"].itertuples(index=False)
            ],
            "",
            "Per monitor: `lomo_by_monitor.csv`.",
            "",
        ]
    lines += ["## Baselines", ""]
    b = c["baselines"]
    if not len(b):
        lines += ["Skipped: no kriged hour.", ""]
        return lines
    lines += [
        "The same matchups predicted by the hour's mean and median of the kriging points (zero "
        "skill in space), by inverse-distance weighting (IDW, power 2) of the hour's kriging "
        "points, and by a random forest trained on the kriging points of every kriged hour "
        "(location, UTC hour of day, the hour's median point value). No baseline is built from "
        "a monitor value: each uses the kriging points only, and the forest is trained on point "
        "values alone. The forest is trained on the same hours it predicts, so it is a spatial "
        "hold-out, not a hold-out in time (`baselines.csv`, with level splits).",
        "",
        "| Method | Subset | n | Bias [95% CI] | MAE | RMSE [95% CI] | R² skill | r | r² |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    names = {
        "kriging": "kriging (stored surface)",
        "hour_mean": "hourly mean",
        "hour_median": "hourly median",
        "idw": "IDW",
        "rf": "random forest",
    }
    for r in b[b["split"] == "overall"].itertuples(index=False):
        lines.append(
            f"| {names[r.method]} | {r.subset} | {int(r.n)} | "
            f"{_ci(r.bias, r.bias_ci_low, r.bias_ci_high)} | {_num(r.mae)} | "
            f"{_ci(r.rmse, r.rmse_ci_low, r.rmse_ci_high)} | {_num(r.r2)} | "
            f"{_num(r.pearson_r)} | {_num(r.pearson_r2)} |"
        )
    paired = c["paired"]
    lines += [
        "",
        "Paired comparison over unflagged matchups, mean |kriging error| − |baseline error| "
        "(µg/m³, negative favours kriging; `baseline_paired.csv`):",
        "",
        "| Baseline | Split | Group | n | Mean difference [95% CI] |",
        "|---|---|---|---|---|",
    ]
    for r in paired.itertuples(index=False):
        lines.append(
            f"| {names[r.baseline]} | {r.split} | {r.group} | {r.n} | "
            f"{_ci(r.mean_difference, r.ci_low, r.ci_high)} |"
        )
    return lines + [""]


# @spec EVAL-RUN-003
def git_state(cwd: Path | str | None = None) -> dict:
    """``git_commit`` (HEAD), ``git_dirty`` (anything in ``git status --porcelain``), and, when
    dirty, ``git_diff_sha256`` (SHA-256 of ``git diff HEAD``); all None outside a checkout."""
    none = {"git_commit": None, "git_dirty": None, "git_diff_sha256": None}
    where = Path(cwd) if cwd is not None else Path(__file__).parent
    if not where.is_dir():
        return none

    def git(*args) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["git", *args], cwd=where, capture_output=True, check=False)
        except OSError:
            return None

    head = git("rev-parse", "HEAD")
    if head is None or head.returncode != 0:
        return none
    status = git("status", "--porcelain")
    dirty = bool(status.stdout.strip()) if status is not None else None
    diff_sha = None
    if dirty:
        diff = git("diff", "HEAD")
        diff_sha = hashlib.sha256(diff.stdout).hexdigest() if diff is not None else None
    return {
        "git_commit": head.stdout.decode().strip() or None,
        "git_dirty": dirty,
        "git_diff_sha256": diff_sha,
    }


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# @spec EVAL-RUN-003
def airnow_provenance(objects) -> dict:
    """The count, latest modification, and manifest hash of the AirNow objects read."""
    ordered = sorted(objects, key=lambda o: o.key)
    manifest = []
    for o in ordered:
        entry = {"key": o.key, "size": o.size}
        if o.etag is not None:
            entry["etag"] = o.etag
        if o.last_modified is not None:
            entry["last_modified"] = o.last_modified.isoformat()
        manifest.append(entry)
    stamps = [o.last_modified for o in ordered if o.last_modified is not None]
    return {
        "airnow_objects": len(ordered),
        "airnow_last_modified": max(stamps).isoformat() if stamps else None,
        "airnow_manifest_sha256": hashlib.sha256(
            json.dumps(manifest, sort_keys=True).encode()
        ).hexdigest(),
    }


# @spec EVAL-RUN-003
def write_run_json(path: Path, c: dict) -> None:
    try:
        package_version = version("air-quality-digital-twin")
    except PackageNotFoundError:
        package_version = None
    meta = {
        "window_start": c["start"].isoformat(),
        "window_end": c["end"].isoformat(),
        "archive_uri": c["archive_uri"],
        "seed": c["settings"].seed,
        "evaluation_settings": c["settings"].model_dump(),
        "map_settings": c["map_settings"].model_dump(),
        "version": package_version,
        **git_state(),
        "matchups_sha256": c["matchups_sha256"],
        **airnow_provenance(c["airnow_objects"]),
    }
    path.write_text(json.dumps(meta, indent=2, sort_keys=True))


# --- Plain answers and diagnostics -------------------------------------------------------------


def _flag_lines(rates: dict) -> list[str]:
    obs, obs_n = rates["observations_flagged"], rates["observations"]
    m, m_n = rates["matchups_flagged"], rates["matchups"]
    return [
        f"- Flagged share of AirNow observations in kriged hours: {obs} of {obs_n} "
        f"({_pct(obs / obs_n if obs_n else math.nan)}) — the error budget's flag rate.",
        f"- Flagged share of matchups: {m} of {m_n} ({_pct(m / m_n if m_n else math.nan)}) — "
        "the rate that applies to the scored numbers.",
    ]


def _beats(lo, hi) -> str:
    if _finite(hi) and hi < 0:
        return "kriging is closer"
    if _finite(lo) and lo > 0:
        return "the baseline is closer"
    return "no difference the interval can distinguish"


# @spec EVAL-RUN-005, EVAL-BASE-005, EVAL-COND-003, EVAL-LOMO-007, EVAL-POOL-001
def _plain_answers(c: dict) -> list[str]:
    lines = ["## Plain answers", ""]
    if c["kriged_hours"] == 0 or not len(c["matchups"]):
        return lines + [
            "No hour was kriged in the window, so none of the questions below (baselines, "
            "leave one monitor out, predicted level, inflation, spike, pooled) can be answered.",
            "",
        ]
    # 1. Baselines
    paired = c["paired"]
    overall = paired[paired["split"] == "overall"].set_index("baseline")
    b = c["baselines"]
    rmse = b[(b["split"] == "overall") & (b["subset"] == "unflagged")].set_index("method")
    parts = []
    for name, label in (("hour_mean", "the hourly mean"), ("hour_median", "the hourly median"),
                        ("idw", "IDW"), ("rf", "the random forest")):
        if name not in overall.index:
            continue
        r = overall.loc[name]
        parts.append(
            f"against {label}, mean |kriging error| − |baseline error| = "
            f"{_ci(r['mean_difference'], r['ci_low'], r['ci_high'])} µg/m³ "
            f"({_beats(r['ci_low'], r['ci_high'])}; RMSE {_num(rmse.loc[name, 'rmse'])} vs "
            f"kriging {_num(rmse.loc['kriging', 'rmse'])})"
        )
    lines += [
        "1. **Does kriging beat the baselines?** Over the unflagged matchups, " + "; ".join(parts)
        + ". Negative means kriging is closer to the monitor; a baseline is beaten only when "
        "its interval lies wholly below 0 (`baseline_paired.csv`).",
        "",
    ]
    # 2. Leave one monitor out
    s = c["lomo"]
    if s["n"]:
        bands = s["bands"].set_index("band")
        band_text = ", ".join(
            f"{band}: median {_ci(r['median'], r['median_ci_low'], r['median_ci_high'])} "
            f"({int(r['rows'])} rows)"
            for band, r in bands.iterrows()
        )
        lines += [
            "2. **How much does leave one monitor out worsen the error?** Median "
            f"d = |error without the monitor| − |control error| = "
            f"{_ci(s['median_d'], s['median_d_ci_low'], s['median_d_ci_high'])} µg/m³, mean "
            f"{_ci(s['mean_d'], s['mean_d_ci_low'], s['mean_d_ci_high'])} µg/m³ over "
            f"{s['n']} monitor-hours ({c['lomo_hours']} hours; intervals resample hours); worse "
            f"in {_pct(s['share_worse'])} of them. RMSE control {_num(s['control_error'])} → "
            f"without the monitor {_num(s['lomo_error'])} µg/m³. By sensors fitted to the "
            f"monitor — {band_text}. "
            + (
                f"No row falls in band {', '.join(f'`{b}`' for b in s['empty_bands'])}. "
                if s["empty_bands"]
                else ""
            )
            + (
                f"The medians {'rise' if s['dose_rises'] else 'do not rise'} with the count, "
                f"and the `{s['dose_compared'][0]}` and `{s['dose_compared'][-1]}` intervals "
                f"{'separate' if s['dose_separates'] else 'overlap'}"
                + (", so the data show a dose-response."
                   if s["dose_rises"] and s["dose_separates"]
                   else ", so the data do not show a clear dose-response.")
                if s["dose_compared"]
                else "Too few bands have rows to test a dose-response."
            ),
            "",
        ]
    else:
        lines += ["2. **Leave one monitor out** was not run (`lomo_hours` is 0).", ""]
    # 3. Predicted level
    metrics = c["metrics"]
    low, high = _group(metrics, "pred_level", "first"), _group(metrics, "pred_level", "last")
    olow, ohigh = _group(metrics, "level", "first"), _group(metrics, "level", "last")
    slopes = c["conditional"].set_index("slope")
    rel = slopes.loc["obs_on_surface"] if "obs_on_surface" in slopes.index else None
    verdict = smoothing_verdict(rel["ci_low"], rel["ci_high"]) if rel is not None else "unknown"
    holds = (
        clean_dirty_holds(low["bias_ci_low"], high["bias_ci_high"])
        if low is not None and high is not None
        else False
    )
    if low is not None and high is not None and olow is not None and ohigh is not None:
        lines += [
            "3. **Does the level-dependent bias survive by predicted level?** By observed level "
            f"the bias runs {_ci(olow['bias'], olow['bias_ci_low'], olow['bias_ci_high'])} "
            f"({olow['group']}) to "
            f"{_ci(ohigh['bias'], ohigh['bias_ci_low'], ohigh['bias_ci_high'])}"
            f" ({ohigh['group']}) µg/m³, a split that produces that pattern even for an unbiased "
            f"surface. By predicted level it runs "
            f"{_ci(low['bias'], low['bias_ci_low'], low['bias_ci_high'])} ({low['group']}) to "
            f"{_ci(high['bias'], high['bias_ci_low'], high['bias_ci_high'])} ({high['group']}). "
            f"\"High in clean air, low in dirty air\" "
            f"{'survives' if holds else 'does not survive'} the predicted-level split. The "
            f"reliability slope (obs on surface) is "
            f"{_ci(rel['estimate'], rel['ci_low'], rel['ci_high'])}: the surface is "
            f"**{verdict}**.",
            "",
        ]
    # 4. Inflation
    cov = c["coverage_diagnostics"].set_index("kind")
    if len(cov):
        t, r, z = cov.loc["z_total"], cov.loc["z_total_raw"], cov.loc["z"]
        sh = cov.loc["z_total_shared"]
        sigma2, n_hours = c["shared"]
        lines += [
            "4. **What inflation would make coverage nominal?** The total variance would need "
            f"multiplying by {_ci(t['k68'], t['k68_ci_low'], t['k68_ci_high'])} for ±1 SD and "
            f"{_ci(t['k95'], t['k95_ci_low'], t['k95_ci_high'])} for ±2 SD (kriging variance "
            f"alone: {_num(z['k68'])} and {_num(z['k95'])}). Coverage ±1/±2 SD: kriging only "
            f"{_pct(z['cov1'])}/{_pct(z['cov2'])}; total without the n / n_eff correction "
            f"{_pct(r['cov1'])}/{_pct(r['cov2'])}; total with it {_pct(t['cov1'])}/"
            f"{_pct(t['cov2'])} — the correction moves ±2 SD coverage by "
            f"{_num(100 * (t['cov2'] - r['cov2']), 1)} points. A shared hourly offset of "
            f"variance {_num(sigma2, 3)} (µg/m³)² ({n_hours} hours) would bring it to "
            f"{_pct(sh['cov1'])}/{_pct(sh['cov2'])} (diagnostic only). z_total has mean "
            f"{_num(t['mean'])} and SD {_num(t['sd'])} (0 and 1 if right).",
            "",
        ]
    # 5. Spikes
    sp = c["spike_summary"]
    if sp["points"] == 0:
        lines += ["5. **Spikes.** The spike rule flagged no kriging point in the window.", ""]
    else:
        day_text = "; ".join(
            f"{r.day}: RMSE {_num(r.rmse)} → {_num(r.rmse_without)} µg/m³"
            for r in sp["days"].itertuples(index=False)
        )
        h, u = sp["headline_all"], sp["headline_unaffected"]
        lines += [
            f"5. **How much of the affected days' error is the spikes?** The rule flagged "
            f"{sp['points']} spike points in {sp['hours']} hours. Removing them (re-krige, "
            f"diagnostic) changes the affected days' RMSE as: {day_text}. Headline RMSE "
            f"{_num(h['rmse'])} µg/m³ with every hour, {_num(u['rmse'])} without the affected "
            f"hours (bias {_num(h['bias'])} → {_num(u['bias'])}).",
            "",
        ]
    # 6. Pooling
    pool = c["pooling"].set_index("model")
    if len(pool) and pool["coefficient"].notna().all():
        supported = bool(((pool["ci_low"] > 0) | (pool["ci_high"] < 0)).all())
        lines += [
            "6. **Does the pooled fraction near a monitor drive the bias?** Error on the pooled "
            "fraction of sensors within 10 km, day held fixed: "
            + "; ".join(
                f"{model} effects {_ci(r['coefficient'], r['ci_low'], r['ci_high'])} µg/m³ per "
                "unit fraction"
                for model, r in pool.iterrows()
            )
            + f" (n = {int(pool['n'].iloc[0])}). "
            + ("Both intervals exclude 0, so the data support pooling as a driver of the bias."
               if supported
               else "The intervals do not both exclude 0, so the data do not support pooling "
               "as a driver of the bias."),
            "",
        ]
    return lines


def _diagnostic_sections(c: dict) -> list[str]:
    """Conditional bias, coverage diagnostics, spikes, pooling, variograms, lags, monitors."""
    lines = ["## Coverage diagnostics", ""]
    lines += [
        "The calibration variance at a monitor: each sensor's calibrated value carries its "
        "fit's parameter variance [1, x] C [1, x]ᵀ plus the fit's residual variance σ̂² (the "
        "diagonal of Σ_s); two pooled sensors of the same `as_of` share the pooled fit's "
        "parameter error (the off-diagonal); the kriging weights carry it to the monitor as "
        "λᵀ M Σ_s Mᵀ λ. C is the OLS covariance inflated by n / n_eff for serially correlated "
        "residuals.",
        "",
    ]
    cov = c["coverage_diagnostics"]
    if len(cov):
        lines += [
            "| z | n | ±1 SD | ±2 SD | k for ±1 SD | k for ±2 SD | mean | SD |",
            "|---|---|---|---|---|---|---|---|",
        ]
        names = {
            "z": "kriging only",
            "z_total_raw": "total, OLS covariance",
            "z_total": "total, n / n_eff corrected",
            "z_total_shared": "total + shared hourly offset (diagnostic)",
        }
        for r in cov.itertuples(index=False):
            lines.append(
                f"| {names[r.kind]} | {r.n} | {_pct(r.cov1)} | {_pct(r.cov2)} | "
                f"{_ci(r.k68, r.k68_ci_low, r.k68_ci_high)} | "
                f"{_ci(r.k95, r.k95_ci_low, r.k95_ci_high)} | {_num(r.mean)} | {_num(r.sd)} |"
            )
        sigma2, n_hours = c["shared"]
        lines += [
            "",
            f"Shared hourly offset: σ²_shared = {_num(sigma2, 3)} (µg/m³)² from {n_hours} hours "
            "(the variance of the hour's mean calibration residual over fitted sensors, less "
            "its independent part). It is computed against the same monitors coverage is "
            "scored at, so it is a diagnostic and is not added to the total. QQ plot: "
            "`qq_ztotal.png`.",
            "",
        ]
    lines += ["## Conditional bias", ""]
    slopes = c["conditional"]
    if len(slopes):
        lines += ["| Slope | Estimate [95% CI] | n |", "|---|---|---|"]
        for r in slopes.itertuples(index=False):
            lines.append(f"| {r.slope} | {_ci(r.estimate, r.ci_low, r.ci_high)} | {r.n} |")
        lines += [
            "",
            "The slope of obs on surface is the reliability slope (1 when calibrated, above 1 "
            "over-smoothed, below 1 over-dispersed); the slope of surface on obs falls below 1 "
            "for any imperfect predictor. Reliability by surface decile: `reliability.csv`.",
            "",
        ]
    lines += ["## Spikes", ""]
    sp = c["spike_summary"]
    points = c["spike_points"]
    if not len(points):
        lines += ["No kriging point met the spike rule (median + 5 × 1.4826 × MAD and above "
                  "20 µg/m³).", ""]
    else:
        lines += ["Spike points (`spike_points.csv`):", "",
                  "| Hour | Sensors | Value | Hour median | MAD |", "|---|---|---|---|---|"]
        for r in points.itertuples(index=False):
            lines.append(f"| {_iso(r.hour)} | {r.site_ids} | {_num(r.z, 1)} | "
                         f"{_num(r.median)} | {_num(r.mad)} |")
        lines += ["", "Affected hours, unflagged matchups, µg/m³ (stored, and stored + Δ "
                  "from the re-krige without the spike points):", "",
                  "| Hour | n | Bias | RMSE | Bias without | RMSE without |",
                  "|---|---|---|---|---|---|"]
        for r in sp["hour_table"].itertuples(index=False):
            lines.append(f"| {_iso(r.hour)} | {r.n} | {_num(r.bias)} | {_num(r.rmse)} | "
                         f"{_num(r.bias_without)} | {_num(r.rmse_without)} |")
        lines += [""]
    lines += ["## Pooling within the hour", ""]
    pool = c["pooling"]
    if len(pool):
        lines += ["| Model | Coefficient [95% CI] | n |", "|---|---|---|"]
        for r in pool.itertuples(index=False):
            lines.append(f"| {r.model} | {_ci(r.coefficient, r.ci_low, r.ci_high)} | {r.n} |")
        lines += ["", "Error regressed on the pooled fraction of kriging-input sensors within "
                  "10 km of the monitor, with fixed effects; intervals resample days.", ""]
    lines += ["## Variograms, timing, monitors", ""]
    v = c["variogram"]
    if len(v):
        lookup = v.set_index(["quantity", "name"])["value"]
        classes = ", ".join(
            f"{name} {int(lookup[('class_count', name)])} ({_pct(lookup[('class_share', name)])})"
            for name in ("pure_nugget", "short_range", "regular")
        )
        ranges = ", ".join(
            f"{name} {_num(value / 1000, 1)} km"
            for (q, name), value in lookup.items()
            if q == "range_m_percentile"
        )
        lines += [f"- Variogram classes: {classes}. Range percentiles: {ranges} "
                  "(`variogram.csv`; metrics by class above)."]
    lag = c["lag"]
    if len(lag):
        lines += ["- Timing (`lag.csv`): " + "; ".join(
            f"lag {int(r.lag):+d} h r {_num(r.pearson_r)}, RMSE {_num(r.rmse)} (n {int(r.n)})"
            for r in lag.itertuples(index=False)
        ) + "."]
    lines += ["- Per monitor: `per_monitor.csv`.", ""]
    return lines
