"""The report's prose and provenance: ``summary.md`` and ``run.json``."""

from __future__ import annotations

import json
import math
import subprocess
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pandas as pd


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


def _metrics_row(label: str, m: dict) -> str:
    return (
        f"| {label} | {m['n']} | {_num(m['bias'])} | {_num(m['mae'])} | {_num(m['rmse'])} | "
        f"{_num(m['r2'])} | {_num(m['pearson_r'])} | {_pct(m['cov1_krig'])} | "
        f"{_pct(m['cov2_krig'])} | {_pct(m['cov1_total'])} | {_pct(m['cov2_total'])} |"
    )


_HEADER = (
    "| Subset | n | Bias | MAE | RMSE | R² | r | ±1 SD krig | ±2 SD krig | ±1 SD total | "
    "±2 SD total |\n|---|---|---|---|---|---|---|---|---|---|---|"
)


def _split_table(metrics: pd.DataFrame, split: str) -> list[str]:
    rows = metrics[(metrics["subset"] == "unflagged") & (metrics["split"] == split)]
    lines = [_HEADER.replace("Subset", split.replace("_", " ").capitalize())]
    for row in rows.itertuples(index=False):
        lines.append(_metrics_row(str(row.group), row._asdict()))
    return lines


def _flagged(matchups: pd.DataFrame) -> int:
    return int(matchups["flagged"].sum()) if len(matchups) else 0


def _hours(values) -> str:
    values = list(values)
    if not values:
        return "none"
    shown = ", ".join(_iso(v) for v in values[:30])
    return shown + (f" … ({len(values)} in all)" if len(values) > 30 else "")


# @spec EVAL-RUN-002, EVAL-AVAIL-001, EVAL-AVAIL-003, EVAL-AVAIL-004, EVAL-FIG-005
def write_summary(path: Path, c: dict) -> None:
    lines = [
        "# Surface evaluation",
        "",
        f"Window `[{_iso(c['start'])}, {_iso(c['end'])})` UTC. Archive `{c['archive_uri']}`.",
        "",
        "## Availability",
        "",
    ]
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
        "",
        "Per monitor: `availability.csv`.",
        "",
        "## Accuracy and coverage",
        "",
        "Headline numbers use `unflagged` matchups (monitor hours without QC flags); `all` "
        "includes the flagged ones. Errors are surface − monitor, µg/m³. Coverage is the "
        "share of matchups within ±1 and ±2 standard deviations, with the kriging SD alone and "
        "with the total SD (kriging plus calibration); Gaussian errors with the right variance "
        "would give 68.3% and 95.4%.",
        "",
        _HEADER,
        _metrics_row("unflagged", c["headline"]),
        _metrics_row("all", c["all"]),
        "",
    ]
    if len(c["matchups"]):
        for split in ("level", "pooled_band", "day", "hour_of_day"):
            lines += [f"### By {split.replace('_', ' ')} (unflagged)", ""]
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
    lines += _further(c)
    ms = c["map_settings"]
    lines += [
        "## Maps",
        "",
        f"Fixed colour limits: `pm25_max` = {_num(ms.pm25_max, 1)} µg/m³, `sd_max` = "
        f"{_num(ms.sd_max, 1)} µg/m³. Window maximum surface value {_num(c['max_pm25'])} µg/m³ "
        f"({_pct(c['frac_above_pm25'])} of cells above the limit); maximum SD "
        f"{_num(c['max_sd'])} µg/m³ ({_pct(c['frac_above_sd'])} of cells above). Cells above a "
        "limit are drawn in the top colour and the colorbar carries an arrow.",
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


# @spec EVAL-RUN-002, EVAL-LOMO-004, EVAL-BASE-003
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
        ]
    lines += ["## Baselines", ""]
    b = c["baselines"]
    if not len(b):
        lines += ["Skipped: no kriged hour.", ""]
        return lines
    lines += [
        "The same matchups predicted by inverse-distance weighting (IDW, power 2) of the hour's "
        "kriging points, and by a random forest trained on the kriging points of every kriged "
        "hour (location, UTC hour of day, the hour's median point value). Neither sees a "
        "monitor value. The forest is trained on the same hours it predicts, so it is a spatial "
        "hold-out, not a hold-out in time (`baselines.csv`).",
        "",
        "| Method | Subset | n | Bias | MAE | RMSE | R² | r |",
        "|---|---|---|---|---|---|---|---|",
    ]
    names = {"kriging": "kriging (stored surface)", "idw": "IDW", "rf": "random forest"}
    for r in b.itertuples(index=False):
        lines.append(
            f"| {names[r.method]} | {r.subset} | {int(r.n)} | {_num(r.bias)} | {_num(r.mae)} | "
            f"{_num(r.rmse)} | {_num(r.r2)} | {_num(r.pearson_r)} |"
        )
    return lines + [""]


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).parent,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


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
        "git_commit": _git_commit(),
    }
    path.write_text(json.dumps(meta, indent=2, sort_keys=True))
