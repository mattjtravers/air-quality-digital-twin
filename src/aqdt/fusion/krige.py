"""Per-hour variogram selection and chunked ordinary-kriging prediction (PyKrige)."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import numpy as np
from pykrige.ok import OrdinaryKriging

from aqdt.fusion.grid import Grid
from aqdt.fusion.points import HourPoints

log = logging.getLogger(__name__)

#: Candidate variogram models, in tie-break order.
CANDIDATES = ("spherical", "exponential")
#: Grid cells per prediction call; bounds memory independently of the grid's size.
CHUNK_CELLS = 50_000


@dataclass(frozen=True, eq=False)
class Variogram:
    """The hour's selected variogram and the fitted kriging system that predicts with it."""

    model: str
    sill: float  # partial sill + nugget, (µg/m³)²
    range_m: float
    nugget: float
    q1: float
    q2: float
    cr: float
    kriger: Any


# @spec FUS-VAR-001, FUS-VAR-002, FUS-VAR-003, FUS-VAR-004, FUS-VAR-005
def select_variogram(points: HourPoints) -> Variogram | None:
    """Fit each candidate model to the hour's points and keep the one with the smallest
    cross-validation ``cR`` (ties to the earlier candidate); ``None`` when none is eligible."""
    best: Variogram | None = None
    for model in CANDIDATES:
        try:
            kriger = OrdinaryKriging(
                points.x,
                points.y,
                points.z,
                variogram_model=model,
                nlags=6,
                weight=False,
                enable_statistics=True,
                coordinates_type="euclidean",
            )
        except Exception as error:  # noqa: BLE001 - any failure makes the candidate ineligible
            log.debug("variogram %s not eligible: %s", model, error)
            continue
        cr = float(kriger.cR)
        if not math.isfinite(cr):
            log.debug("variogram %s not eligible: cR = %s", model, cr)
            continue
        if best is None or cr < best.cr:
            psill, range_m, nugget = (float(v) for v in kriger.variogram_model_parameters)
            best = Variogram(
                model=model,
                sill=psill + nugget,
                range_m=range_m,
                nugget=nugget,
                q1=float(kriger.Q1),
                q2=float(kriger.Q2),
                cr=cr,
                kriger=kriger,
            )
    return best


# @spec FUS-PRED-001, FUS-PRED-002, FUS-PRED-003
def predict_chunked(
    variogram: Variogram, grid: Grid, chunk_cells: int = CHUNK_CELLS
) -> tuple[np.ndarray, np.ndarray]:
    """Krige every cell centre, ``chunk_cells`` at a time; return the estimate and the kriging
    variance as ``(height, width)`` float32 arrays, each clamped at 0."""
    estimate = np.empty(grid.n_cells, dtype=np.float32)
    variance = np.empty(grid.n_cells, dtype=np.float32)
    for start in range(0, grid.n_cells, chunk_cells):
        stop = min(start + chunk_cells, grid.n_cells)
        xs, ys = grid.cell_centres(start, stop)
        z, ss = variogram.kriger.execute("points", xs, ys, backend="vectorized")
        estimate[start:stop] = np.maximum(np.ma.getdata(z), 0.0)
        variance[start:stop] = np.maximum(np.ma.getdata(ss), 0.0)
    return estimate.reshape(grid.height, grid.width), variance.reshape(grid.height, grid.width)
