"""Diagnostics beside the headline scores: conditional bias, coverage diagnostics, pooling with
the day held fixed, per-monitor scores, variogram classes, timing lags, and flag rates."""

from __future__ import annotations

import numpy as np
import pandas as pd

from aqdt.evaluation.bootstrap import block_interval, days, mean_interval, sums_interval
from aqdt.evaluation.metrics import matchup_metrics

RELIABILITY_COLUMNS = ["bin", "n", "surface_mean", "obs_mean", "obs_ci_low", "obs_ci_high"]
SLOPE_COLUMNS = ["slope", "estimate", "ci_low", "ci_high", "n"]
Z_KINDS = ["z", "z_total_raw", "z_total", "z_total_shared"]
SPLIT_KINDS = ["z_total_shared_odd_on_even", "z_total_shared_even_on_odd"]
COVERAGE_COLUMNS = [
    "kind", "n", "cov1", "cov1_ci_low", "cov1_ci_high", "cov2", "cov2_ci_low", "cov2_ci_high",
    "k68", "k68_ci_low", "k68_ci_high", "k95", "k95_ci_low", "k95_ci_high", "mean", "sd",
    "shared_variance", "shared_hours",
]
POOLING_COLUMNS = ["quantity", "estimate", "ci_low", "ci_high", "n"]
SHARE_QUANTILES = (("share_p25", 0.25), ("share_p50", 0.50), ("share_p75", 0.75))
VARIOGRAM_CLASSES = ["pure_nugget", "short_range", "regular"]
PERCENTILES = (5, 25, 50, 75, 95)
LAGS = (-1, 0, 1)
LAG_COLUMNS = ["lag", "n", "pearson_r", "rmse", "rmse_difference", "rmse_difference_ci_low",
               "rmse_difference_ci_high"]


# --- Conditional bias -------------------------------------------------------------------------


# @spec EVAL-COND-001
def reliability(frame: pd.DataFrame, seed: int, bins: int = 10) -> pd.DataFrame:
    """Deciles of ``surface``: count, mean surface, mean obs and its day-bootstrap interval."""
    if len(frame) < bins:
        return pd.DataFrame(columns=RELIABILITY_COLUMNS)
    decile = pd.qcut(frame["surface"].rank(method="first"), bins, labels=False).to_numpy()
    blocks = days(frame["hour"])
    rows = []
    for b in range(bins):
        mask = decile == b
        part = frame[mask]
        lo, hi = mean_interval(blocks[mask], part["obs"], seed)
        rows.append(
            {
                "bin": b + 1,
                "n": int(mask.sum()),
                "surface_mean": float(part["surface"].mean()),
                "obs_mean": float(part["obs"].mean()),
                "obs_ci_low": lo,
                "obs_ci_high": hi,
            }
        )
    return pd.DataFrame(rows, columns=RELIABILITY_COLUMNS)


def _slope_from_sums(t: np.ndarray) -> np.ndarray:
    n, sx, sy, sxx, sxy = (t[:, k] for k in range(5))
    return (sxy - sx * sy / n) / (sxx - sx**2 / n)


# @spec EVAL-COND-002
def slopes(frame: pd.DataFrame, seed: int) -> pd.DataFrame:
    """OLS slope of obs on surface (the reliability slope) and of surface on obs, with
    day-bootstrap intervals."""
    blocks = days(frame["hour"]) if len(frame) else np.array([])
    rows = []
    for name, x, y in (("obs_on_surface", "surface", "obs"), ("surface_on_obs", "obs", "surface")):
        xv = frame[x].to_numpy(dtype=float)
        yv = frame[y].to_numpy(dtype=float)
        data = np.column_stack([np.ones_like(xv), xv, yv, xv**2, xv * yv])
        estimate, lo, hi = np.nan, np.nan, np.nan
        if len(xv) > 1:
            with np.errstate(divide="ignore", invalid="ignore"):
                estimate = float(_slope_from_sums(data.sum(axis=0)[None, :])[0])
            lo, hi = sums_interval(blocks, data, _slope_from_sums, seed)
        rows.append({"slope": name, "estimate": estimate, "ci_low": lo, "ci_high": hi,
                     "n": len(xv)})
    return pd.DataFrame(rows, columns=SLOPE_COLUMNS)


# @spec EVAL-COND-003
def smoothing_verdict(ci_low: float, ci_high: float) -> str:
    """The reliability slope's verdict: above 1 over-smoothed, below 1 over-dispersed."""
    if ci_low > 1:
        return "over-smoothed"
    if ci_high < 1:
        return "over-dispersed"
    return "not distinguishable from calibrated"


# @spec EVAL-COND-003
def clean_dirty_holds(low_band_ci_low: float, high_band_ci_high: float) -> bool:
    """Whether the surface reads high in clean air and low in dirty air: the lowest predicted
    band's bias interval above 0 and the highest's below 0."""
    return bool(low_band_ci_low > 0 and high_band_ci_high < 0)


# --- Coverage diagnostics ---------------------------------------------------------------------


def _finite(z) -> np.ndarray:
    z = np.asarray(z, dtype=float)
    return z[np.isfinite(z)]


# @spec EVAL-UNC-002
def inflation_factors(z) -> tuple[float, float]:
    """The variance multipliers that would make ±1 SD and ±2 SD coverage nominal."""
    az = np.abs(_finite(z))
    if not len(az):
        return (np.nan, np.nan)
    return (float(np.quantile(az, 0.683) ** 2), float((np.quantile(az, 0.954) / 2) ** 2))


def _with_shared(frame: pd.DataFrame, z_total: pd.Series, shared_variance: float) -> pd.Series:
    """``error / √(total_variance + σ²_shared)`` where ``z_total`` is defined."""
    total_variance = (frame["error"] / z_total) ** 2
    shared = 0.0 if not np.isfinite(shared_variance) else shared_variance
    z_shared = np.where(z_total == 0, 0.0, frame["error"] / np.sqrt(total_variance + shared))
    return pd.Series(np.where(z_total.notna(), z_shared, np.nan), index=frame.index)


def _odd_day(hours) -> np.ndarray:
    return pd.to_datetime(pd.Series(hours), utc=True).dt.day.to_numpy() % 2 == 1


def _kinds(frame: pd.DataFrame, shared_variance: float, residuals: pd.DataFrame | None):
    """Each z kind with the σ²_shared and hour count it was scored with (null when none)."""
    reproduced = frame["inputs_reproduced"].astype(bool)
    z_total = frame["z_total"].where(reproduced)
    kinds = {
        "z": (frame["z"], np.nan, np.nan),
        "z_total_raw": (frame["z_total_raw"].where(reproduced), np.nan, np.nan),
        "z_total": (z_total, np.nan, np.nan),
        "z_total_shared": (_with_shared(frame, z_total, shared_variance), shared_variance, np.nan),
    }
    if residuals is None:
        return kinds
    odd_rows = _odd_day(frame["hour"]) if len(frame) else np.zeros(0, dtype=bool)
    odd_residuals = (
        _odd_day(residuals["hour"]) if len(residuals) else np.zeros(0, dtype=bool)
    )
    for kind, estimate_on_odd in zip(SPLIT_KINDS, (True, False)):
        sigma2, hours = shared_offset(residuals[odd_residuals == estimate_on_odd])
        scored = odd_rows != estimate_on_odd
        z = _with_shared(frame, z_total, sigma2).where(scored)
        kinds[kind] = (z, sigma2, hours)
    return kinds


def _coverage(zv: np.ndarray, bound: float) -> float:
    return float(np.mean(np.abs(zv) <= bound)) if len(zv) else np.nan


# @spec EVAL-UNC-002, EVAL-UNC-003, EVAL-UNC-004, EVAL-UNC-005
def coverage_diagnostics(
    frame: pd.DataFrame, seed: int, shared_variance: float,
    residuals: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Per z kind (kriging only, total uninflated, total, total with the shared offset, and, given
    the calibration residuals, the shared offset estimated on odd days and scored on even days and
    the reverse): the two coverages and the inflation factors with day-bootstrap intervals, and
    z's mean and SD."""
    rows = []
    for kind, (z, sigma2, hours) in _kinds(frame, shared_variance, residuals).items():
        values = z.to_numpy(dtype=float)
        keep = np.isfinite(values)
        zv = values[keep]
        blocks = days(frame.loc[keep, "hour"]) if keep.any() else np.array([])
        k68, k95 = inflation_factors(zv)
        k68_ci = block_interval(blocks, lambda idx, zv=zv: inflation_factors(zv[idx])[0], seed)
        k95_ci = block_interval(blocks, lambda idx, zv=zv: inflation_factors(zv[idx])[1], seed)
        cov1_ci = mean_interval(blocks, (np.abs(zv) <= 1).astype(float), seed)
        cov2_ci = mean_interval(blocks, (np.abs(zv) <= 2).astype(float), seed)
        rows.append(
            {
                "kind": kind,
                "n": len(zv),
                "cov1": _coverage(zv, 1),
                "cov1_ci_low": cov1_ci[0],
                "cov1_ci_high": cov1_ci[1],
                "cov2": _coverage(zv, 2),
                "cov2_ci_low": cov2_ci[0],
                "cov2_ci_high": cov2_ci[1],
                "k68": k68,
                "k68_ci_low": k68_ci[0],
                "k68_ci_high": k68_ci[1],
                "k95": k95,
                "k95_ci_low": k95_ci[0],
                "k95_ci_high": k95_ci[1],
                "mean": float(zv.mean()) if len(zv) else np.nan,
                "sd": float(zv.std(ddof=1)) if len(zv) > 1 else np.nan,
                "shared_variance": sigma2,
                "shared_hours": hours,
            }
        )
    return pd.DataFrame(rows, columns=COVERAGE_COLUMNS)


# @spec EVAL-UNC-003
def shared_offset(residuals: pd.DataFrame) -> tuple[float, int]:
    """The variance across hours of the hour's mean calibration residual, less the part its
    independent residuals contribute (floored at 0), and the number of hours with at least two
    residuals."""
    grouped = residuals.groupby("hour")["r"]
    stats = pd.DataFrame({"n": grouped.count(), "mean": grouped.mean(), "var": grouped.var()})
    stats = stats[stats["n"] >= 2]
    if len(stats) < 2:
        return (np.nan, len(stats))
    value = stats["mean"].var() - (stats["var"] / stats["n"]).mean()
    return (float(max(0.0, value)), len(stats))


# --- Pooling within the hour ------------------------------------------------------------------


def _dummies(labels: np.ndarray, drop_first: bool) -> np.ndarray:
    values, inverse = np.unique(labels, return_inverse=True)
    out = np.zeros((len(labels), len(values)))
    out[np.arange(len(labels)), inverse] = 1.0
    return out[:, 1:] if drop_first else out


def _coefficient(y: np.ndarray, x: np.ndarray, effects: list[np.ndarray]) -> float:
    design = np.column_stack([x, *effects])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    return float(beta[0])


# @spec EVAL-POOL-001, EVAL-POOL-002
def pooling_regression(frame: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Over the unflagged matchups with a pooled share: OLS of ``error`` on
    ``pooled_share_10km`` with day fixed effects, and with day and hour-of-day fixed effects,
    with day-cluster intervals; the raw error difference between matchups above the median share
    and the rest, with its interval; and the share's mean, quartiles, and fraction exactly 0."""
    rows = frame[~frame["flagged"].astype(bool) & frame["pooled_share_10km"].notna()]
    hours = pd.to_datetime(rows["hour"], utc=True)
    day = days(rows["hour"])
    hod = hours.dt.hour.to_numpy()
    y = rows["error"].to_numpy(dtype=float)
    x = rows["pooled_share_10km"].to_numpy(dtype=float)
    out = []
    for model, with_hod in (("day", False), ("day+hour_of_day", True)):
        if len(rows) < 3:
            out.append({"quantity": model, "estimate": np.nan, "ci_low": np.nan,
                        "ci_high": np.nan, "n": len(rows)})
            continue

        def fit(idx, with_hod=with_hod):
            effects = [_dummies(day[idx], False)]
            if with_hod:
                effects.append(_dummies(hod[idx], True))
            return _coefficient(y[idx], x[idx], effects)

        all_rows = np.arange(len(rows))
        lo, hi = block_interval(day, fit, seed)
        out.append({"quantity": model, "estimate": fit(all_rows), "ci_low": lo, "ci_high": hi,
                    "n": len(rows)})
    out.append({"quantity": "raw_high_minus_low", **_raw_difference(x, y, day, seed),
                "n": len(rows)})
    for quantity, value in _share_distribution(x):
        out.append({"quantity": quantity, "estimate": value, "ci_low": np.nan, "ci_high": np.nan,
                    "n": len(rows)})
    return pd.DataFrame(out, columns=POOLING_COLUMNS)


def _raw_difference(x: np.ndarray, y: np.ndarray, day: np.ndarray, seed: int) -> dict:
    """Mean error above the median share minus mean error at or below it."""
    if not len(x):
        return {"estimate": np.nan, "ci_low": np.nan, "ci_high": np.nan}
    high = (x > np.median(x)).astype(float)
    data = np.column_stack([high, high * y, 1 - high, (1 - high) * y])

    def difference(t: np.ndarray) -> np.ndarray:
        return t[:, 1] / t[:, 0] - t[:, 3] / t[:, 2]

    with np.errstate(divide="ignore", invalid="ignore"):
        estimate = float(difference(data.sum(axis=0)[None, :])[0])
    lo, hi = sums_interval(day, data, difference, seed)
    return {"estimate": estimate, "ci_low": lo, "ci_high": hi}


def _share_distribution(x: np.ndarray) -> list[tuple[str, float]]:
    if not len(x):
        return [(name, np.nan) for name in
                ("share_mean", *(q for q, _ in SHARE_QUANTILES), "share_zero")]
    return [
        ("share_mean", float(x.mean())),
        *((name, float(np.quantile(x, q))) for name, q in SHARE_QUANTILES),
        ("share_zero", float(np.mean(x == 0))),
    ]


# --- Per monitor, variograms, lags, flag rates ------------------------------------------------


# @spec EVAL-MON-001
def per_monitor(matchups: pd.DataFrame, availability: pd.DataFrame) -> pd.DataFrame:
    """Per monitor: accuracy and coverage over its unflagged matchups, its flagged share, its
    calibration targets, and the mean distance to its nearest sensor."""
    rows = []
    for site_id, frame in matchups.groupby("site_id", sort=True):
        m = matchup_metrics(frame[~frame["flagged"].astype(bool)])
        rows.append(
            {
                "site_id": site_id,
                **{k: m[k] for k in ("n", "bias", "mae", "rmse", "pearson_r", "cov1_krig",
                                     "cov2_krig", "cov1_total", "cov2_total")},
                "flagged_share": float(frame["flagged"].astype(bool).mean()),
                "mean_nearest_sensor_km": float(frame["nearest_sensor_km"].mean()),
            }
        )
    table = pd.DataFrame(rows)
    if table.empty:
        return pd.DataFrame(
            columns=["site_id", "n", "bias", "mae", "rmse", "pearson_r", "cov1_krig", "cov2_krig",
                     "cov1_total", "cov2_total", "flagged_share", "calibration_targets",
                     "median_target_distance_m", "mean_nearest_sensor_km"]
        )
    targets = availability[["site_id", "calibration_targets", "median_target_distance_m"]]
    return table.merge(targets, on="site_id", how="left")


# @spec EVAL-VARIO-001
def variogram_class(model: str, sill: float, nugget: float, range_m: float) -> str:
    """``pure_nugget``, ``short_range`` (exponential, range under 1 km), or ``regular``."""
    if sill - nugget < 0.01 * sill:
        return "pure_nugget"
    if model == "exponential" and range_m < 1000:
        return "short_range"
    return "regular"


# @spec EVAL-VARIO-001
def variogram_table(fits: pd.DataFrame) -> pd.DataFrame:
    """Counts and shares of each variogram class, counts of each model, range percentiles."""
    classes = [
        variogram_class(r.variogram_model, r.sill, r.nugget, r.range_m)
        for r in fits.itertuples(index=False)
    ]
    n = len(classes)
    rows = []
    for name in VARIOGRAM_CLASSES:
        count = classes.count(name)
        rows.append(("class_count", name, count))
        rows.append(("class_share", name, count / n if n else np.nan))
    for name, count in fits["variogram_model"].value_counts().sort_index().items():
        rows.append(("model_count", name, int(count)))
    ranges = fits["range_m"].to_numpy(dtype=float)
    for p in PERCENTILES:
        rows.append(("range_m_percentile", f"p{p}",
                     float(np.percentile(ranges, p)) if n else np.nan))
    return pd.DataFrame(rows, columns=["quantity", "name", "value"])


# @spec EVAL-LAG-001, EVAL-LAG-002
def lag_table(matchups: pd.DataFrame, observations: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """For lags −1, 0, +1 h: pairs, correlation, and RMSE of surface(H) against the same
    monitor's unflagged obs(H + lag); for ±1, the RMSE minus lag 0's over the matchups with both,
    with a day-bootstrap interval."""
    trusted = observations[
        observations["qc_flags"].map(len).eq(0) & observations["pm25_raw"].notna()
    ]
    lookup = pd.Series(
        trusted["pm25_raw"].to_numpy(dtype=float),
        index=pd.MultiIndex.from_arrays(
            [trusted["site_id"].astype(str), pd.to_datetime(trusted["observed_at"], utc=True)]
        ),
    )
    hours = pd.to_datetime(matchups["hour"], utc=True)
    surface = matchups["surface"].to_numpy(dtype=float)
    blocks = days(hours) if len(matchups) else np.array([])
    observed = {}
    for lag in LAGS:
        keys = pd.MultiIndex.from_arrays(
            [matchups["site_id"].astype(str), hours + pd.Timedelta(hours=lag)]
        )
        observed[lag] = lookup.reindex(keys).to_numpy(dtype=float)
    rows = []
    for lag in LAGS:
        obs = observed[lag]
        ok = np.isfinite(obs) & np.isfinite(surface)
        n = int(ok.sum())
        r = float(np.corrcoef(surface[ok], obs[ok])[0, 1]) if n > 2 else np.nan
        rmse = float(np.sqrt(np.mean((surface[ok] - obs[ok]) ** 2))) if n else np.nan
        difference, interval = np.nan, (np.nan, np.nan)
        both = ok & np.isfinite(observed[0])
        if lag != 0 and both.any():
            data = np.column_stack([
                np.ones(int(both.sum())),
                (surface[both] - obs[both]) ** 2,
                (surface[both] - observed[0][both]) ** 2,
            ])

            def rmse_difference(t: np.ndarray) -> np.ndarray:
                return np.sqrt(t[:, 1] / t[:, 0]) - np.sqrt(t[:, 2] / t[:, 0])

            difference = float(rmse_difference(data.sum(axis=0)[None, :])[0])
            interval = sums_interval(blocks[both], data, rmse_difference, seed)
        rows.append({"lag": lag, "n": n, "pearson_r": r, "rmse": rmse,
                     "rmse_difference": difference, "rmse_difference_ci_low": interval[0],
                     "rmse_difference_ci_high": interval[1]})
    return pd.DataFrame(rows, columns=LAG_COLUMNS)


# @spec EVAL-FLAG-001
def flag_rates(matchups: pd.DataFrame, observations: pd.DataFrame) -> dict[str, int]:
    """Flagged counts over both denominators: AirNow observations, and matchups."""
    return {
        "observations_flagged": int(observations["qc_flags"].map(len).gt(0).sum()),
        "observations": len(observations),
        "matchups_flagged": int(matchups["flagged"].astype(bool).sum()),
        "matchups": len(matchups),
    }
