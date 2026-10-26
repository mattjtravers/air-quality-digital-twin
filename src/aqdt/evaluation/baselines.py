"""Baselines scored at the same matchups as the surface: the hour's mean and median,
inverse-distance weighting, and a random forest, each from the hour's kriging points only; and the
paired comparison of kriging against each."""

from __future__ import annotations

import numpy as np
import pandas as pd

from aqdt.evaluation.bootstrap import days, mean_interval, metric_intervals
from aqdt.evaluation.metrics import level_group, matchup_metrics, tertile_bands

FEATURES = ["x", "y", "hour_of_day", "median"]
METHODS = {
    "kriging": "surface",
    "hour_mean": "hour_mean",
    "hour_median": "hour_median",
    "idw": "idw",
    "rf": "rf",
}
BASELINES = ["hour_mean", "hour_median", "idw", "rf"]
BASE_METRICS = ["n", "bias", "mae", "rmse", "r2", "pearson_r2", "pearson_r"]
INTERVALS = [f"{m}_ci_{side}" for m in BASE_METRICS[1:] for side in ("low", "high")]
COLUMNS = ["method", "subset", "split", "group", *BASE_METRICS, *INTERVALS]
PAIRED_COLUMNS = ["baseline", "split", "group", "n", "mean_difference", "ci_low", "ci_high"]


# @spec EVAL-BASE-004
def hour_level(pz: np.ndarray) -> tuple[float, float]:
    """The hour's zero-skill prediction: the mean and the median of its point values."""
    pz = np.asarray(pz, dtype=float)
    return float(pz.mean()), float(np.median(pz))


# @spec EVAL-BASE-001
def idw(px: np.ndarray, py: np.ndarray, pz: np.ndarray, x: float, y: float) -> float:
    """Inverse-distance-weighted mean (power 2) of the point values at ``(x, y)``; the mean of the
    points at distance 0 when any are."""
    d2 = (np.asarray(px) - x) ** 2 + (np.asarray(py) - y) ** 2
    pz = np.asarray(pz, dtype=float)
    at = d2 == 0
    if at.any():
        return float(pz[at].mean())
    w = 1.0 / d2
    return float((w * pz).sum() / w.sum())


def features(frame: pd.DataFrame, points: pd.DataFrame) -> pd.DataFrame:
    """The forest's features for ``frame`` (columns ``hour``, ``x``, ``y``): location, UTC hour of
    day, and the median of ``points``' values in the same hour."""
    median = points.groupby("hour")["z"].median()
    hours = pd.to_datetime(frame["hour"], utc=True)
    return pd.DataFrame(
        {
            "x": frame["x"].to_numpy(dtype=float),
            "y": frame["y"].to_numpy(dtype=float),
            "hour_of_day": hours.dt.hour.to_numpy(),
            "median": frame["hour"].map(median).to_numpy(dtype=float),
        },
        index=frame.index,
        columns=FEATURES,
    )


# @spec EVAL-BASE-002
def fit_forest(points: pd.DataFrame, seed: int):
    """A random forest trained on every kriging point (``hour``, ``x``, ``y``, ``z``)."""
    from sklearn.ensemble import RandomForestRegressor

    model = RandomForestRegressor(
        n_estimators=200, min_samples_leaf=5, random_state=seed, n_jobs=-1
    )
    model.fit(features(points, points), points["z"].to_numpy(dtype=float))
    return model


def predict_forest(model, points: pd.DataFrame, query: pd.DataFrame) -> np.ndarray:
    """The forest's predictions for ``query``, averaged over trees in one thread so the sum —
    and the CSV it lands in — is the same on every run."""
    return model.set_params(n_jobs=1).predict(features(query, points))


def _split_groups(frame: pd.DataFrame) -> dict[str, pd.Series]:
    """``overall``, observed ``level``, and ``pred_level`` (always the kriging surface's band)."""
    return {
        "overall": pd.Series("all", index=frame.index),
        "level": frame["obs"].map(level_group),
        "pred_level": tertile_bands(frame["surface"]),
    }


# @spec EVAL-BASE-003
def baseline_table(matchups: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """The accuracy metrics and their day-bootstrap intervals of each method (``kriging`` is the
    stored surface) over the ``unflagged`` and ``all`` matchups, overall and by observed and
    predicted level."""
    subsets = {"unflagged": matchups[~matchups["flagged"].astype(bool)], "all": matchups}
    rows = []
    for method, column in METHODS.items():
        for subset, frame in subsets.items():
            for split, groups in _split_groups(frame).items():
                for group in sorted(groups.unique()):
                    part = frame[groups == group]
                    scored = pd.DataFrame(
                        {
                            "hour": part["hour"].to_numpy(),
                            "obs": part["obs"].to_numpy(dtype=float),
                            "surface": part[column].to_numpy(dtype=float),
                        }
                    )
                    scored["error"] = scored["surface"] - scored["obs"]
                    scored["z"] = np.nan
                    scored["z_total"] = np.nan
                    scored["inputs_reproduced"] = False
                    m = matchup_metrics(scored)
                    ci = metric_intervals(scored, seed)
                    rows.append(
                        {
                            "method": method,
                            "subset": subset,
                            "split": split,
                            "group": group,
                            **{k: m[k] for k in BASE_METRICS},
                            **{k: ci[k] for k in INTERVALS},
                        }
                    )
    return pd.DataFrame(rows, columns=COLUMNS)


# @spec EVAL-BASE-005
def paired_table(matchups: pd.DataFrame, seed: int = 0) -> pd.DataFrame:
    """Per baseline and group of the unflagged matchups: the mean of ``|kriging error| −
    |baseline error|`` with its day-bootstrap interval; negative means kriging is closer."""
    frame = matchups[~matchups["flagged"].astype(bool)]
    rows = []
    obs = frame["obs"].to_numpy(dtype=float)
    kriging = np.abs(frame["surface"].to_numpy(dtype=float) - obs)
    for baseline in BASELINES:
        difference = kriging - np.abs(frame[baseline].to_numpy(dtype=float) - obs)
        for split, groups in _split_groups(frame).items():
            for group in sorted(groups.unique()):
                mask = (groups == group).to_numpy()
                d = difference[mask]
                lo, hi = mean_interval(days(frame.loc[mask, "hour"]), d, seed)
                rows.append(
                    {
                        "baseline": baseline,
                        "split": split,
                        "group": group,
                        "n": int(mask.sum()),
                        "mean_difference": float(d.mean()) if len(d) else np.nan,
                        "ci_low": lo,
                        "ci_high": hi,
                    }
                )
    return pd.DataFrame(rows, columns=PAIRED_COLUMNS)
