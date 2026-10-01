"""Pandera frame model for ``surface_fits``, and the record → frame bridge."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
import pandera.pandas as pa
from pandera.typing import Series

from aqdt.fusion.schemas import SurfaceFit, SurfaceStatus
from aqdt.observation_store.frames import UTC_NS, empty_frame

_STATUS = [status.value for status in SurfaceStatus]


# @spec FUS-STORE-002, FUS-STORE-003
class SurfaceFitFrame(pa.DataFrameModel):
    hour: Series[pd.DatetimeTZDtype] = pa.Field(dtype_kwargs=UTC_NS)
    status: Series[str] = pa.Field(isin=_STATUS, coerce=True)
    n_sensors: Series[int] = pa.Field(ge=0, coerce=True)
    n_points: Series[int] = pa.Field(ge=0, coerce=True)
    variogram_model: Series[str] = pa.Field(nullable=True, coerce=True)
    sill: Series[float] = pa.Field(nullable=True, coerce=True)
    range_m: Series[float] = pa.Field(nullable=True, coerce=True)
    nugget: Series[float] = pa.Field(nullable=True, coerce=True)
    q1: Series[float] = pa.Field(nullable=True, coerce=True)
    q2: Series[float] = pa.Field(nullable=True, coerce=True)
    cr: Series[float] = pa.Field(nullable=True, coerce=True)
    resolution_m: Series[float] = pa.Field(nullable=True, coerce=True)
    width: Series[pd.Int64Dtype] = pa.Field(nullable=True, coerce=True)
    height: Series[pd.Int64Dtype] = pa.Field(nullable=True, coerce=True)
    surface_uri: Series[str] = pa.Field(nullable=True, coerce=True)
    surface_sha256: Series[str] = pa.Field(nullable=True, coerce=True)

    class Config:
        strict = True
        unique = ["hour"]


def surface_fits_to_frame(records: Iterable[SurfaceFit]) -> pd.DataFrame:
    """A validated frame of the records (typed and empty when there are none)."""
    rows = [record.model_dump(mode="python") for record in records]
    if not rows:
        return empty_frame(SurfaceFitFrame)
    frame = pd.DataFrame(rows, columns=list(SurfaceFit.model_fields))
    frame["hour"] = pd.to_datetime(frame["hour"], utc=True).astype("datetime64[ns, UTC]")
    frame["status"] = frame["status"].map(lambda v: v.value if isinstance(v, SurfaceStatus) else v)
    for name in ("sill", "range_m", "nugget", "q1", "q2", "cr", "resolution_m"):
        frame[name] = frame[name].astype("float64")
    for name in ("n_sensors", "n_points"):
        frame[name] = frame[name].astype("int64")
    for name in ("width", "height"):
        frame[name] = frame[name].astype("Int64")
    return SurfaceFitFrame.validate(frame)
