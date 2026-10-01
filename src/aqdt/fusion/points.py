"""An hour's kriging points: calibrated sensor values at projected, de-duplicated locations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import numpy as np
import pandas as pd
from pyproj import Transformer

from aqdt.calibration.schemas import FitStatus
from aqdt.fusion.grid import CRS
from aqdt.observation_store.schemas import SiteType

_TO_UTM = Transformer.from_crs(4326, CRS, always_xy=True)
_KRIGED_STATUSES = [FitStatus.fitted.value, FitStatus.pooled.value]


@dataclass(frozen=True, eq=False)
class HourPoints:
    """Distinct kriging locations (EPSG:26918 metres) and their values, µg/m³."""

    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    n_sensors: int  # calibrated rows used
    n_points: int  # distinct locations after merging co-located sensors


# @spec FUS-IN-002, FUS-IN-003, FUS-IN-004, FUS-PTS-001, FUS-PTS-002, FUS-PTS-003, FUS-PTS-004
def hour_points(calibrated: pd.DataFrame, sites: pd.DataFrame, hour: datetime) -> HourPoints:
    """The points to krige for ``hour``: fitted or pooled sensor values with a site record,
    projected to EPSG:26918, with sensors at the same metre merged to their mean. Sensors outside
    the grid's extent are kept; reference monitors never appear."""
    rows = calibrated[
        (calibrated["hour"] == hour)
        & calibrated["fit_status"].isin(_KRIGED_STATUSES)
        & calibrated["pm25_calibrated"].notna()
    ]
    sensors = sites[sites["site_type"] == SiteType.low_cost_sensor.value]
    located = rows[["site_id", "pm25_calibrated"]].merge(
        sensors[["site_id", "latitude", "longitude"]], on="site_id", how="inner"
    )
    if located.empty:
        empty = np.empty(0)
        return HourPoints(x=empty, y=empty, z=empty, n_sensors=0, n_points=0)
    x, y = _TO_UTM.transform(located["longitude"].to_numpy(), located["latitude"].to_numpy())
    merged = (
        pd.DataFrame({"x": np.round(x), "y": np.round(y), "z": located["pm25_calibrated"]})
        .groupby(["x", "y"], sort=True, as_index=False)["z"]
        .mean()
    )
    return HourPoints(
        x=merged["x"].to_numpy(dtype=float),
        y=merged["y"].to_numpy(dtype=float),
        z=merged["z"].to_numpy(dtype=float),
        n_sensors=len(located),
        n_points=len(merged),
    )
