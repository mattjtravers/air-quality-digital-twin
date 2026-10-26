"""Accuracy and coverage metrics for sets of matchups, and the subsets and splits they are
reported over."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

METRICS = [
    "n",
    "bias",
    "mae",
    "rmse",
    "r2",
    "pearson_r2",
    "pearson_r",
    "cov1_krig",
    "cov2_krig",
    "cov1_total",
    "cov2_total",
    "cov1_total_raw",
    "cov2_total_raw",
]
SPLITS = [
    "overall",
    "level",
    "pred_level",
    "hour_mean_points",
    "hour_mean_obs",
    "hour_of_day",
    "day",
    "pooled_band",
    "spike_hour",
    "spike_day",
    "variogram",
    "monitor",
]
# Splits read from a label column of the matchups; skipped when the column is absent.
LABEL_COLUMNS = {
    "hour_mean_points": "hour_mean_points_band",
    "hour_mean_obs": "hour_mean_obs_band",
    "spike_hour": "spike_hour",
    "spike_day": "spike_day",
    "variogram": "variogram",
    "monitor": "site_id",
}


def _coverage(z: pd.Series, limit: float) -> float:
    z = z.dropna()
    return float((z.abs() <= limit).mean()) if len(z) else math.nan


# @spec EVAL-METRIC-001, EVAL-METRIC-002
def matchup_metrics(frame: pd.DataFrame) -> dict[str, float | int]:
    """Every metric over ``frame``'s matchups (columns ``obs``, ``surface``, ``error``, ``z``,
    ``z_total``, ``inputs_reproduced``)."""
    n = len(frame)
    out: dict[str, float | int] = {"n": n}
    error = frame["error"].to_numpy(dtype=float)
    obs = frame["obs"].to_numpy(dtype=float)
    surface = frame["surface"].to_numpy(dtype=float)
    out["bias"] = float(error.mean()) if n else math.nan
    out["mae"] = float(np.abs(error).mean()) if n else math.nan
    out["rmse"] = float(np.sqrt((error**2).mean())) if n else math.nan
    ss_obs = float(((obs - obs.mean()) ** 2).sum()) if n else 0.0
    ss_surface = float(((surface - surface.mean()) ** 2).sum()) if n else 0.0
    if n >= 2 and ss_obs > 0:
        out["r2"] = 1.0 - float((error**2).sum()) / ss_obs
    else:
        out["r2"] = math.nan
    if n >= 2 and ss_obs > 0 and ss_surface > 0:
        out["pearson_r"] = float(np.corrcoef(surface, obs)[0, 1])
    else:
        out["pearson_r"] = math.nan
    out["pearson_r2"] = out["pearson_r"] ** 2
    out["cov1_krig"] = _coverage(frame["z"], 1)
    out["cov2_krig"] = _coverage(frame["z"], 2)
    reproduced = frame["inputs_reproduced"].astype(bool)
    for suffix, column in (("total", "z_total"), ("total_raw", "z_total_raw")):
        z = frame.loc[reproduced, column] if column in frame else pd.Series(dtype=float)
        out[f"cov1_{suffix}"] = _coverage(z, 1)
        out[f"cov2_{suffix}"] = _coverage(z, 2)
    return {k: out[k] for k in METRICS}


# @spec EVAL-SPLIT-002
def level_group(obs: float) -> str:
    """The PM2.5 level of a monitor value: ``<5``, ``5–9`` (inclusive), ``>9`` µg/m³."""
    if obs < 5:
        return "<5"
    if obs <= 9:
        return "5–9"
    return ">9"


# @spec EVAL-SPLIT-003
def tertile_bands(shares: pd.Series) -> pd.Series:
    """Each value's tertile band, labelled with its edges; coinciding edges merge."""
    shares = shares.astype(float)
    if shares.empty:
        return pd.Series(dtype=object, index=shares.index)
    if shares.nunique() <= 1:
        value = float(shares.iloc[0])
        return pd.Series(f"[{value:.2f}, {value:.2f}]", index=shares.index)
    _, edges = pd.qcut(shares, 3, retbins=True, duplicates="drop")
    labels = [
        f"{'[' if k == 0 else '('}{lo:.2f}, {hi:.2f}]"
        for k, (lo, hi) in enumerate(zip(edges[:-1], edges[1:]))
    ]
    banded = pd.cut(shares, edges, labels=labels, include_lowest=True)
    return banded.astype(str)


def pooled_bands(shares: pd.Series) -> pd.Series:
    """Each hour's pooled-share tertile band (``tertile_bands`` over the hourly shares)."""
    return tertile_bands(shares)


def _groups(frame: pd.DataFrame, split: str, bands: pd.Series) -> pd.Series:
    hours = pd.to_datetime(frame["hour"], utc=True)
    if split == "overall":
        return pd.Series("all", index=frame.index)
    if split == "level":
        return frame["obs"].map(level_group)
    if split == "pred_level":
        return tertile_bands(frame["surface"])
    if split in LABEL_COLUMNS:
        return frame[LABEL_COLUMNS[split]].astype(str)
    if split == "hour_of_day":
        return hours.dt.hour.map(lambda h: f"{h:02d}")
    if split == "day":
        return hours.dt.strftime("%Y-%m-%d")
    lookup = {pd.Timestamp(k): v for k, v in bands.items()}
    return hours.map(lambda h: lookup.get(h, "none"))


def _row(frame: pd.DataFrame, seed: int) -> dict:
    from aqdt.evaluation.bootstrap import metric_intervals

    return {**matchup_metrics(frame), **metric_intervals(frame, seed)}


def _available(frame: pd.DataFrame, split: str) -> bool:
    return split not in LABEL_COLUMNS or LABEL_COLUMNS[split] in frame.columns


# @spec EVAL-SPLIT-001, EVAL-SPLIT-002, EVAL-SPLIT-004, EVAL-METRIC-003
def metric_table(matchups: pd.DataFrame, bands: pd.Series, seed: int = 0) -> pd.DataFrame:
    """One long table: ``subset`` (``unflagged`` then ``all``) × ``split`` × ``group``, every
    metric with its day-bootstrap interval."""
    from aqdt.evaluation.bootstrap import INTERVAL_METRICS
    rows = []
    subsets = {
        "unflagged": matchups[~matchups["flagged"].astype(bool)] if len(matchups) else matchups,
        "all": matchups,
    }
    for subset, frame in subsets.items():
        for split in SPLITS:
            if frame.empty:
                if split == "overall":
                    rows.append(
                        {"subset": subset, "split": split, "group": "all", **_row(frame, seed)}
                    )
                continue
            if not _available(frame, split):
                continue
            groups = _groups(frame, split, bands)
            for group in sorted(groups.unique()):
                rows.append(
                    {
                        "subset": subset,
                        "split": split,
                        "group": group,
                        **_row(frame[groups == group], seed),
                    }
                )
    intervals = [f"{m}_ci_{side}" for m in INTERVAL_METRICS for side in ("low", "high")]
    return pd.DataFrame(rows, columns=["subset", "split", "group", *METRICS, *intervals])
