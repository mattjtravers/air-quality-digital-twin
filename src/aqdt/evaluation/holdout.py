"""``holdout``: evaluate a development window and a later test window with the same settings,
estimate the shared hourly offset and tune and fit a random forest on the development window
alone, and score both — with the reliability of every interval, where and when the uncertainty
is large, and worked prediction examples — on the test window."""

from __future__ import annotations

import json
import logging
import math
import platform
import sys
import tarfile
from datetime import datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import numpy as np
import pandas as pd
from scipy.stats import norm

from aqdt.calibration.products import CALIBRATED_HOURLY
from aqdt.calibration.schemas import CalibrationSettings, hour_bound
from aqdt.evaluation import forest, holdout_figures, lomo, report
from aqdt.evaluation.bootstrap import days, mean_interval, metric_intervals, sums_interval
from aqdt.evaluation.calse import POOLED, rebuild_fits
from aqdt.evaluation.metrics import matchup_metrics
from aqdt.evaluation.run import (
    _date_filter,
    _fit_key,
    _monitor_sites,
    calibration_variance_at,
    evaluate,
)
from aqdt.evaluation.schemas import EvaluationSettings, HoldoutSummary
from aqdt.fusion.grid import Grid
from aqdt.fusion.krige import CANDIDATES
from aqdt.fusion.points import hour_points, hour_sensors
from aqdt.fusion.products import SURFACE_FITS
from aqdt.fusion.schemas import FusionSettings, SurfaceStatus
from aqdt.maps.settings import MapSettings
from aqdt.maps.surface import read_surface
from aqdt.observation_store.archive import (
    read_observations,
    read_partitioned,
    read_sites,
    resolve_archive_uri,
)
from aqdt.observation_store.schemas import Source
from aqdt.purpleair.qc import CORRECTION_HUMIDITY, CORRECTION_INTERCEPT, CORRECTION_SLOPE

log = logging.getLogger(__name__)

LOCAL_TZ = "America/New_York"
HALF_WIDTHS = [0.6745, 1.0, 1.2816, 1.6449, 2.0]
SD_KINDS = ["kriging", "total", "total_dev_offset", "rf_trees"]
DISTANCE_EDGES = [0.0, 0.75, 1.25, 2.0, math.inf]
DISTANCE_LABELS = ["<0.75", "0.75–1.25", "1.25–2", "≥2"]
METHODS = {
    "kriging": "surface",
    "rf_dev": "rf_dev",
    "idw": "idw",
    "hour_mean": "hour_mean",
    "rf_in_window": "rf",
}
REFERENCE_NOTE = "reference only: trained on the hours it is scored on"
EXAMPLE_HOURS = (6, 12, 18)
MIN_EXAMPLE_MATCHUPS = 20
VERSIONED = {
    "aqdt": "air-quality-digital-twin",
    "numpy": "numpy",
    "pandas": "pandas",
    "scipy": "scipy",
    "scikit-learn": "scikit-learn",
    "pykrige": "pykrige",
    "shap": "shap",
    "matplotlib": "matplotlib",
    "rasterio": "rasterio",
    "pyproj": "pyproj",
}
_SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")
REDACTED = "<redacted>"
HOUR = timedelta(hours=1)


# --- Windows and archive reads ---------------------------------------------------------------


def _window(window, name: str) -> tuple[datetime, datetime]:
    start, end = (hour_bound(value, f"{name} {side}") for value, side in zip(window, ("start",
                                                                                       "end")))
    if end <= start:
        raise ValueError(f"the {name} window must end after it starts: [{start}, {end})")
    return start, end


def _fits(archive_uri: str, start: datetime, end: datetime) -> pd.DataFrame:
    fits = read_partitioned(archive_uri, SURFACE_FITS, date=_date_filter(start, end))
    return fits[(fits["hour"] >= start) & (fits["hour"] < end)].sort_values("hour")


def _calibrated(archive_uri: str, start: datetime, end: datetime) -> pd.DataFrame:
    return read_partitioned(archive_uri, CALIBRATED_HOURLY, date=_date_filter(start, end))


# @spec EVAL-HOLD-012
def window_points(archive_uri: str | None, start: datetime, end: datetime) -> pd.DataFrame:
    """Every kriging point (``hour``, ``x``, ``y``, ``z``) of every kriged hour in
    ``[start, end)``, rebuilt with fusion's ``hour_points``; no AirNow value enters it."""
    archive_uri = resolve_archive_uri(archive_uri)
    fits = _fits(archive_uri, start, end)
    calibrated = _calibrated(archive_uri, start, end)
    sites = read_sites(archive_uri)
    return _points(fits, calibrated, sites)


def _points(fits: pd.DataFrame, calibrated: pd.DataFrame, sites: pd.DataFrame) -> pd.DataFrame:
    frames = []
    for hour in fits.loc[fits["status"] == SurfaceStatus.kriged.value, "hour"]:
        p = hour_points(calibrated, sites, pd.Timestamp(hour).to_pydatetime())
        if p.n_points:
            frames.append(pd.DataFrame({"hour": hour, "x": p.x, "y": p.y, "z": p.z}))
    if not frames:
        return pd.DataFrame(columns=["hour", "x", "y", "z"])
    return pd.concat(frames, ignore_index=True)


# @spec EVAL-HOLD-011
def hour_calibration_variance(archive_uri: str | None, hour: datetime, tx, ty) -> np.ndarray:
    """The calibration term ``λᵀ M Σ_s Mᵀ λ`` (EVAL-TOTVAR-003) of one kriged hour at each target
    ``(tx, ty)``, from the hour's rebuilt kriging inputs and fits."""
    archive_uri = resolve_archive_uri(archive_uri)
    hour = hour_bound(hour, "hour")
    fit = _fits(archive_uri, hour, hour + HOUR).iloc[0]
    calibrated = _calibrated(archive_uri, hour, hour + HOUR)
    sites = read_sites(archive_uri)
    sensors = hour_sensors(calibrated, sites, hour)
    points = hour_points(calibrated, sites, hour)
    fitted, pooled = set(), set()
    for row in sensors.itertuples(index=False):
        key = _fit_key(row)
        (pooled.add(key[1]) if key[0] == POOLED else fitted.add(key))
    rebuilt = rebuild_fits(archive_uri, fitted, pooled, CalibrationSettings())
    stored = {(r.site_id, pd.Timestamp(r.as_of)): r for r in rebuilt.stored.itertuples(index=False)}
    return calibration_variance_at(fit, sensors, points, rebuilt, stored, tx, ty)


def _read_matchups(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    frame["hour"] = pd.to_datetime(frame["hour"], utc=True)
    for column in ("flagged", "inputs_reproduced"):
        frame[column] = frame[column].astype(bool)
    return frame


def _read_lomo(path: Path) -> pd.DataFrame:
    frame = pd.read_csv(path)
    if len(frame):
        frame["hour"] = pd.to_datetime(frame["hour"], utc=True)
        frame["flagged"] = frame["flagged"].astype(bool)
    return frame


# --- Interval helpers --------------------------------------------------------------------------


def _mean_ci(frame: pd.DataFrame, values, seed: int) -> tuple[float, float]:
    return mean_interval(days(frame["hour"]), np.asarray(values, dtype=float), seed)


def _rmse_ci(frame: pd.DataFrame, error, seed: int) -> tuple[float, float]:
    e = np.asarray(error, dtype=float)
    data = np.column_stack([np.ones_like(e), e**2])
    return sums_interval(days(frame["hour"]), data, lambda t: np.sqrt(t[:, 1] / t[:, 0]), seed)


def _with_ci(name: str, value: float, ci: tuple[float, float]) -> dict:
    return {name: value, f"{name}_ci_low": ci[0], f"{name}_ci_high": ci[1]}


def _covered(error, sd, k: float) -> np.ndarray:
    return (np.abs(np.asarray(error, dtype=float)) <= k * np.asarray(sd, dtype=float)).astype(
        float
    )


def _coverage(frame: pd.DataFrame, error, sd, k: float, name: str, seed: int) -> dict:
    hit = _covered(error, sd, k)
    value = float(hit.mean()) if len(hit) else math.nan
    return _with_ci(name, value, _mean_ci(frame, hit, seed) if len(hit) else (math.nan,) * 2)


def _accuracy(frame: pd.DataFrame, seed: int) -> dict:
    e = frame["error"].to_numpy(dtype=float)
    if not len(e):
        return {"n": 0}
    return {
        "n": len(e),
        **_with_ci("bias", float(e.mean()), _mean_ci(frame, e, seed)),
        **_with_ci("mae", float(np.abs(e).mean()), _mean_ci(frame, np.abs(e), seed)),
        **_with_ci("rmse", float(np.sqrt((e**2).mean())), _rmse_ci(frame, e, seed)),
    }


# --- Tables ------------------------------------------------------------------------------------


# @spec EVAL-HOLD-003
def scored_set(matchups: pd.DataFrame) -> pd.DataFrame:
    """The unflagged matchups ``cov1_total`` counts: inputs reproduced, a calibration term, and a
    positive total SD."""
    keep = (
        ~matchups["flagged"]
        & matchups["inputs_reproduced"]
        & matchups["cal_variance"].notna()
        & (matchups["total_sd"] > 0)
    )
    return matchups[keep].reset_index(drop=True)


# @spec EVAL-HOLD-004
def _completeness(
    name: str,
    start: datetime,
    end: datetime,
    fits: pd.DataFrame,
    observations: pd.DataFrame,
    inside: set[str],
    matchups: pd.DataFrame,
    scored: pd.DataFrame | None,
) -> dict:
    hours = len(pd.date_range(start, end, freq="h", inclusive="left"))
    kriged = set(fits.loc[fits["status"] == SurfaceStatus.kriged.value, "hour"])
    row = {
        "window": name,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hours": hours,
        "kriged_hours": len(kriged),
        "no_fit_row": hours - fits["hour"].nunique(),
    }
    for status in SurfaceStatus:
        if status != SurfaceStatus.kriged:
            row[f"not_kriged_{status.value}"] = int((fits["status"] == status.value).sum())
    row["days"] = len({pd.Timestamp(h).date() for h in kriged})
    at = observations[observations["observed_at"].isin(kriged) & observations["site_id"].isin(
        inside)]
    valued = at["pm25_raw"].notna()
    flagged = at["qc_flags"].map(lambda f: f is not None and len(f) > 0)
    row.update(
        {
            "monitor_hours_all": len(at),
            "monitor_hours_valued": int(valued.sum()),
            "monitor_hours_unflagged": int((valued & ~flagged).sum()),
            "monitor_hours_possible": len(kriged) * len(inside),
            "matchups": len(matchups),
            "matchups_unflagged": int((~matchups["flagged"]).sum()),
            "scored": len(scored) if scored is not None else math.nan,
        }
    )
    return row


DEV_VS_TEST = ["n", "bias", "mae", "rmse", "r2", "pearson_r", "cov1_krig", "cov2_krig",
               "cov1_total", "cov2_total"]


# @spec EVAL-HOLD-005
def _dev_vs_test(windows: dict[str, Path], seed: int) -> pd.DataFrame:
    rows: dict[str, dict] = {s: {"statistic": s} for s in
                             [*DEV_VS_TEST, "stored_rmse", "control_rmse", "lomo_rmse",
                              "lomo_median_d"]}
    for name, folder in windows.items():
        metrics = pd.read_csv(folder / "metrics.csv")
        overall = metrics[(metrics["subset"] == "unflagged") & (metrics["split"] == "overall")]
        row = overall.iloc[0] if len(overall) else pd.Series(dtype=float)
        for stat in DEV_VS_TEST:
            rows[stat][name] = row.get(stat, math.nan)
            rows[stat][f"{name}_ci_low"] = row.get(f"{stat}_ci_low", math.nan)
            rows[stat][f"{name}_ci_high"] = row.get(f"{stat}_ci_high", math.nan)
        stats = lomo.lomo_statistics(_read_lomo(folder / "lomo.csv"), seed)
        for stat, column in (("stored_rmse", "stored_error"), ("control_rmse", "control_error"),
                             ("lomo_rmse", "lomo_error")):
            rows[stat].update({name: stats[column], f"{name}_ci_low": math.nan,
                               f"{name}_ci_high": math.nan})
        rows["lomo_median_d"].update(
            {
                name: stats["median_d"],
                f"{name}_ci_low": stats["median_d_ci_low"],
                f"{name}_ci_high": stats["median_d_ci_high"],
            }
        )
    columns = ["statistic"] + [f"{n}{s}" for n in windows for s in ("", "_ci_low", "_ci_high")]
    return pd.DataFrame(list(rows.values()), columns=columns)


# @spec EVAL-HOLD-006
def _oos_offset(scored: pd.DataFrame, sigma2: float, hours: int, seed: int) -> pd.DataFrame:
    sd = np.sqrt(scored["total_variance"] + sigma2)
    row = {"sigma2_shared_dev": sigma2, "hours_dev": hours, "n": len(scored)}
    for k in (1, 2):
        row.update(_coverage(scored, scored["error"], sd, k, f"cov{k}", seed))
    return pd.DataFrame([row])


def _sd_kinds(scored: pd.DataFrame, sigma2: float) -> dict[str, tuple[pd.Series, pd.Series]]:
    return {
        "kriging": (scored["error"], scored["sd"]),
        "total": (scored["error"], scored["total_sd"]),
        "total_dev_offset": (scored["error"], np.sqrt(scored["total_variance"] + sigma2)),
        "rf_trees": (scored["rf_dev"] - scored["obs"], scored["rf_sd"]),
    }


# @spec EVAL-HOLD-007
def _reliability(scored: pd.DataFrame, sigma2: float, seed: int) -> pd.DataFrame:
    rows = []
    for kind, (error, sd) in _sd_kinds(scored, sigma2).items():
        for k in HALF_WIDTHS:
            c = _coverage(scored, error, sd, k, "observed", seed)
            rows.append({"sd_kind": kind, "k": k, "nominal": float(2 * norm.cdf(k) - 1),
                         "n": len(scored), "observed": c["observed"],
                         "ci_low": c["observed_ci_low"], "ci_high": c["observed_ci_high"]})
    return pd.DataFrame(rows)


# @spec EVAL-HOLD-008
def _spread_skill(scored: pd.DataFrame, seed: int) -> pd.DataFrame:
    values = scored["total_sd"].astype(float)
    edges = np.unique(np.quantile(values, np.linspace(0, 1, 6)))
    if len(edges) < 2:
        bins = pd.Series(0, index=scored.index)
        edges = np.array([values.min(), values.max()])
    else:
        bins = pd.Series(pd.cut(values, edges, labels=False, include_lowest=True),
                         index=scored.index)
    rows = []
    for b in sorted(bins.dropna().unique()):
        part = scored[bins == b]
        e = part["error"].to_numpy(dtype=float)
        rows.append(
            {
                "bin": int(b) + 1,
                "total_sd_low": float(edges[int(b)]),
                "total_sd_high": float(edges[int(b) + 1]),
                "n": len(part),
                "mean_total_sd": float(part["total_sd"].mean()),
                "rms_total_sd": float(np.sqrt(part["total_variance"].mean())),
                **_with_ci("rmse", float(np.sqrt((e**2).mean())), _rmse_ci(part, e, seed)),
                **_with_ci("bias", float(e.mean()), _mean_ci(part, e, seed)),
                "cov1_total": float(_covered(e, part["total_sd"], 1).mean()),
            }
        )
    return pd.DataFrame(rows)


def _group_table(scored: pd.DataFrame, groups: pd.Series, order, seed: int) -> pd.DataFrame:
    rows = []
    for group in order:
        part = scored[groups == group]
        if not len(part):
            continue
        e = part["error"].to_numpy(dtype=float)
        monitors = sorted(part["site_id"].unique())
        rows.append(
            {
                "group": group,
                "monitors": ";".join(monitors),
                "n_monitors": len(monitors),
                "n": len(part),
                "mean_sd": float(part["sd"].mean()),
                "mean_total_sd": float(part["total_sd"].mean()),
                **_with_ci("mae", float(np.abs(e).mean()), _mean_ci(part, np.abs(e), seed)),
                **_with_ci("rmse", float(np.sqrt((e**2).mean())), _rmse_ci(part, e, seed)),
            }
        )
    return pd.DataFrame(rows)


def distance_bins(km: pd.Series) -> pd.Series:
    """Each value's ``nearest_sensor_km`` bin: ``<0.75``, ``0.75–1.25``, ``1.25–2``, ``≥2``."""
    return pd.cut(km, DISTANCE_EDGES, right=False, labels=DISTANCE_LABELS).astype(str)


def local_hour(hours: pd.Series) -> pd.Series:
    return pd.to_datetime(hours, utc=True).dt.tz_convert(LOCAL_TZ).dt.hour


# @spec EVAL-HOLD-009
def _by_distance(scored: pd.DataFrame, seed: int) -> pd.DataFrame:
    return _group_table(scored, distance_bins(scored["nearest_sensor_km"]), DISTANCE_LABELS, seed)


# @spec EVAL-HOLD-009
def _by_local_hour(scored: pd.DataFrame, seed: int) -> pd.DataFrame:
    hours = local_hour(scored["hour"])
    return _group_table(scored, hours, sorted(hours.unique()), seed)


# @spec EVAL-HOLD-010
def _by_monitor(scored: pd.DataFrame, sigma2: float, seed: int) -> pd.DataFrame:
    rows = []
    for site_id in sorted(scored["site_id"].unique()):
        part = scored[scored["site_id"] == site_id]
        row = {
            "site_id": site_id,
            "name": part["name"].iloc[0],
            "mean_nearest_sensor_km": float(part["nearest_sensor_km"].mean()),
            **_accuracy(part, seed),
        }
        for kind, (error, sd) in _sd_kinds(part, sigma2).items():
            if kind == "rf_trees":
                continue
            for k in (1, 2):
                row.update(_coverage(part, error, sd, k, f"cov{k}_{kind}", seed))
        rows.append(row)
    table = pd.DataFrame(rows)
    first = ["site_id", "name", "n", "mean_nearest_sensor_km"]
    return table[first + [c for c in table if c not in first and c not in ("mae", "mae_ci_low",
                                                                            "mae_ci_high")]]


# @spec EVAL-HOLD-011
def typical_hour(scored: pd.DataFrame) -> pd.Timestamp | None:
    """The hour whose median scored ``total_sd`` is closest to the median of that over the
    regular-variogram hours (every hour when none is regular); the earliest on a tie."""
    if not len(scored):
        return None
    pool = scored[scored["variogram"] == "regular"]
    pool = pool if len(pool) else scored
    medians = pool.groupby("hour")["total_sd"].median()
    gaps = (medians - medians.median()).abs()
    return gaps[gaps == gaps.min()].index.min()


def _scored_frame(frame: pd.DataFrame, prediction: pd.Series) -> pd.DataFrame:
    scored = pd.DataFrame(
        {
            "hour": frame["hour"].to_numpy(),
            "obs": frame["obs"].to_numpy(dtype=float),
            "surface": prediction.to_numpy(dtype=float),
        }
    )
    scored["error"] = scored["surface"] - scored["obs"]
    scored["z"] = np.nan
    scored["z_total"] = np.nan
    scored["inputs_reproduced"] = False
    return scored


# @spec EVAL-HOLD-015
def _methods(matchups: pd.DataFrame, scored: pd.DataFrame, seed: int) -> pd.DataFrame:
    unflagged = matchups[~matchups["flagged"]].reset_index(drop=True)
    kriging_abs = (unflagged["surface"] - unflagged["obs"]).abs().to_numpy()
    rows = []
    for method, column in METHODS.items():
        frame = _scored_frame(unflagged, unflagged[column])
        m = matchup_metrics(frame)
        ci = metric_intervals(frame, seed)
        row = {"method": method, "note": REFERENCE_NOTE if method == "rf_in_window" else ""}
        row["n"] = m["n"]
        for metric in ("bias", "mae", "rmse", "r2", "pearson_r"):
            row[metric] = m[metric]
            if metric == "mae":
                lo, hi = _mean_ci(frame, frame["error"].abs(), seed)
            else:
                lo, hi = ci[f"{metric}_ci_low"], ci[f"{metric}_ci_high"]
            row[f"{metric}_ci_low"], row[f"{metric}_ci_high"] = lo, hi
        if method == "kriging":
            row.update({"paired_mean": math.nan, "paired_ci_low": math.nan,
                        "paired_ci_high": math.nan})
        else:
            d = kriging_abs - frame["error"].abs().to_numpy()
            row["paired_mean"] = float(d.mean())
            row["paired_ci_low"], row["paired_ci_high"] = _mean_ci(frame, d, seed)
        if method == "rf_dev":
            error = scored["rf_dev"] - scored["obs"]
            row["cov_n"] = len(scored)
            for k in (1, 2):
                row.update(_coverage(scored, error, scored["rf_sd"], k, f"cov{k}", seed))
        rows.append(row)
    columns = ["method", "note", "n"]
    for metric in ("bias", "mae", "rmse", "r2", "pearson_r"):
        columns += [metric, f"{metric}_ci_low", f"{metric}_ci_high"]
    columns += ["paired_mean", "paired_ci_low", "paired_ci_high", "cov_n", "cov1", "cov1_ci_low",
                "cov1_ci_high", "cov2", "cov2_ci_low", "cov2_ci_high"]
    return pd.DataFrame(rows).reindex(columns=columns)


# --- Prediction examples ------------------------------------------------------------------------


# @spec EVAL-HOLD-017
def lower_median_key(values: dict):
    """The key of the lower median value, the earliest key on a tie."""
    ordered = sorted(values, key=lambda key: (values[key], key))
    return ordered[(len(ordered) - 1) // 2]


def _local_day_hours(date) -> list[pd.Timestamp]:
    """UTC hours of the local date's every hour and the next date's 00:00."""
    first = pd.Timestamp(date).tz_localize(LOCAL_TZ)
    nxt = pd.Timestamp(date + timedelta(days=1)).tz_localize(LOCAL_TZ)
    return list(pd.date_range(first, nxt, freq="h", inclusive="both").tz_convert("UTC"))


# @spec EVAL-HOLD-017
def example_day(kriged: set, matchups: pd.DataFrame):
    """The qualifying local date with the lower-median kriging RMSE, or None."""
    dates = sorted({pd.Timestamp(h).tz_convert(LOCAL_TZ).date() for h in kriged})
    qualifying = [d for d in dates if all(h in kriged for h in _local_day_hours(d))]
    unflagged = matchups[~matchups["flagged"]]
    local_date = unflagged["hour"].dt.tz_convert(LOCAL_TZ).dt.date
    rmse = {}
    for d in qualifying:
        e = unflagged.loc[local_date == d, "error"]
        if len(e):
            rmse[d] = float(np.sqrt((e**2).mean()))
    return lower_median_key(rmse) if rmse else None


def _example_monitors(matchups: pd.DataFrame, date) -> list[str]:
    day = matchups[matchups["hour"].dt.tz_convert(LOCAL_TZ).dt.date == date]
    counts = day[~day["flagged"]].groupby("site_id").size()
    eligible = counts[counts >= MIN_EXAMPLE_MATCHUPS].index
    distance = day[day["site_id"].isin(eligible)].groupby("site_id")["nearest_sensor_km"].mean()
    ordered = sorted(distance.index, key=lambda s: (distance[s], s))
    if len(ordered) <= 3:
        return ordered
    return [ordered[0], ordered[(len(ordered) - 1) // 2], ordered[-1]]


# @spec EVAL-HOLD-018
def _example_series(matchups: pd.DataFrame, date, monitors: list[str], sigma2: float):
    day = matchups[
        (matchups["hour"].dt.tz_convert(LOCAL_TZ).dt.date == date)
        & matchups["site_id"].isin(monitors)
    ].sort_values(["site_id", "hour"])
    oos = np.sqrt(day["total_variance"] + sigma2)
    return pd.DataFrame(
        {
            "hour": day["hour"],
            "local_time": day["hour"].dt.tz_convert(LOCAL_TZ).dt.strftime("%Y-%m-%d %H:%M %Z"),
            "site_id": day["site_id"],
            "name": day["name"],
            "obs": day["obs"],
            "flagged": day["flagged"],
            "surface": day["surface"],
            "total_sd": day["total_sd"],
            "oos_sd": oos,
            "lower_oos": day["surface"] - 2 * oos,
            "upper_oos": day["surface"] + 2 * oos,
            "lower_total": day["surface"] - 2 * day["total_sd"],
            "upper_total": day["surface"] + 2 * day["total_sd"],
        }
    ).reset_index(drop=True)


def _grid(surface) -> Grid:
    height, width = surface.pm25.shape
    return Grid(
        x_min=surface.transform.c,
        y_max=surface.transform.f,
        width=width,
        height=height,
        resolution_m=surface.transform.a,
    )


# --- Reproducibility ---------------------------------------------------------------------------


def _secret_name(name: str) -> bool:
    upper = str(name).upper()
    return any(word in upper for word in _SECRET_WORDS)


def _redact_url(value: str) -> str:
    if "://" not in value:
        return value
    try:
        parts = urlsplit(value)
        password = parts.password
    except ValueError:
        return value
    if not password:
        return value
    netloc = parts.netloc.replace(f":{password}@", f":{REDACTED}@", 1)
    return urlunsplit(parts._replace(netloc=netloc))


# @spec EVAL-HOLD-019
def redact(values):
    """``values`` with every entry whose name names a secret replaced by ``"<redacted>"`` and
    every URL's password masked, recursively."""
    if isinstance(values, dict):
        return {
            k: (REDACTED if _secret_name(k) and v not in (None, "") else redact(v))
            for k, v in values.items()
        }
    if isinstance(values, list):
        return [redact(v) for v in values]
    if isinstance(values, str):
        return _redact_url(values)
    return values


def _versions() -> dict:
    out = {}
    for name, dist in VERSIONED.items():
        try:
            out[name] = version(dist)
        except PackageNotFoundError:
            out[name] = None
    out["python"] = platform.python_version()
    return out


# @spec EVAL-HOLD-019
def _run_config(archive_uri, dev, test, settings, folds, grid, choice) -> dict:
    import os

    calibration = CalibrationSettings()
    fusion = FusionSettings()
    return redact(
        {
            "command": list(sys.argv),
            "dev": {"start": dev[0].isoformat(), "end": dev[1].isoformat()},
            "test": {"start": test[0].isoformat(), "end": test[1].isoformat()},
            "archive_uri": archive_uri,
            "environment": {k: v for k, v in sorted(os.environ.items()) if k.startswith("AQDT_")},
            "settings": {
                "evaluation": settings.model_dump(mode="json"),
                "calibration": calibration.model_dump(mode="json"),
                "fusion": fusion.model_dump(mode="json"),
                "maps": MapSettings().model_dump(mode="json"),
            },
            "forest": {
                "features": ["x", "y", "hour_of_day (UTC)", "hour median of the kriging points"],
                "grid": forest.configurations(),
                "grid_axes": forest.GRID,
                "folds": folds,
                "grouping": "GroupKFold by UTC date of the hour",
                "score": "RMSE at the held-out folds' kriging points",
                "selection_rule": forest.SELECTION_RULE,
                "chosen": choice,
                "random_state": settings.seed,
                "grid_mean_rmse": grid[["n_estimators", "min_samples_leaf", "max_features",
                                        "mean_rmse"]].to_dict("records"),
            },
            # as fusion's select_variogram fits each candidate (FUS-VAR)
            "variogram": {
                "candidates": list(CANDIDATES),
                "library": "PyKrige OrdinaryKriging",
                "nlags": 6,
                "weight": False,
                "coordinates_type": "euclidean",
                "selection": "smallest cross-validation cR; ties to the earlier candidate",
                "min_points": fusion.min_points,
                "resolution_m": fusion.resolution_m,
            },
            "calibration_rules": {
                "correction": {
                    "formula": "pm25_corrected = slope·(A + B)/2 − humidity·RH + intercept",
                    "slope": CORRECTION_SLOPE,
                    "humidity": CORRECTION_HUMIDITY,
                    "intercept": CORRECTION_INTERCEPT,
                },
                "pairing_distance_m": calibration.max_distance_m,
                "window_days": calibration.window_days,
                "min_pairs": calibration.min_pairs,
                "fit": "OLS of the monitor's trusted value on the sensor's corrected hourly mean",
                "slope_gate": "a fit needs at least min_pairs pairs, a non-constant input, and a "
                "slope > 0",
                "pooled_fallback": "a sensor without an accepted own fit takes the pooled OLS fit "
                "over the accepted sensors' pairs, or is uncalibrated when that fails the gate",
                "apply": "pm25_calibrated = max(0, intercept + slope · pm25_corrected_mean) from "
                "the latest fit at or before the hour",
            },
            "seed": settings.seed,
            "shap_sample": forest.SHAP_SAMPLE,
            "reliability_half_widths": HALF_WIDTHS,
            "versions": _versions(),
            **report.git_state(),
        }
    )


# @spec EVAL-HOLD-022
def _pack(out: Path) -> Path:
    archive = out.parent / f"{out.name}.tgz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(out, arcname=out.name)
    return archive


# --- Entry point -------------------------------------------------------------------------------


# @spec EVAL-HOLD-001, EVAL-HOLD-002, EVAL-HOLD-003, EVAL-HOLD-020, EVAL-HOLD-023
def holdout(
    archive_uri: str | None,
    dev,
    test,
    out_dir: str | Path,
    settings: EvaluationSettings,
    *,
    pack: bool = False,
) -> HoldoutSummary:
    """Evaluate ``dev`` and ``test`` into ``out_dir/dev`` and ``out_dir/test``, then estimate on
    the development window alone and score on the test window. Reads the archive only."""
    dev, test = _window(dev, "development"), _window(test, "test")
    if test[0] < dev[1]:
        raise ValueError(
            f"the test window [{test[0].isoformat()}, {test[1].isoformat()}) must start at or "
            f"after the development window [{dev[0].isoformat()}, {dev[1].isoformat()}) ends"
        )
    archive_uri = resolve_archive_uri(archive_uri)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    seed = settings.seed
    folders = {"dev": out / "dev", "test": out / "test"}
    for name, window in (("dev", dev), ("test", test)):
        log.info("holdout: evaluating the %s window [%s, %s)", name, *window)
        evaluate(archive_uri, *window, folders[name], settings)

    sites = read_sites(archive_uri)
    monitor_sites = _monitor_sites(sites)
    matchups = {n: _read_matchups(f / "matchups.csv") for n, f in folders.items()}
    fits = {n: _fits(archive_uri, *w) for n, w in (("dev", dev), ("test", test))}
    calibrated = {n: _calibrated(archive_uri, *w) for n, w in (("dev", dev), ("test", test))}
    points = {n: _points(fits[n], calibrated[n], sites) for n in folders}
    tm = matchups["test"]

    diag = pd.read_csv(folders["dev"] / "coverage_diagnostics.csv")
    shared = diag[diag["kind"] == "z_total_shared"]
    sigma2 = float(shared["shared_variance"].iloc[0]) if len(shared) else math.nan
    sigma2_hours = int(shared["shared_hours"].iloc[0]) if len(shared) else 0
    if not math.isfinite(sigma2):
        sigma2 = 0.0

    # the development forest
    window_label = f"[{dev[0].isoformat()}, {dev[1].isoformat()})"
    grid = forest.grid_search(points["dev"], seed, window_label)
    choice = forest.choose(grid)
    model = forest.fit(points["dev"], choice, seed)
    query = tm[["hour", "site_id"]].merge(monitor_sites[["site_id", "x", "y"]], on="site_id",
                                          how="left")
    tm["rf_dev"], tm["rf_sd"] = forest.predict_with_spread(model, points["test"], query)
    rf_dev = tm[["hour", "site_id", "rf_dev", "rf_sd"]]
    scored = scored_set(tm)

    inside = {}
    for name, folder in folders.items():
        availability = pd.read_csv(folder / "availability.csv")
        inside[name] = set(availability.loc[availability["inside_grid"].astype(bool), "site_id"])
    completeness = pd.DataFrame(
        [
            _completeness(
                name,
                *window,
                fits[name],
                read_observations(archive_uri, Source.airnow, *window),
                inside[name],
                matchups[name],
                scored if name == "test" else None,
            )
            for name, window in (("dev", dev), ("test", test))
        ]
    )
    dev_vs_test = _dev_vs_test(folders, seed)
    oos = _oos_offset(scored, sigma2, sigma2_hours, seed)
    reliability = _reliability(scored, sigma2, seed)
    spread = _spread_skill(scored, seed)
    by_distance = _by_distance(scored, seed)
    by_hour = _by_local_hour(scored, seed)
    by_monitor = _by_monitor(scored, sigma2, seed)
    methods = _methods(tm, scored, seed)
    shap_X, shap_values = forest.shap_sample(model, points["test"], seed)
    importance = (
        pd.DataFrame({"feature": shap_X.columns,
                      "mean_abs_shap": shap_values.abs().mean().to_numpy()})
        .sort_values("mean_abs_shap", ascending=False, kind="stable")
        .reset_index(drop=True)
    )

    tables = {
        "completeness": completeness,
        "dev_vs_test": dev_vs_test,
        "oos_offset": oos,
        "rf_grid": grid,
        "rf_dev": rf_dev,
        "methods_test": methods,
        "reliability_coverage": reliability,
        "spread_skill": spread,
        "by_distance": by_distance,
        "by_local_hour": by_hour,
        "by_monitor": by_monitor,
        "shap_importance": importance,
    }
    for name, table in tables.items():
        table.to_csv(out / f"{name}.csv", index=False)

    written = []
    holdout_figures.reliability_coverage(reliability, out)
    holdout_figures.spread_skill(spread, out)
    holdout_figures.by_group(by_distance, out, "by_distance")
    holdout_figures.by_group(by_hour, out, "by_local_hour")
    holdout_figures.by_monitor(by_monitor, out)
    written += ["reliability_coverage", "spread_skill", "by_distance", "by_local_hour",
                "by_monitor"]

    test_fits = fits["test"].set_index("hour")
    monitors_inside = monitor_sites[monitor_sites["site_id"].isin(inside["test"])]
    typical = typical_hour(scored)
    map_info = None
    if typical is not None:
        surface = read_surface(test_fits.loc[typical, "surface_uri"])
        grid_ = _grid(surface)
        finite = np.isfinite(surface.variance).ravel()
        xs, ys = grid_.cell_centres()
        cal = np.full(grid_.n_cells, np.nan)
        cal[finite] = hour_calibration_variance(
            archive_uri, typical.to_pydatetime(), xs[finite], ys[finite]
        )
        total_sd = np.sqrt(surface.variance.ravel().astype(float) + cal).reshape(
            surface.variance.shape
        )
        sensors = hour_sensors(calibrated["test"], sites, typical.to_pydatetime())
        map_info = holdout_figures.total_sd_map(
            typical, total_sd, grid_, sensors, monitors_inside, MapSettings(), out
        )
        written.append("total_sd_map")
    holdout_figures.methods_test(methods, out)
    holdout_figures.shap_beeswarm(shap_X, shap_values, out)
    written += ["methods_test", "shap_beeswarm"]

    kriged = set(fits["test"].loc[fits["test"]["status"] == SurfaceStatus.kriged.value, "hour"])
    date = example_day(kriged, tm)
    example = None
    if date is not None:
        monitors = _example_monitors(tm, date)
        series = _example_series(tm, date, monitors, sigma2)
        holdout_figures.example_timeseries(series, out)
        hours = [pd.Timestamp(date).tz_localize(LOCAL_TZ) + pd.Timedelta(hours=h)
                 for h in EXAMPLE_HOURS]
        hours.append(pd.Timestamp(date + timedelta(days=1)).tz_localize(LOCAL_TZ))
        utc_hours = [h.tz_convert("UTC") for h in hours]
        surfaces = [read_surface(test_fits.loc[h, "surface_uri"]) for h in utc_hours]
        holdout_figures.example_maps(
            utc_hours, surfaces, _grid(surfaces[0]), points["test"], monitors_inside, out
        )
        written += ["example_timeseries", "example_maps"]
        example = {"date": date, "monitors": monitors}
    holdout_figures.write_captions(out, written)

    report_context = {
        "dev": dev,
        "test": test,
        "completeness": completeness,
        "dev_vs_test": dev_vs_test,
        "oos": oos,
        "test_overall": _overall(folders["test"]),
        "reliability": reliability,
        "spread": spread,
        "by_distance": by_distance,
        "by_monitor": by_monitor,
        "typical": typical,
        "map": map_info,
        "grid": grid,
        "choice": choice,
        "folds": forest.n_folds(points["dev"]),
        "methods": methods,
        "importance": importance,
        "example": example,
        "sigma2": sigma2,
        "sigma2_hours": sigma2_hours,
        "scored_n": len(scored),
    }
    _write_summary(out / "summary.md", report_context)
    config = _run_config(archive_uri, dev, test, settings, forest.n_folds(points["dev"]), grid,
                         choice)
    (out / "run_config.json").write_text(json.dumps(config, indent=2, sort_keys=True))
    archive = _pack(out) if pack else None
    return HoldoutSummary(dev=dev, test=test, out_dir=out, archive=archive, rf_choice=choice)


def _overall(folder: Path) -> dict:
    metrics = pd.read_csv(folder / "metrics.csv")
    overall = metrics[(metrics["subset"] == "unflagged") & (metrics["split"] == "overall")]
    return overall.iloc[0].to_dict() if len(overall) else {}


# --- Summary -----------------------------------------------------------------------------------

_PCT_STATS = {"cov1_krig", "cov2_krig", "cov1_total", "cov2_total"}
_STAT_LABELS = {
    "n": "Matchups (unflagged)",
    "bias": "Bias (µg/m³)",
    "mae": "MAE (µg/m³)",
    "rmse": "RMSE (µg/m³)",
    "r2": "R² (skill, 1 − SSE/SS_obs)",
    "pearson_r": "Pearson r",
    "cov1_krig": "±1 SD coverage, kriging SD",
    "cov2_krig": "±2 SD coverage, kriging SD",
    "cov1_total": "±1 SD coverage, total SD",
    "cov2_total": "±2 SD coverage, total SD",
    "stored_rmse": "LOMO: stored-surface RMSE (µg/m³)",
    "control_rmse": "LOMO: control re-krige RMSE (µg/m³)",
    "lomo_rmse": "LOMO: without-monitor RMSE (µg/m³)",
    "lomo_median_d": "LOMO: median d = |lomo| − |control| (µg/m³)",
}
_KIND_LABELS = {
    "kriging": "kriging only",
    "total": "total",
    "total_dev_offset": "total + D-offset",
    "rf_trees": "forest tree spread",
}


def _f(row, name: str, pct: bool = False, digits: int = 2) -> str:
    value, lo, hi = row.get(name), row.get(f"{name}_ci_low"), row.get(f"{name}_ci_high")
    return report._pct_ci(value, lo, hi) if pct else report._ci(value, lo, hi, digits)


def _window_text(w) -> str:
    return f"[{w[0]:%Y-%m-%dT%H}, {w[1]:%Y-%m-%dT%H}) UTC"


# @spec EVAL-HOLD-021
def _write_summary(path: Path, c: dict) -> None:
    lines = [
        "# Development and test windows",
        "",
        f"Development window D: {_window_text(c['dev'])}. Test window T: "
        f"{_window_text(c['test'])}. Everything estimated — the shared hourly offset, the "
        "forest's settings and fit — comes from D; T only scores. Intervals are 95% day-block "
        "bootstrap intervals over the window's UTC days. Uncertainty statistics on T use the "
        f"scored set: T's unflagged matchups with reproduced inputs and a calibration term "
        f"(n = {c['scored_n']}).",
        "",
        "## Completeness",
        "",
        "| Window | Hours | Kriged | No surface row | Not kriged | Days | Monitor hours "
        "(present / possible) | with a value | unflagged | Matchups (unflagged) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in c["completeness"].to_dict("records"):
        not_kriged = sum(v for k, v in r.items() if k.startswith("not_kriged_"))
        lines.append(
            f"| {r['window']} | {r['hours']} | {r['kriged_hours']} | {r['no_fit_row']} | "
            f"{not_kriged} | {r['days']} | {r['monitor_hours_all']} / "
            f"{r['monitor_hours_possible']} | {r['monitor_hours_valued']} | "
            f"{r['monitor_hours_unflagged']} | {r['matchups']} ({r['matchups_unflagged']}) |"
        )
    lines += ["", "## Development against test", "",
              "Both windows evaluated with the same settings; each interval is its own window's.",
              "", "| Statistic | D | T |", "|---|---|---|"]
    for r in c["dev_vs_test"].to_dict("records"):
        stat = r["statistic"]
        pct = stat in _PCT_STATS
        if stat == "n":
            dev_text, test_text = str(int(r["dev"])), str(int(r["test"]))
        else:
            dev_text = _f({k.replace("dev", "v"): v for k, v in r.items()}, "v", pct)
            test_text = _f({k.replace("test", "v"): v for k, v in r.items()}, "v", pct)
        lines.append(f"| {_STAT_LABELS[stat]} | {dev_text} | {test_text} |")

    oos = c["oos"].iloc[0].to_dict()
    overall = c["test_overall"]
    lines += [
        "",
        "## Shared offset out of sample",
        "",
        f"σ²_shared(D) = {report._num(c['sigma2'], 3)} (µg/m³)², estimated from "
        f"{c['sigma2_hours']} development hours only (EVAL-UNC-003) and added to T's total "
        "variance. On T's scored set:",
        "",
        "| SD | ±1 SD | ±2 SD |",
        "|---|---|---|",
        f"| total | {_f(overall, 'cov1_total', True)} | {_f(overall, 'cov2_total', True)} |",
        f"| total + D-offset | {_f(oos, 'cov1', True)} | {_f(oos, 'cov2', True)} |",
        "",
        "Nominal: 68.3% and 95.4%.",
        "",
        "## Reliability",
        "",
        "Observed central coverage against nominal on T's scored set.",
        "",
        "| Nominal | " + " | ".join(_KIND_LABELS[k] for k in SD_KINDS) + " |",
        "|---|" + "---|" * len(SD_KINDS),
    ]
    rel = c["reliability"]
    for k in HALF_WIDTHS:
        cells = []
        for kind in SD_KINDS:
            r = rel[(rel["sd_kind"] == kind) & (rel["k"] == k)].iloc[0]
            cells.append(report._pct_ci(r["observed"], r["ci_low"], r["ci_high"]))
        nominal = rel[rel["k"] == k]["nominal"].iloc[0]
        lines.append(f"| {report._pct(nominal)} (±{k:g} SD) | " + " | ".join(cells) + " |")

    lines += ["", "## Where and when the uncertainty is large", "", "**Spread–skill** (quintiles "
              "of total SD):", "", "| Bin | n | mean total SD | RMS total SD | RMSE | bias | "
              "±1 SD coverage |", "|---|---|---|---|---|---|---|"]
    for r in c["spread"].to_dict("records"):
        lines.append(
            f"| {r['total_sd_low']:.2f}–{r['total_sd_high']:.2f} | {r['n']} | "
            f"{r['mean_total_sd']:.2f} | {r['rms_total_sd']:.2f} | {_f(r, 'rmse')} | "
            f"{_f(r, 'bias')} | {report._pct(r['cov1_total'])} |"
        )
    distance = c["by_distance"]
    counts = distance["n_monitors"].tolist() if len(distance) else []
    span = (f"{min(counts)}–{max(counts)}" if counts and min(counts) != max(counts)
            else (str(counts[0]) if counts else "0"))
    lines += ["", "**Distance to the nearest sensor:**", "",
              "| Distance (km) | Monitors | n | mean kriging SD | mean total SD | MAE | RMSE |",
              "|---|---|---|---|---|---|---|"]
    for r in distance.to_dict("records"):
        lines.append(
            f"| {r['group']} | {r['n_monitors']} | {r['n']} | {r['mean_sd']:.2f} | "
            f"{r['mean_total_sd']:.2f} | {_f(r, 'mae')} | {_f(r, 'rmse')} |"
        )
    lines += [
        "",
        f"Each distance bin holds only {span} monitors, and a monitor's nearest sensor barely "
        "moves between hours, so the distance result is indicative: it cannot separate distance "
        "from the monitors' other differences.",
        "",
        "**Per monitor:**",
        "",
        "| Monitor | n | km to sensor | RMSE | ±1 / ±2 SD kriging | ±1 / ±2 SD total | "
        "±1 / ±2 SD total + D-offset |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in c["by_monitor"].to_dict("records"):
        lines.append(
            f"| {r['name']} | {r['n']} | {r['mean_nearest_sensor_km']:.2f} | {_f(r, 'rmse')} | "
            f"{report._pct(r['cov1_kriging'])} / {report._pct(r['cov2_kriging'])} | "
            f"{report._pct(r['cov1_total'])} / {report._pct(r['cov2_total'])} | "
            f"{report._pct(r['cov1_total_dev_offset'])} / "
            f"{report._pct(r['cov2_total_dev_offset'])} |"
        )
    if c["typical"] is not None and c["map"] is not None:
        m = c["map"]
        lines += [
            "",
            f"Total-SD map: the typical hour is {c['typical']:%Y-%m-%dT%H} UTC (median scored "
            f"total SD closest to the median over the hours); its maximum total SD is "
            f"{m['max_total_sd']:.2f} µg/m³, {report._pct(m['frac_above_sd_max'])} of cells above "
            f"the map's sd_max of {m['sd_max']:g} µg/m³.",
        ]

    grid, choice = c["grid"], c["choice"]
    chosen = grid[grid["chosen"]].iloc[0]
    lines += [
        "",
        "## Chosen forest",
        "",
        f"Twelve configurations scored by GroupKFold over UTC days on D ({c['folds']} folds), "
        "at the held-out folds' kriging points. Chosen: "
        f"n_estimators = {choice['n_estimators']}, min_samples_leaf = "
        f"{choice['min_samples_leaf']}, max_features = {choice['max_features']:g}, mean fold "
        f"RMSE {chosen['mean_rmse']:.3f} µg/m³ (SD {chosen['sd_rmse']:.3f}). Full grid in "
        "rf_grid.csv.",
        "",
        "## Test-window methods",
        "",
        "Unflagged T matchups. Paired = mean |kriging error| − |method error| (negative: kriging "
        "closer).",
        "",
        "| Method | n | bias | MAE | RMSE | R² (skill, 1 − SSE/SS_obs) | r | paired |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in c["methods"].to_dict("records"):
        name = r["method"] + (" (reference only, trained on the hours it is scored on)"
                              if r["method"] == "rf_in_window" else "")
        lines.append(
            f"| {name} | {int(r['n'])} | {_f(r, 'bias')} | {_f(r, 'mae')} | {_f(r, 'rmse')} | "
            f"{_f(r, 'r2')} | {_f(r, 'pearson_r')} | "
            f"{report._ci(r['paired_mean'], r['paired_ci_low'], r['paired_ci_high'])} |"
        )
    rf = c["methods"].set_index("method").loc["rf_dev"].to_dict()
    lines += [
        "",
        f"Forest tree spread as an SD on the scored set: ±1 SD {_f(rf, 'cov1', True)}, ±2 SD "
        f"{_f(rf, 'cov2', True)}.",
        "",
        "## SHAP",
        "",
        "Mean |SHAP| of the chosen forest on a seeded sample of T's kriging points (µg/m³): "
        + ", ".join(f"{r['feature']} {r['mean_abs_shap']:.2f}"
                    for r in c["importance"].to_dict("records"))
        + ".",
        "",
        "## Prediction examples",
        "",
    ]
    if c["example"] is None:
        lines.append("No local date in T has every hour and the next midnight kriged; no example "
                     "figure is written.")
    else:
        lines.append(
            f"Example day {c['example']['date']} ({LOCAL_TZ}), monitors "
            f"{', '.join(c['example']['monitors'])} (nearest, median, and farthest from a sensor). "
            "The band is the out-of-sample prediction interval ±2 √(total variance + "
            "σ²_shared(D)), with ±2 total SD as the lighter inner band."
        )
    lines += [
        "",
        "## Caveats",
        "",
        "- The forest's tree spread measures disagreement among trees; it is not a predictive "
        "variance and omits the noise in the target, so it is expected to be narrow.",
        f"- Each distance bin holds only {span} monitors, so the distance result is indicative.",
        "- rf_in_window is a reference only: it is trained on the hours it is scored on.",
        "- T's calibration fits are trained over trailing windows that reach into D, and the "
        "monitors are calibration targets for nearby sensors in both windows.",
        "- AirNow data are preliminary and can be revised after the run.",
        "",
    ]
    path.write_text("\n".join(lines))
