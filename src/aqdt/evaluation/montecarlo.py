"""Monte Carlo propagation of calibration uncertainty through kriging, as a check on the analytic
term: draw fit parameters and residuals, recalibrate, krige with the hour's fixed variogram."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
import pandas as pd
from pykrige.ok import OrdinaryKriging

from aqdt.evaluation.totvar import aggregation_matrix, as_fixed


# @spec EVAL-MC-001
def select_mc_hours(hour_means: pd.Series, n: int, seed: int) -> list:
    """``n`` hours (index of ``hour_means``, the hour's mean monitor value), seeded, spread over
    the tertiles of the mean and over distinct days; every hour when there are at most ``n``."""
    hours = list(hour_means.index)
    if len(hours) <= n:
        return sorted(hours)
    rng = np.random.default_rng(seed)
    tertile = pd.qcut(hour_means.rank(method="first"), 3, labels=False)
    pools = {t: list(rng.permutation(hour_means.index[tertile == t])) for t in range(3)}
    picked: list = []
    days: set = set()
    t = 0
    while len(picked) < n and any(pools.values()):
        pool = pools[t % 3]
        t += 1
        if not pool:
            continue
        fresh = [h for h in pool if pd.Timestamp(h).date() not in days]
        choice = fresh[0] if fresh else pool[0]
        pool.remove(choice)
        picked.append(choice)
        days.add(pd.Timestamp(choice).date())
    return sorted(picked)


# @spec EVAL-MC-002, EVAL-MC-003
def propagate(
    x,
    y,
    corrected,
    intercept,
    slope,
    C,
    sigma2,
    pooled_key: Sequence[object | None],
    model: str,
    params: Sequence[float],
    tx,
    ty,
    *,
    draws: int,
    rng: np.random.Generator,
    shared_pooled: bool = True,
) -> np.ndarray:
    """Variance across ``draws`` of the kriged estimate at each target, with each sensor's
    calibration drawn from its fit's parameter covariance and residual variance. With
    ``shared_pooled``, pooled sensors of one fit share each parameter draw."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    corrected = np.asarray(corrected, dtype=float)
    theta = np.column_stack([np.asarray(intercept, dtype=float), np.asarray(slope, dtype=float)])
    C = np.asarray(C, dtype=float)
    sd = np.sqrt(np.asarray(sigma2, dtype=float))
    keys = list(pooled_key)
    px_py = (
        pd.DataFrame({"x": np.round(x), "y": np.round(y)}).drop_duplicates().sort_values(["x", "y"])
    )
    px, py = px_py["x"].to_numpy(), px_py["y"].to_numpy()
    M = aggregation_matrix(x, y, px, py)
    first_of: dict[object, int] = {}
    for i, key in enumerate(keys):
        if key is not None and key not in first_of:
            first_of[key] = i
    predictions = np.empty((draws, len(np.atleast_1d(tx))))
    for k in range(draws):
        params_draw = np.empty_like(theta)
        shared = (
            {key: rng.multivariate_normal(theta[i], C[i]) for key, i in first_of.items()}
            if shared_pooled
            else {}
        )
        for i, key in enumerate(keys):
            if key is not None and shared_pooled:
                params_draw[i] = shared[key]
            else:
                params_draw[i] = rng.multivariate_normal(theta[i], C[i])
        residual = rng.normal(0.0, sd)
        values = np.maximum(0.0, params_draw[:, 0] + params_draw[:, 1] * corrected + residual)
        kriger = OrdinaryKriging(
            px, py, M @ values, variogram_model=model, variogram_parameters=as_fixed(params)
        )
        z, _ = kriger.execute("points", np.atleast_1d(tx), np.atleast_1d(ty), backend="vectorized")
        predictions[k] = np.ma.getdata(z)
    return predictions.var(axis=0, ddof=1)
