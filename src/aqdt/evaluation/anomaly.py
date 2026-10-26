"""Common offset and within-hour anomaly: how much of each method's error is the hour's shared
offset across monitors, and how well it places the differences between monitors within the
hour."""

from __future__ import annotations

import numpy as np
import pandas as pd

from aqdt.evaluation.bootstrap import days, sums_interval

MIN_MATCHUPS = 5
METHODS = {"kriging": "surface", "idw": "idw", "rf": "rf", "hour_mean": "hour_mean"}
STATS = ["offset_share", "anomaly_share", "anomaly_r", "anomaly_rmse", "hourly_r",
         "offset_reduction", "anomaly_reduction", "v_across", "v_within", "across_share",
         "within_share", "across_explained", "within_explained"]
COLUMNS = ["method", "n", "hours"] + [
    f"{s}{suffix}" for s in STATS for suffix in ("", "_ci_low", "_ci_high")
]


def _eligible(matchups: pd.DataFrame) -> pd.DataFrame:
    rows = matchups[~matchups["flagged"].astype(bool)]
    counts = rows.groupby("hour")["obs"].transform("size")
    return rows[counts >= MIN_MATCHUPS]


def _parts(rows: pd.DataFrame, column: str) -> dict[str, np.ndarray]:
    """Per matchup: the hour's offset and the anomaly of the error, and the anomalies of the
    prediction and of the monitor value."""
    hour = rows["hour"]
    prediction = rows[column].astype(float)
    obs = rows["obs"].astype(float)
    error = prediction - obs
    offset = error.groupby(hour).transform("mean")
    return {
        "e2": (error**2).to_numpy(),
        "offset2": (offset**2).to_numpy(),
        "anomaly2": ((error - offset) ** 2).to_numpy(),
        "p": (prediction - prediction.groupby(hour).transform("mean")).to_numpy(),
        "o": (obs - obs.groupby(hour).transform("mean")).to_numpy(),
        "obs": obs.to_numpy(),
        "obs_hour": obs.groupby(hour).transform("mean").to_numpy(),
    }


def _with_interval(name: str, estimate: float, interval: tuple[float, float]) -> dict:
    return {name: estimate, f"{name}_ci_low": interval[0], f"{name}_ci_high": interval[1]}


def _ratio(t: np.ndarray) -> np.ndarray:
    return t[:, 0] / t[:, 1]


def _correlation(t: np.ndarray) -> np.ndarray:
    """Pearson r of two columns with zero mean in every hour: ``Σpo / √(Σp² Σo²)``, null when
    the prediction's anomalies have no variance."""
    spo, spp, soo, n = t[:, 0], t[:, 1], t[:, 2], t[:, 3]
    return np.where(spp > 1e-12 * n, spo / np.sqrt(spp * soo), np.nan)


def _variance_parts(t: np.ndarray) -> tuple[np.ndarray, ...]:
    """From sums of ``1, obs, mean_H(obs)², anomaly(obs)², ē_H², a²``: the monitors' across- and
    within-hour variance and the method's two MSE parts, per resample."""
    n, so, som2, soa2, soff2, san2 = (t[:, k] for k in range(6))
    across = som2 / n - (so / n) ** 2
    return across, soa2 / n, soff2 / n, san2 / n


_VARIANCE_STATS = {
    "v_across": lambda t: _variance_parts(t)[0],
    "v_within": lambda t: _variance_parts(t)[1],
    "across_share": lambda t: _variance_parts(t)[0] / (_variance_parts(t)[0]
                                                       + _variance_parts(t)[1]),
    "within_share": lambda t: _variance_parts(t)[1] / (_variance_parts(t)[0]
                                                       + _variance_parts(t)[1]),
    "across_explained": lambda t: 1 - _variance_parts(t)[2] / _variance_parts(t)[0],
    "within_explained": lambda t: 1 - _variance_parts(t)[3] / _variance_parts(t)[1],
}


def _pearson(t: np.ndarray) -> np.ndarray:
    n, sx, sy, sxx, syy, sxy = (t[:, k] for k in range(6))
    vx, vy = sxx - sx**2 / n, syy - sy**2 / n
    return np.where((vx > 1e-12 * n) & (vy > 0), (sxy - sx * sy / n) / np.sqrt(vx * vy), np.nan)


def _stat(data: np.ndarray, fn, blocks: np.ndarray, seed: int) -> tuple[float, tuple]:
    with np.errstate(divide="ignore", invalid="ignore"):
        estimate = float(fn(data.sum(axis=0)[None, :])[0]) if len(data) else np.nan
    return estimate, sums_interval(blocks, data, fn, seed) if len(data) else (np.nan, np.nan)


# @spec EVAL-ANOM-001, EVAL-ANOM-002, EVAL-ANOM-003, EVAL-ANOM-005
def anomaly_table(matchups: pd.DataFrame, seed: int) -> pd.DataFrame:
    """Per method, over the unflagged matchups of hours with at least ``MIN_MATCHUPS`` of them:
    the offset and anomaly shares of the MSE, the anomaly and hourly-mean correlations, the
    anomaly RMSE, the reductions from the hourly mean, and the across- and within-hour variance
    explained, each with a day-bootstrap interval."""
    rows = _eligible(matchups)
    blocks = days(rows["hour"]) if len(rows) else np.array([])
    hour_blocks = days(rows["hour"].drop_duplicates()) if len(rows) else np.array([])
    reference = _parts(rows, "hour_mean") if "hour_mean" in rows else None
    ones = np.ones(len(rows))
    out = []
    for method, column in METHODS.items():
        if column not in matchups:
            continue
        parts = _parts(rows, column)
        record = {"method": method, "n": len(rows), "hours": int(rows["hour"].nunique())}
        e2, off2, an2 = parts["e2"], parts["offset2"], parts["anomaly2"]
        for name, numerator in (("offset_share", off2), ("anomaly_share", an2)):
            record.update(_with_interval(
                name, *_stat(np.column_stack([numerator, e2]), _ratio, blocks, seed)
            ))
        p, o = parts["p"], parts["o"]
        record.update(_with_interval("anomaly_r", *_stat(
            np.column_stack([p * o, p**2, o**2, ones]), _correlation, blocks, seed
        )))
        estimate, (lo, hi) = _stat(np.column_stack([an2, ones]), _ratio, blocks, seed)
        record.update(_with_interval(
            "anomaly_rmse", float(np.sqrt(estimate)), (float(np.sqrt(lo)), float(np.sqrt(hi)))
        ))
        means = rows.groupby("hour", sort=True).agg(p=(column, "mean"), o=("obs", "mean"))
        pm, om = means["p"].to_numpy(dtype=float), means["o"].to_numpy(dtype=float)
        record.update(_with_interval("hourly_r", *_stat(
            np.column_stack([np.ones_like(pm), pm, om, pm**2, om**2, pm * om]), _pearson,
            hour_blocks, seed,
        )))
        for name, key in (("offset_reduction", "offset2"), ("anomaly_reduction", "anomaly2")):
            if method == "hour_mean" or reference is None:
                record.update(_with_interval(name, np.nan, (np.nan, np.nan)))
                continue
            data = np.column_stack([reference[key] - parts[key], ones])
            record.update(_with_interval(name, *_stat(data, _ratio, blocks, seed)))
        variance = np.column_stack([
            ones, parts["obs"], parts["obs_hour"] ** 2, o**2, off2, an2,
        ])
        for name, fn in _VARIANCE_STATS.items():
            record.update(_with_interval(name, *_stat(variance, fn, blocks, seed)))
        out.append(record)
    return pd.DataFrame(out, columns=COLUMNS)


# @spec EVAL-ANOM-004
def skill_source(
    v_across: float, across_explained: float, v_within: float, within_explained: float
) -> str:
    """Where a method's skill mainly comes from: the part of the monitors' variance in which it
    explains more, in variance rather than in fractions."""
    if v_across * across_explained > v_within * within_explained:
        return "the hour's level"
    return "the within-hour pattern"
