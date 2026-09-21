"""Calibration record models and settings.

Three products: ``SensorHourly`` (a sensor's trusted snapshots aggregated to an hour),
``CalibrationFit`` (one row per sensor per ``as_of``), and ``CalibratedHourly`` (a sensor hour
on the reference monitors' scale). Every datetime is timezone-aware UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class FitStatus(StrEnum):
    fitted = "fitted"  # per-sensor fit accepted
    pooled = "pooled"  # the network-wide fit over accepted sensors' pairs
    uncalibrated = "uncalibrated"  # no accepted fit applies


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def hour_bound(value: datetime, name: str) -> datetime:
    """A run bound: timezone-aware, converted to UTC, truncated to the hour."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


# @spec CAL-STORE-001
class SensorHourly(BaseModel):
    """One sensor's trusted, corrected snapshots in ``[hour, hour + 1 h)``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    site_id: str
    hour: datetime
    pm25_corrected_mean: float
    humidity_mean: float | None
    n_snapshots: int = Field(ge=1)
    n_flagged: int = Field(ge=0)

    _utc = field_validator("hour")(_require_utc)


# @spec CAL-STORE-001
class CalibrationFit(BaseModel):
    """A sensor's fit for one ``as_of``: the reference it was matched to, the half-open fitting
    window, and the coefficients and diagnostics (null when ``uncalibrated``)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    site_id: str
    as_of: datetime
    status: FitStatus
    ref_site_id: str | None
    distance_m: float | None
    window_start: datetime
    window_end: datetime
    n_pairs: int = Field(ge=0)
    slope: float | None
    intercept: float | None
    r2: float | None
    rmse: float | None

    _utc = field_validator("as_of", "window_start", "window_end")(_require_utc)


# @spec CAL-STORE-001
class CalibratedHourly(BaseModel):
    """A sensor hour with the most recent applicable fit applied."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    site_id: str
    hour: datetime
    pm25_calibrated: float | None
    pm25_corrected_mean: float
    n_snapshots: int = Field(ge=1)
    fit_as_of: datetime | None
    fit_status: FitStatus

    _utc = field_validator("hour")(_require_utc)

    @field_validator("fit_as_of")
    @classmethod
    def _optional_utc(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)


# @spec CAL-CFG-001, CAL-CFG-002
class CalibrationSettings(BaseSettings):
    """``AQDT_CAL_*`` environment variables, all optional, all strictly positive."""

    model_config = SettingsConfigDict(env_prefix="AQDT_CAL_", extra="ignore", frozen=True)

    max_distance_m: float = Field(default=10_000, gt=0)
    window_days: int = Field(default=30, gt=0)
    min_pairs: int = Field(default=72, gt=0)
