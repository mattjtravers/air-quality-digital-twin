"""Pandera frame models for the calibration products, and the record → frame bridge."""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd
import pandera.pandas as pa
from pandera.typing import Series
from pydantic import BaseModel

from aqdt.calibration.schemas import CalibratedHourly, CalibrationFit, FitStatus, SensorHourly
from aqdt.observation_store.frames import UTC_NS, empty_frame

_STATUS = [status.value for status in FitStatus]


# @spec CAL-STORE-002, CAL-STORE-003
class SensorHourlyFrame(pa.DataFrameModel):
    site_id: Series[str] = pa.Field(coerce=True)
    hour: Series[pd.DatetimeTZDtype] = pa.Field(dtype_kwargs=UTC_NS)
    pm25_corrected_mean: Series[float] = pa.Field(coerce=True)
    humidity_mean: Series[float] = pa.Field(nullable=True, coerce=True)
    n_snapshots: Series[int] = pa.Field(ge=1, coerce=True)
    n_flagged: Series[int] = pa.Field(ge=0, coerce=True)

    class Config:
        strict = True
        unique = ["site_id", "hour"]


# @spec CAL-STORE-002, CAL-STORE-003
class CalibrationFitFrame(pa.DataFrameModel):
    site_id: Series[str] = pa.Field(coerce=True)
    as_of: Series[pd.DatetimeTZDtype] = pa.Field(dtype_kwargs=UTC_NS)
    status: Series[str] = pa.Field(isin=_STATUS, coerce=True)
    ref_site_id: Series[str] = pa.Field(nullable=True, coerce=True)
    distance_m: Series[float] = pa.Field(nullable=True, coerce=True)
    window_start: Series[pd.DatetimeTZDtype] = pa.Field(dtype_kwargs=UTC_NS)
    window_end: Series[pd.DatetimeTZDtype] = pa.Field(dtype_kwargs=UTC_NS)
    n_pairs: Series[int] = pa.Field(ge=0, coerce=True)
    slope: Series[float] = pa.Field(nullable=True, coerce=True)
    intercept: Series[float] = pa.Field(nullable=True, coerce=True)
    r2: Series[float] = pa.Field(nullable=True, coerce=True)
    rmse: Series[float] = pa.Field(nullable=True, coerce=True)

    class Config:
        strict = True
        unique = ["site_id", "as_of"]


# @spec CAL-STORE-002, CAL-STORE-003
class CalibratedHourlyFrame(pa.DataFrameModel):
    site_id: Series[str] = pa.Field(coerce=True)
    hour: Series[pd.DatetimeTZDtype] = pa.Field(dtype_kwargs=UTC_NS)
    pm25_calibrated: Series[float] = pa.Field(nullable=True, coerce=True)
    pm25_corrected_mean: Series[float] = pa.Field(coerce=True)
    n_snapshots: Series[int] = pa.Field(ge=1, coerce=True)
    fit_as_of: Series[pd.DatetimeTZDtype] = pa.Field(dtype_kwargs=UTC_NS, nullable=True)
    fit_status: Series[str] = pa.Field(isin=_STATUS, coerce=True)

    class Config:
        strict = True
        unique = ["site_id", "hour"]


_FRAME_FOR: dict[type[BaseModel], type[pa.DataFrameModel]] = {
    SensorHourly: SensorHourlyFrame,
    CalibrationFit: CalibrationFitFrame,
    CalibratedHourly: CalibratedHourlyFrame,
}


def _datetime_utc(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, utc=True).astype("datetime64[ns, UTC]")


def records_to_frame(records: Iterable[BaseModel], model: type[BaseModel]) -> pd.DataFrame:
    """A validated frame of the records, with the frame model's dtypes (typed and empty when
    there are no records)."""
    frame_model = _FRAME_FOR[model]
    rows = [record.model_dump(mode="python") for record in records]
    if not rows:
        return empty_frame(frame_model)
    frame = pd.DataFrame(rows, columns=list(model.model_fields))
    for name, column in frame_model.to_schema().columns.items():
        dtype = str(column.dtype)
        if "datetime64" in dtype:
            frame[name] = _datetime_utc(frame[name])
        elif "float" in dtype:
            frame[name] = frame[name].astype("float64")
        elif "int" in dtype:
            frame[name] = frame[name].astype("int64")
        elif isinstance(frame[name].dtype, object):
            frame[name] = frame[name].map(lambda v: v.value if isinstance(v, FitStatus) else v)
    return frame_model.validate(frame)
