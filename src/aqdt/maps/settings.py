"""Map colour limits: named defaults the environment may override."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Upper colour limit for PM2.5 maps, µg/m³ (maps LLD § Decisions).
PM25_MAX = 12.0
#: Upper colour limit for standard-deviation maps, µg/m³.
SD_MAX = 3.0


# @spec MAP-CFG-001, MAP-CFG-002
class MapSettings(BaseSettings):
    """``AQDT_MAP_PM25_MAX`` and ``AQDT_MAP_SD_MAX``, both optional."""

    model_config = SettingsConfigDict(env_prefix="AQDT_MAP_", extra="ignore", frozen=True)

    pm25_max: float = Field(default=PM25_MAX, gt=0)
    sd_max: float = Field(default=SD_MAX, gt=0)
