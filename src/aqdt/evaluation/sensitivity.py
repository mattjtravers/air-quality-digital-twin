"""Variogram sensitivity: how much the surface at the monitors would move under the candidate
variogram fusion did not select."""

from __future__ import annotations

import math

import numpy as np
from pykrige.ok import OrdinaryKriging

from aqdt.evaluation.totvar import as_fixed
from aqdt.fusion.krige import CANDIDATES
from aqdt.fusion.points import HourPoints


# @spec EVAL-SENS-001
def variogram_sensitivity(points: HourPoints, model: str, params, tx, ty) -> tuple[str, float]:
    """The alternative candidate and the RMS difference between its prediction and the recorded
    variogram's at the targets; NaN when the alternative is not eligible or there is no target."""
    alternative = next(c for c in CANDIDATES if c != model)
    tx, ty = np.atleast_1d(tx), np.atleast_1d(ty)
    if len(tx) == 0:
        return alternative, math.nan
    try:
        other = OrdinaryKriging(
            points.x,
            points.y,
            points.z,
            variogram_model=alternative,
            nlags=6,
            weight=False,
            enable_statistics=True,
            coordinates_type="euclidean",
        )
    except Exception:  # noqa: BLE001 - an unfittable candidate is not eligible (FUS-VAR-003)
        return alternative, math.nan
    if not math.isfinite(float(other.cR)):
        return alternative, math.nan
    recorded = OrdinaryKriging(
        points.x, points.y, points.z, variogram_model=model, variogram_parameters=as_fixed(params)
    )
    a, _ = recorded.execute("points", tx, ty, backend="vectorized")
    b, _ = other.execute("points", tx, ty, backend="vectorized")
    diff = np.ma.getdata(a) - np.ma.getdata(b)
    return alternative, float(np.sqrt((diff**2).mean()))
