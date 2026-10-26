"""The report's prose and provenance: ``summary.md`` and ``run.json``."""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pandas as pd

from aqdt.evaluation.anomaly import skill_source
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
    return f"{100 * value + 0.0:.1f}%" if abs(value) >= 5e-4 else "0.0%"


def _finite(value) -> bool:
    return value is not None and isinstance(value, (int, float)) and math.isfinite(value)


def _ci(value, lo, hi, digits: int = 2) -> str:
    """``value [lo, hi]``, or the value alone when the interval is not defined."""
    if not _finite(lo) or not _finite(hi):
        return _num(value, digits)
    return f"{_num(value, digits)} [{_num(lo, digits)}, {_num(hi, digits)}]"


def _signed(value, digits: int = 2) -> str:
    if not _finite(value):
        return "—"
    return f"{value:+.{digits}f}"


def _sci(value, lo, hi, digits: int = 2) -> str:
    """``_ci`` with explicit signs."""
    if not _finite(lo) or not _finite(hi):
        return _signed(value, digits)
    return f"{_signed(value, digits)} [{_signed(lo, digits)}, {_signed(hi, digits)}]"


def _pct_ci(value, lo, hi) -> str:
    if not _finite(lo) or not _finite(hi):
        return _pct(value)
    return f"{_pct(value)} [{_pct(lo)}, {_pct(hi)}]"


# @spec EVAL-RUN-006
def headline_rows(metrics: pd.DataFrame, headline: dict, all_metrics: dict) -> tuple[dict, dict]:
    """The headline ``unflagged`` and ``all`` metrics with the bias and RMSE intervals of the
    metric table's ``overall`` rows for the same subset."""
    out = []
    for subset, m in (("unflagged", headline), ("all", all_metrics)):
        rows = metrics[(metrics["subset"] == subset) & (metrics["split"] == "overall")]
        row = dict(m)
        if len(rows):
            first = rows.iloc[0]
            for name in ("bias", "rmse"):
                for side in ("low", "high"):
                    key = f"{name}_ci_{side}"
                    if key in first.index:
                        row[key] = first[key]
        out.append(row)
    return out[0], out[1]


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
# @spec EVAL-SPLIT-005, EVAL-FLAG-001, EVAL-RUN-006
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
        *(
            _metrics_row(label, row)
            for label, row in zip(
                ("unflagged", "all"), headline_rows(c["metrics"], c["headline"], c["all"])
            )
        ),
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


METHOD_NAMES = {"kriging": "kriging", "idw": "IDW", "rf": "the random forest",
                "hour_mean": "the hourly mean"}
TABLE_NAMES = {"kriging": "Kriging", "idw": "IDW", "rf": "Random forest",
               "hour_mean": "Hourly mean"}


# @spec EVAL-ANOM-004
def anomaly_answer(table: pd.DataFrame) -> str:
    """Where kriging's skill comes from: its offset and anomaly shares, the anomaly and
    hourly-mean correlations, the verdict, and beside it the variance-explained table."""
    title = "**Where does kriging's skill come from?** "
    t = table.set_index("method")
    if not len(t) or "kriging" not in t.index or not t.loc["kriging", "n"]:
        return title + (
            "No hour has at least 5 unflagged matchups, so the error cannot be split into the "
            "hour's common offset and the within-hour anomaly."
        )
    k = t.loc["kriging"]

    def ci(row, name: str) -> str:
        return _ci(row[name], row[f"{name}_ci_low"], row[f"{name}_ci_high"])

    def pct(row, name: str) -> str:
        return _pct_ci(row[name], row[f"{name}_ci_low"], row[f"{name}_ci_high"])

    spatial = [m for m in ("kriging", "idw", "rf") if m in t.index]
    corr = ", ".join(f"{ci(t.loc[m], 'anomaly_r')} for {METHOD_NAMES[m]}" for m in spatial)
    source = skill_source(k["v_across"], k["across_explained"], k["v_within"],
                          k["within_explained"])
    others = [m for m in ("idw", "rf") if m in t.index]
    level_note = (
        f" (the hourly mean, which tracks only the level, explains "
        f"{_pct(t.loc['hour_mean', 'across_explained'])})"
        if "hour_mean" in t.index
        else ""
    )
    pattern_note = (
        " (" + ", ".join(f"{METHOD_NAMES[m]} {_pct(t.loc[m, 'within_explained'])}"
                         for m in others) + ")"
        if others
        else ""
    )
    text = title + (
        f"Over {int(k['n'])} unflagged matchups in {int(k['hours'])} hours with at least 5 "
        f"monitors, the hour's common offset (the mean error across the monitors) is "
        f"{pct(k, 'offset_share')} of kriging's MSE and the within-hour anomaly "
        f"{pct(k, 'anomaly_share')}. Within the hour, the predicted anomalies correlate with "
        f"the monitors' at r = {corr}; across hours, kriging's hourly mean tracks the monitors' "
        f"at r = {ci(k, 'hourly_r')}. Kriging's skill comes mainly from {source}: it explains "
        f"{_pct(k['across_explained'])} of the monitors' across-hour variance{level_note} and "
        f"{_pct(k['within_explained'])} of their within-hour variance{pattern_note}. Variance "
        f"explained, with day-bootstrap intervals (`anomaly.csv`):"
    )
    header = (
        f"| Method | Across hours ({_pct(k['across_share'])} of monitor variance) | "
        f"Within the hour ({_pct(k['within_share'])}) |"
    )
    rows = [
        f"| {TABLE_NAMES[m]} | "
        f"{pct(t.loc[m], 'across_explained')} | {pct(t.loc[m], 'within_explained')} |"
        for m in ("kriging", "idw", "rf", "hour_mean")
        if m in t.index
    ]
    table_lines = [header, "|---|---|---|", *rows]
    return text + "\n\n" + "\n".join("   " + line for line in table_lines)


# @spec EVAL-POOL-003
def pooling_answer(table: pd.DataFrame) -> str:
    """The pooled share's distribution, the raw and within-day differences with their signs and
    per-10-point sizes, the effect across the interquartile range, and the leverage."""
    title = "**What does the pooled fraction near a monitor do to the bias?** "
    t = table.set_index("quantity")
    needed = ("day", "day+hour_of_day", "raw_high_minus_low", "share_p25", "share_p75")
    if not len(t) or any(q not in t.index or not _finite(t.loc[q, "estimate"]) for q in needed):
        return title + "Too few matchups have a pooled share within 10 km to answer."
    day, hod, raw = t.loc["day"], t.loc["day+hour_of_day"], t.loc["raw_high_minus_low"]
    p25, p75 = t.loc["share_p25", "estimate"], t.loc["share_p75", "estimate"]
    same = (raw["estimate"] > 0) == (day["estimate"] > 0)
    distinguishable = all(
        (r["ci_low"] > 0) or (r["ci_high"] < 0) for r in (day, hod)
    )

    def per10(r) -> str:
        return _sci(r["estimate"] / 10, r["ci_low"] / 10, r["ci_high"] / 10)

    leverage = (
        "Leverage is low: three quarters of the matchups have under 10% pooled sensors "
        "nearby, so no coefficient on the share can carry much of the bias."
        if p75 < 0.10
        else "Leverage is not low: at least a quarter of the matchups have 10% or more pooled "
        "sensors nearby."
    )
    return title + (
        f"The pooled share of kriging-input sensors within 10 km has mean "
        f"{_pct(t.loc['share_mean', 'estimate'])}, quartiles {_pct(p25)} / "
        f"{_pct(t.loc['share_p50', 'estimate'])} / {_pct(p75)}, and exactly 0 for "
        f"{_pct(t.loc['share_zero', 'estimate'])} of the {int(day['n'])} matchups. Raw, the "
        f"matchups above the median share differ in mean error from the rest by "
        f"{_sci(raw['estimate'], raw['ci_low'], raw['ci_high'])} µg/m³ (high − low). "
        f"With the day held fixed the sign is {'the same' if same else 'the opposite'}: "
        f"error changes by {per10(day)} µg/m³ per 10 percentage points of pooled share with day "
        f"effects ({_sci(day['estimate'], day['ci_low'], day['ci_high'])} per unit fraction), "
        f"and {per10(hod)} with day and hour-of-day effects; "
        + (
            "both intervals exclude 0, so the within-day association is distinguishable from 0. "
            if distinguishable
            else "the intervals do not both exclude 0, so the within-day association is not "
            "distinguishable from 0. "
        )
        + f"Across the interquartile range ({_pct(p25)} to {_pct(p75)}) the day-effects "
        f"coefficient amounts to {_num(day['estimate'] * (p75 - p25))} µg/m³. {leverage} "
        "(`pooling.csv`)"
    )


def _inflation_answer(c: dict) -> str | None:
    cov = c["coverage_diagnostics"].set_index("kind")
    if not len(cov):
        return None
    t, r, z = cov.loc["z_total"], cov.loc["z_total_raw"], cov.loc["z"]
    sh = cov.loc["z_total_shared"]
    sigma2, n_hours = c["shared"]
    text = (
        "**What inflation would make coverage nominal, and does a shared hourly offset close the "
        "gap without peeking?** The total variance would need multiplying by "
        f"{_ci(t['k68'], t['k68_ci_low'], t['k68_ci_high'])} for ±1 SD and "
        f"{_ci(t['k95'], t['k95_ci_low'], t['k95_ci_high'])} for ±2 SD (kriging variance alone: "
        f"{_num(z['k68'])} and {_num(z['k95'])}). Coverage ±1/±2 SD: kriging only "
        f"{_pct(z['cov1'])}/{_pct(z['cov2'])}; total without the n / n_eff correction "
        f"{_pct(r['cov1'])}/{_pct(r['cov2'])}; total with it "
        f"{_pct_ci(t['cov1'], t['cov1_ci_low'], t['cov1_ci_high'])}/"
        f"{_pct_ci(t['cov2'], t['cov2_ci_low'], t['cov2_ci_high'])} — the correction moves ±2 SD "
        f"coverage by {_num(100 * (t['cov2'] - r['cov2']), 1)} points. A shared hourly offset of "
        f"variance {_num(sigma2, 3)} (µg/m³)² ({n_hours} hours), estimated and scored on the same "
        f"matchups, would bring it to {_pct(sh['cov1'])}/{_pct(sh['cov2'])}."
    )
    split = []
    for kind, est, scored in (("z_total_shared_odd_on_even", "odd", "even"),
                              ("z_total_shared_even_on_odd", "even", "odd")):
        if kind not in cov.index:
            continue
        row = cov.loc[kind]
        split.append(
            f"estimated on {est} UTC days (σ²_shared {_num(row['shared_variance'], 3)} "
            f"(µg/m³)², {_num(row['shared_hours'], 0)} hours) and scored on the {int(row['n'])} "
            f"matchups of {scored} days, ±1/±2 SD coverage is "
            f"{_pct_ci(row['cov1'], row['cov1_ci_low'], row['cov1_ci_high'])}/"
            f"{_pct_ci(row['cov2'], row['cov2_ci_low'], row['cov2_ci_high'])}"
        )
    if split:
        text += (
            " Without the overlap — " + "; ".join(split) + " (68.3%/95.4% nominal). Neither "
            "estimate is scored on the matchups it came from (diagnostic only; not added to the "
            "total)."
        )
    return text + f" z_total has mean {_num(t['mean'])} and SD {_num(t['sd'])} (0 and 1 if right)."


def _baselines_answer(c: dict) -> str:
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
    return (
        "**Does kriging beat the baselines?** Over the unflagged matchups, " + "; ".join(parts)
        + ". Negative means kriging is closer to the monitor; a baseline is beaten only when "
        "its interval lies wholly below 0 (`baseline_paired.csv`)."
    )


def _lomo_answer(c: dict) -> str:
    s = c["lomo"]
    if not s["n"]:
        return "**Leave one monitor out** was not run (`lomo_hours` is 0)."
    bands = s["bands"].set_index("band")
    band_text = ", ".join(
        f"{band}: median {_ci(r['median'], r['median_ci_low'], r['median_ci_high'])} "
        f"({int(r['rows'])} rows)"
        for band, r in bands.iterrows()
    )
    return (
        "**How much does leave one monitor out worsen the error?** Median "
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
        )
    )


def _level_answer(c: dict) -> str | None:
    metrics = c["metrics"]
    low, high = _group(metrics, "pred_level", "first"), _group(metrics, "pred_level", "last")
    olow, ohigh = _group(metrics, "level", "first"), _group(metrics, "level", "last")
    if low is None or high is None or olow is None or ohigh is None:
        return None
    slopes = c["conditional"].set_index("slope")
    rel = slopes.loc["obs_on_surface"] if "obs_on_surface" in slopes.index else None
    verdict = smoothing_verdict(rel["ci_low"], rel["ci_high"]) if rel is not None else "unknown"
    holds = clean_dirty_holds(low["bias_ci_low"], high["bias_ci_high"])
    observed_spread = olow["bias"] - ohigh["bias"]
    predicted_spread = low["bias"] - high["bias"]
    ratio = predicted_spread / observed_spread if observed_spread else math.nan
    size = (
        f"a low-to-high spread of {_num(predicted_spread)} µg/m³ against {_num(observed_spread)} "
        f"by observed level ({_pct(ratio)} of it)"
    )
    pattern = (
        f"\"High in clean air, low in dirty air\" persists by predicted level — both band "
        f"intervals exclude 0 — but at {size}."
        if holds
        else f"\"High in clean air, low in dirty air\" does not survive the predicted-level split "
        f"({size})."
    )
    return (
        "**Does the level-dependent bias survive by predicted level?** By observed level the "
        f"bias runs {_sci(olow['bias'], olow['bias_ci_low'], olow['bias_ci_high'])} "
        f"({olow['group']}) to {_sci(ohigh['bias'], ohigh['bias_ci_low'], ohigh['bias_ci_high'])}"
        f" ({ohigh['group']}) µg/m³, a split that produces that pattern even for an unbiased "
        f"surface. By predicted level it runs "
        f"{_sci(low['bias'], low['bias_ci_low'], low['bias_ci_high'])} ({low['group']}) to "
        f"{_sci(high['bias'], high['bias_ci_low'], high['bias_ci_high'])} ({high['group']}). "
        f"{pattern} The reliability slope (obs on surface) is "
        + (_ci(rel["estimate"], rel["ci_low"], rel["ci_high"]) if rel is not None else "—")
        + f": the surface is **{verdict}**."
    )


def _spike_answer(c: dict) -> str:
    sp = c["spike_summary"]
    if sp["points"] == 0:
        return "**Spikes.** The spike rule flagged no kriging point in the window."
    day_text = "; ".join(
        f"{r.day}: RMSE {_num(r.rmse)} → {_num(r.rmse_without)} µg/m³"
        for r in sp["days"].itertuples(index=False)
    )
    h, u = sp["headline_all"], sp["headline_unaffected"]
    tiers = c["spike_tiers"]
    tier_text = "; ".join(
        f"{r.tier} µg/m³: {r.points} points in {r.hours} hours, bias {_num(r.bias)} → "
        f"{_num(r.bias_without)} over {r.n} matchups (change "
        f"{_ci(r.change, r.change_ci_low, r.change_ci_high)})"
        for r in tiers.itertuples(index=False)
        if r.points
    )
    return (
        f"**How much of the affected days' error is the spikes?** The rule flagged "
        f"{sp['points']} spike points in {sp['hours']} hours. Removing them (re-krige, "
        f"diagnostic) changes the affected days' RMSE as: {day_text}. Headline RMSE "
        f"{_num(h['rmse'])} µg/m³ with every hour, {_num(u['rmse'])} without the affected "
        f"hours (bias {_num(h['bias'])} → {_num(u['bias'])}). By spike value, removing only "
        f"that tier's points: {tier_text} (`spike_tiers.csv`)."
    )


# @spec EVAL-RUN-005, EVAL-BASE-005, EVAL-COND-003, EVAL-LOMO-007, EVAL-SPIKE-005
def _plain_answers(c: dict) -> list[str]:
    lines = ["## Plain answers", ""]
    if c["kriged_hours"] == 0 or not len(c["matchups"]):
        return lines + [
            "No hour was kriged in the window, so none of the questions below (where kriging's "
            "skill comes from, pooled share, inflation, baselines, leave one monitor out, "
            "predicted level, spike) can be answered.",
            "",
        ]
    answers = [
        anomaly_answer(c["anomaly"]),
        pooling_answer(c["pooling"]),
        _inflation_answer(c),
        _baselines_answer(c),
        _lomo_answer(c),
        _level_answer(c),
        _spike_answer(c),
    ]
    for k, text in enumerate((a for a in answers if a), start=1):
        lines += [f"{k}. {text}", ""]
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
            "| z | n | ±1 SD [95% CI] | ±2 SD [95% CI] | k for ±1 SD | k for ±2 SD | mean | SD |",
            "|---|---|---|---|---|---|---|---|",
        ]
        names = {
            "z": "kriging only",
            "z_total_raw": "total, OLS covariance",
            "z_total": "total, n / n_eff corrected",
            "z_total_shared": "total + shared hourly offset, in sample (diagnostic)",
            "z_total_shared_odd_on_even": "total + shared offset from odd days, scored on even "
            "days",
            "z_total_shared_even_on_odd": "total + shared offset from even days, scored on odd "
            "days",
        }
        for r in cov.itertuples(index=False):
            lines.append(
                f"| {names[r.kind]} | {r.n} | {_pct_ci(r.cov1, r.cov1_ci_low, r.cov1_ci_high)} | "
                f"{_pct_ci(r.cov2, r.cov2_ci_low, r.cov2_ci_high)} | "
                f"{_ci(r.k68, r.k68_ci_low, r.k68_ci_high)} | "
                f"{_ci(r.k95, r.k95_ci_low, r.k95_ci_high)} | {_num(r.mean)} | {_num(r.sd)} |"
            )
        sigma2, n_hours = c["shared"]
        lines += [
            "",
            f"Shared hourly offset: σ²_shared = {_num(sigma2, 3)} (µg/m³)² from {n_hours} hours "
            "(the variance of the hour's mean calibration residual over fitted sensors, less "
            "its independent part). It is computed against the same monitors coverage is "
            "scored at, so it is a diagnostic and is not added to the total. The split-day rows "
            "estimate it on the hours of odd (even) UTC days of the month and score the "
            "matchups of even (odd) days, so no matchup is scored with an offset estimated from "
            "it. QQ plot: `qq_ztotal.png`.",
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
        tiers = c["spike_tiers"]
        lines += ["By spike value (`spike_tiers.csv`), each tier's points removed alone, over the "
                  "unflagged matchups of the hours holding one, µg/m³:", "",
                  "| Tier | Points | Hours | n | Bias | Bias without | Change [95% CI] |",
                  "|---|---|---|---|---|---|---|"]
        for r in tiers.itertuples(index=False):
            lines.append(f"| {r.tier} | {r.points} | {r.hours} | {r.n} | {_num(r.bias)} | "
                         f"{_num(r.bias_without)} | "
                         f"{_ci(r.change, r.change_ci_low, r.change_ci_high)} |")
        lines += [""]
    lines += ["## Pooling within the hour", ""]
    pool = c["pooling"]
    if len(pool):
        labels = {
            "day": "coefficient, day effects (per unit fraction)",
            "day+hour_of_day": "coefficient, day + hour-of-day effects (per unit fraction)",
            "raw_high_minus_low": "raw mean error, above − at or below the median share",
            "share_mean": "pooled share, mean",
            "share_p25": "pooled share, 25th percentile",
            "share_p50": "pooled share, median",
            "share_p75": "pooled share, 75th percentile",
            "share_zero": "pooled share, fraction exactly 0",
        }
        lines += ["| Quantity | Estimate [95% CI] | n |", "|---|---|---|"]
        for r in pool.itertuples(index=False):
            digits = 3 if r.quantity.startswith("share") else 2
            lines.append(f"| {labels.get(r.quantity, r.quantity)} | "
                         f"{_ci(r.estimate, r.ci_low, r.ci_high, digits)} | {r.n} |")
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
        lines += [lag_lines(lag)]
    lines += ["- Per monitor: `per_monitor.csv`.", ""]
    return lines


# @spec EVAL-LAG-002
def lag_lines(lag: pd.DataFrame) -> str:
    """The timestamp convention, each lag's fit, and whether any lag points to a misalignment."""
    parts = []
    for r in lag.itertuples(index=False):
        text = f"lag {int(r.lag):+d} h r {_num(r.pearson_r)}, RMSE {_num(r.rmse)} (n {int(r.n)})"
        if int(r.lag) != 0:
            text += (
                f", RMSE − lag 0 "
                f"{_sci(r.rmse_difference, r.rmse_difference_ci_low, r.rmse_difference_ci_high)}"
            )
        parts.append(text)
    rmse0 = lag.loc[lag["lag"] == 0, "rmse"]
    rmse0 = float(rmse0.iloc[0]) if len(rmse0) else math.nan
    better = [
        r for r in lag.itertuples(index=False)
        if int(r.lag) != 0 and _finite(r.rmse_difference_ci_high) and r.rmse_difference_ci_high < 0
    ]
    verdict = (
        "No lag fits better than lag 0: no RMSE difference interval lies wholly below 0."
        if not better
        else " ".join(
            f"Lag {int(r.lag):+d} h fits better by {_num(-r.rmse_difference)} µg/m³ "
            f"({_pct(-r.rmse_difference / rmse0)} of the lag-0 RMSE)."
            for r in better
        )
        + " The hour labels follow the convention above, so the better fit is not a labelling "
        "error."
    )
    return (
        "- Timing (`lag.csv`): both sides are labelled by the start of their hour. AirNow's hour "
        "H is the monitor's average over [H, H + 1 h); the surface's hour H is kriged from "
        "PurpleAir snapshots with `observed_at` in [H, H + 1 h) — with hourly collection, one "
        "reading near H:30. Matchups pair them at the same H, with no shift. "
        + "; ".join(parts) + ". " + verdict
    )
