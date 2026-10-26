"""Baselines scored at the same matchups as the surface: inverse-distance weighting and a random
forest, each from the hour's kriging points only."""

from __future__ import annotations

import numpy as np
import pandas as pd

from aqdt.evaluation.metrics import matchup_metrics

FEATURES = ["x", "y", "hour_of_day", "median"]
METHODS = {"kriging": "surface", "idw": "idw", "rf": "rf"}
BASE_METRICS = ["n", "bias", "mae", "rmse", "r2", "pearson_r"]
COLUMNS = ["method", "subset", *BASE_METRICS]


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


# @spec EVAL-BASE-003
def baseline_table(matchups: pd.DataFrame) -> pd.DataFrame:
    """The accuracy metrics of each method (``kriging`` is the stored surface) over the
    ``unflagged`` and ``all`` matchups."""
    subsets = {"unflagged": matchups[~matchups["flagged"].astype(bool)], "all": matchups}
    rows = []
    for method, column in METHODS.items():
        for subset, frame in subsets.items():
            scored = pd.DataFrame(
                {
                    "obs": frame["obs"].to_numpy(dtype=float),
                    "surface": frame[column].to_numpy(dtype=float),
                }
            )
            scored["error"] = scored["surface"] - scored["obs"]
            scored["z"] = np.nan
            scored["z_total"] = np.nan
            scored["inputs_reproduced"] = False
            m = matchup_metrics(scored)
            rows.append({"method": method, "subset": subset, **{k: m[k] for k in BASE_METRICS}})
    return pd.DataFrame(rows, columns=COLUMNS)
