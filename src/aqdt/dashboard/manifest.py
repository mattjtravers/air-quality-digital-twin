"""``data/manifest.json``: everything the page shows, as one validated document."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

#: ISO 8601 UTC to the second, the form every timestamp in the manifest takes.
UtcTimestamp = Annotated[str, Field(pattern=r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")]
Colour = Annotated[str, Field(pattern=r"^#[0-9a-f]{6}$")]
LatLon = tuple[float, float]


class HourStatus(StrEnum):
    kriged = "kriged"  # the fit row says kriged and its raster was read
    insufficient_points = "insufficient_points"
    fit_failed = "fit_failed"
    missing = "missing"  # no fit row for the hour
    surface_missing = "surface_missing"  # the fit row says kriged; its raster could not be read


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Scale(_Model):
    min: float
    max: float = Field(gt=0)
    cmap: str
    units: str
    label: str
    stops: list[Colour] = Field(min_length=2)


class Scales(_Model):
    pm25: Scale
    diff: Scale
    sd: Scale


class Monitor(_Model):
    site_id: str
    name: str | None
    lat: float
    lon: float
    obs: float | None  # pm25_raw, µg/m³
    qc_flags: list[str]
    map_value: float | None  # band 1 at the monitor's cell, µg/m³; null unless kriged
    difference: float | None  # map_value − obs; null when flagged or either is null


class Sensor(_Model):
    sensor_id: str
    lat: float
    lon: float
    pm25_calibrated: float | None  # µg/m³; null for an uncalibrated row
    fit_status: str


class Hour(_Model):
    hour: UtcTimestamp
    status: HourStatus
    pm25_png: str | None
    diff_png: str | None
    sd_png: str | None
    bounds_latlon: tuple[LatLon, LatLon] | None  # [[south, west], [north, east]]
    surface_min: float | None
    surface_mean: float | None
    surface_max: float | None
    sd_max: float | None
    n_sensors: int | None
    variogram_model: str | None
    range_km: float | None  # the variogram's range
    nugget: float | None  # (µg/m³)²
    sill: float | None  # partial sill + nugget, (µg/m³)²
    nugget_share: float | None = Field(ge=0)  # nugget / sill of the hour's variogram
    monitor_n: int = Field(ge=0)  # monitors with a difference
    monitor_mean_difference: float | None  # mean map − observed over them, µg/m³
    monitors: list[Monitor]
    sensors: list[Sensor]


# @spec DASH-MAN-001
class Manifest(_Model):
    generated_at: UtcTimestamp
    latest_hour: UtcTimestamp
    window_start: UtcTimestamp
    window_end: UtcTimestamp  # exclusive
    scales: Scales
    hours: list[Hour]
