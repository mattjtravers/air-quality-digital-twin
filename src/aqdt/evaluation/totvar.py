"""The analytic calibration contribution to a kriged estimate's variance: ordinary-kriging
weights, the sensors' calibration error covariance, and the merge of co-located sensors."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np
from pykrige import variogram_models

_MODELS = {
    "spherical": variogram_models.spherical_variogram_model,
    "exponential": variogram_models.exponential_variogram_model,
}
_EXACT = 1e-10  # PyKrige's distance below which a target coincides with a point


def pykrige_parameters(sill: float, range_m: float, nugget: float) -> list[float]:
    """A surface fit row's variogram as PyKrige's internal parameters ``[partial sill, range,
    nugget]`` — the form its variogram functions take (``variogram_model_parameters``)."""
    return [sill - nugget, range_m, nugget]


def as_fixed(params: Sequence[float]) -> dict[str, float]:
    """``[partial sill, range, nugget]`` as the ``variogram_parameters`` dict ``OrdinaryKriging``
    accepts unambiguously (a list there would be read as a full sill)."""
    psill, range_m, nugget = (float(v) for v in params)
    return {"psill": psill, "range": range_m, "nugget": nugget}


# @spec EVAL-TOTVAR-001
def ok_weights(x, y, model: str, params: Sequence[float], tx, ty) -> np.ndarray:
    """Ordinary-kriging weights ``(targets, points)`` for a fixed variogram, solved from the same
    system PyKrige solves; each row sums to one."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    tx, ty = np.atleast_1d(np.asarray(tx, dtype=float)), np.atleast_1d(np.asarray(ty, dtype=float))
    gamma = _MODELS[model]
    params = list(params)
    n = len(x)
    d = np.hypot(x[:, None] - x[None, :], y[:, None] - y[None, :])
    a = np.zeros((n + 1, n + 1))
    a[:n, :n] = -gamma(params, d)
    np.fill_diagonal(a, 0.0)
    a[n, :n] = a[:n, n] = 1.0
    d0 = np.hypot(tx[:, None] - x[None, :], ty[:, None] - y[None, :])
    b = np.ones((len(tx), n + 1))
    b[:, :n] = -gamma(params, d0)
    exact = d0 <= _EXACT
    b[:, :n][exact] = 0.0
    solution = np.linalg.solve(a, b.T).T
    return solution[:, :n]


# @spec EVAL-TOTVAR-002
def sensor_covariance(x, C, sigma2, pooled_key: Sequence[object | None]) -> np.ndarray:
    """Calibration error covariance between an hour's sensors: each sensor's parameter and
    residual variance on the diagonal, and the shared pooled parameter error between pooled
    sensors of the same fit."""
    x = np.asarray(x, dtype=float)
    C = np.asarray(C, dtype=float)
    v = np.column_stack([np.ones_like(x), x])  # [1, x_i]
    sigma = np.zeros((len(x), len(x)))
    diag = np.einsum("ij,ijk,ik->i", v, C, v) + np.asarray(sigma2, dtype=float)
    keys = list(pooled_key)
    groups: dict[object, list[int]] = {}
    for i, key in enumerate(keys):
        if key is not None:
            groups.setdefault(key, []).append(i)
    for members in groups.values():
        idx = np.array(members)
        shared = C[idx[0]]
        sigma[np.ix_(idx, idx)] = v[idx] @ shared @ v[idx].T
    sigma[np.diag_indices_from(sigma)] = diag
    return sigma


# @spec EVAL-TOTVAR-003
def aggregation_matrix(sx, sy, px, py) -> np.ndarray:
    """``(points, sensors)``: each point the mean of the sensors at its metre-rounded location."""
    sx, sy = np.round(np.asarray(sx, dtype=float)), np.round(np.asarray(sy, dtype=float))
    index = {(float(a), float(b)): k for k, (a, b) in enumerate(zip(px, py))}
    M = np.zeros((len(index), len(sx)))
    for j, key in enumerate(zip(sx.tolist(), sy.tolist())):
        M[index[key], j] = 1.0
    return M / M.sum(axis=1, keepdims=True)


# @spec EVAL-TOTVAR-003
def cal_variance(lam: np.ndarray, M: np.ndarray, sigma_s: np.ndarray) -> np.ndarray:
    """``λᵀ M Σ_s Mᵀ λ`` for each target row of ``lam``."""
    w = np.atleast_2d(lam) @ M  # weights on sensors
    return np.einsum("ij,jk,ik->i", w, sigma_s, w)
