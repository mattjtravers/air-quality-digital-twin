"""The development-window forest: a random forest on the baseline's features, tuned by
cross-validation grouped by day, fitted on the development window's kriging points, its tree
spread as an uncertainty, and SHAP attributions."""

from __future__ import annotations

import itertools

import numpy as np
import pandas as pd

from aqdt.evaluation.baselines import FEATURES, features

#: The grid searched, in report order.
GRID = {
    "n_estimators": [200, 500],
    "min_samples_leaf": [1, 5, 20],
    "max_features": [1.0, 0.5],
}
MAX_FOLDS = 5
SHAP_SAMPLE = 1000
SELECTION_RULE = (
    "lowest mean fold RMSE; ties to fewer trees, then larger min_samples_leaf, "
    "then max_features = 0.5"
)


def configurations() -> list[dict]:
    """Every grid point, in report order."""
    keys = list(GRID)
    return [dict(zip(keys, values)) for values in itertools.product(*GRID.values())]


def _model(config: dict, seed: int):
    from sklearn.ensemble import RandomForestRegressor

    return RandomForestRegressor(
        n_estimators=int(config["n_estimators"]),
        min_samples_leaf=int(config["min_samples_leaf"]),
        max_features=float(config["max_features"]),
        random_state=seed,
        n_jobs=-1,
    )


def _dates(points: pd.DataFrame) -> np.ndarray:
    return pd.to_datetime(points["hour"], utc=True).dt.strftime("%Y-%m-%d").to_numpy()


def n_folds(points: pd.DataFrame) -> int:
    return min(MAX_FOLDS, len(np.unique(_dates(points))))


# @spec EVAL-HOLD-012, EVAL-HOLD-013
def grid_search(points: pd.DataFrame, seed: int, window: str) -> pd.DataFrame:
    """Each configuration's RMSE on every held-out fold of ``GroupKFold`` by UTC date over
    ``points`` (``hour``, ``x``, ``y``, ``z``), its mean and SD over folds, and its rank."""
    from sklearn.model_selection import GroupKFold

    folds = n_folds(points)
    if folds < 2:
        raise ValueError(
            f"the development window {window} needs kriging points on at least two UTC days "
            f"for grouped cross-validation; it has {folds}"
        )
    X = features(points, points)
    y = points["z"].to_numpy(dtype=float)
    splits = list(GroupKFold(n_splits=folds).split(X, y, groups=_dates(points)))
    rows = []
    for config in configurations():
        scores = []
        for train, held in splits:
            model = _model(config, seed).fit(X.iloc[train], y[train])
            predicted = model.set_params(n_jobs=1).predict(X.iloc[held])
            scores.append(float(np.sqrt(np.mean((predicted - y[held]) ** 2))))
        rows.append(
            {
                **config,
                **{f"fold_{k + 1}": s for k, s in enumerate(scores)},
                "mean_rmse": float(np.mean(scores)),
                "sd_rmse": float(np.std(scores, ddof=0)),
            }
        )
    table = pd.DataFrame(rows)
    choice = choose(table)
    order = _ordered(table)
    table["rank"] = 0
    table.loc[order.index, "rank"] = np.arange(1, len(table) + 1)
    table["chosen"] = (
        (table["n_estimators"] == choice["n_estimators"])
        & (table["min_samples_leaf"] == choice["min_samples_leaf"])
        & (table["max_features"] == choice["max_features"])
    )
    return table


def _ordered(table: pd.DataFrame) -> pd.DataFrame:
    keyed = table.assign(
        _rmse=table["mean_rmse"].round(12),
        _trees=table["n_estimators"],
        _leaf=-table["min_samples_leaf"],
        _features=table["max_features"],
    )
    return keyed.sort_values(["_rmse", "_trees", "_leaf", "_features"], kind="stable")


# @spec EVAL-HOLD-014
def choose(table: pd.DataFrame) -> dict:
    """The configuration with the lowest mean fold RMSE; ties to the fewer trees, then the
    larger ``min_samples_leaf``, then ``max_features = 0.5``."""
    best = _ordered(table).iloc[0]
    return {
        "n_estimators": int(best["n_estimators"]),
        "min_samples_leaf": int(best["min_samples_leaf"]),
        "max_features": float(best["max_features"]),
    }


def fit(points: pd.DataFrame, config: dict, seed: int):
    """The forest of ``config`` fitted on every row of ``points``."""
    return _model(config, seed).fit(features(points, points), points["z"].to_numpy(dtype=float))


# @spec EVAL-HOLD-014
def predict_with_spread(
    model, points: pd.DataFrame, query: pd.DataFrame
) -> tuple[np.ndarray, np.ndarray]:
    """The forest's prediction for each ``query`` row (``hour``, ``x``, ``y``, with the hour
    median from ``points``) and the population SD of its trees' predictions."""
    X = features(query, points)
    mean = model.set_params(n_jobs=1).predict(X)
    values = X.to_numpy(dtype=np.float32)
    trees = np.stack([tree.predict(values) for tree in model.estimators_])
    return mean, trees.std(axis=0, ddof=0)


# @spec EVAL-HOLD-016
def shap_sample(model, points: pd.DataFrame, seed: int, n: int | None = None):
    """SHAP values of ``model`` on ``n`` (default ``SHAP_SAMPLE``) of ``points`` drawn without
    replacement with ``seed`` (every point when fewer): the sampled features and a frame of their
    SHAP values."""
    import shap

    n = SHAP_SAMPLE if n is None else n
    rng = np.random.default_rng(seed)
    take = np.sort(rng.choice(len(points), size=min(n, len(points)), replace=False))
    sample = points.iloc[take]
    X = features(sample, points).reset_index(drop=True)
    values = shap.TreeExplainer(model).shap_values(X)
    return X, pd.DataFrame(values, columns=[f"shap_{f}" for f in FEATURES])
