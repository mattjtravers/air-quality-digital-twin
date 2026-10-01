"""Fusion record models and settings.

One product, ``SurfaceFit`` — one row per hour describing the hour's variogram fit and the raster
it produced (or why there is none). Every datetime is timezone-aware UTC.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from aqdt.calibration.schemas import _require_utc
from aqdt.observation_store.schemas import DC_METRO, BoundingBox

#: Grid resolution in metres when ``AQDT_FUS_RESOLUTION_M`` is unset (fusion LLD § Decisions).
DEFAULT_RESOLUTION_M = 100


class SurfaceStatus(StrEnum):
    kriged = "kriged"  # a variogram was eligible and the surface was predicted
    insufficient_points = "insufficient_points"  # fewer than min_points distinct locations
    fit_failed = "fit_failed"  # no candidate variogram was eligible


# @spec FUS-STORE-001
class SurfaceFit(BaseModel):
    """An hour's surface: its points, its variogram, and its raster — the last two null unless
    ``status`` is ``kriged``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    hour: datetime
    status: SurfaceStatus
    n_sensors: int = Field(ge=0)
    n_points: int = Field(ge=0)
    variogram_model: str | None
    sill: float | None
    range_m: float | None
    nugget: float | None
    q1: float | None
    q2: float | None
    cr: float | None
    resolution_m: float | None
    width: int | None
    height: int | None
    surface_uri: str | None
    surface_sha256: str | None

    _utc = field_validator("hour")(_require_utc)


# @spec FUS-CFG-001, FUS-CFG-002, FUS-GRID-003
class FusionSettings(BaseSettings):
    """``AQDT_FUS_*`` environment variables and the shared ``AQDT_BBOX``, all optional."""

    model_config = SettingsConfigDict(
        env_prefix="AQDT_FUS_",
        validate_by_name=True,
        validate_by_alias=True,
        extra="ignore",
        frozen=True,
    )

    resolution_m: float = Field(default=DEFAULT_RESOLUTION_M, gt=0)
    min_points: int = Field(default=10, ge=3)
    bbox: Annotated[BoundingBox, NoDecode] = Field(default=DC_METRO, validation_alias="AQDT_BBOX")
