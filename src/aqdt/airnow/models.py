"""AirNow boundary model and settings.

``AirNowRow`` validates one ``/aq/data/`` response row; a row that fails it is a boundary
rejection. ``AirNowSettings`` is the environment-only configuration of the ingester.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from aqdt.observation_store.schemas import BoundingBox

MISSING_SENTINEL = -999  # AirNow's missing-value marker
PARAMETER = "PM2.5"
UNIT = "UG/M3"
UTC_FORMAT = "%Y-%m-%dT%H:%M"


# @spec AN-MODEL-001, AN-MODEL-002, AN-MODEL-003, AN-MODEL-004, AN-MODEL-005, AN-MODEL-007
class AirNowRow(BaseModel):
    """One ``/aq/data/`` row. A null ``Value`` (``-999``) is valid here; it becomes a flag."""

    model_config = ConfigDict(frozen=True)

    utc: datetime = Field(alias="UTC")
    parameter: str = Field(alias="Parameter")
    latitude: float = Field(alias="Latitude", ge=-90, le=90)
    longitude: float = Field(alias="Longitude", ge=-180, le=180)
    value: float | None = Field(alias="Value")
    raw_concentration: float | None = Field(default=None, alias="RawConcentration")
    aqi: int | None = Field(default=None, alias="AQI")
    category: int | None = Field(default=None, alias="Category")
    unit: str = Field(alias="Unit")
    site_name: str | None = Field(default=None, alias="SiteName")
    agency_name: str | None = Field(default=None, alias="AgencyName")
    full_aqs_code: str | None = Field(default=None, alias="FullAQSCode")
    intl_aqs_code: str | None = Field(default=None, alias="IntlAQSCode")

    @field_validator("utc", mode="before")
    @classmethod
    def _hour_stamp(cls, value: Any) -> datetime:
        if not isinstance(value, str):
            raise ValueError(f"UTC must be a {UTC_FORMAT} string")
        return datetime.strptime(value, UTC_FORMAT).replace(tzinfo=UTC)

    @field_validator("parameter")
    @classmethod
    def _pm25(cls, value: str) -> str:
        if value != PARAMETER:
            raise ValueError(f"Parameter must be {PARAMETER}")
        return value

    @field_validator("unit")
    @classmethod
    def _ug_m3(cls, value: str) -> str:
        if value != UNIT:
            raise ValueError(f"Unit must be {UNIT}")
        return value

    @field_validator("value", "raw_concentration", "aqi", mode="before")
    @classmethod
    def _sentinel_to_none(cls, value: Any) -> Any:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return None if value == MISSING_SENTINEL else value
        return value

    @field_validator("full_aqs_code", "intl_aqs_code", mode="before")
    @classmethod
    def _stripped_string(cls, value: Any) -> str | None:
        if value is None:
            return None
        return str(value).strip()


# @spec AN-CFG-001, AN-CFG-002, AN-CFG-003
class AirNowSettings(BaseSettings):
    """Environment-only configuration: the two required variables plus overridable defaults.

    Defaults are overridable from the environment as ``AQDT_AIRNOW_<FIELD>`` or by keyword.
    """

    model_config = SettingsConfigDict(
        env_prefix="AQDT_AIRNOW_",
        validate_by_name=True,
        validate_by_alias=True,
        extra="ignore",
        frozen=True,
    )

    api_key: str = Field(validation_alias="AIRNOW_API_KEY")
    bbox: Annotated[BoundingBox, NoDecode] = Field(validation_alias="AQDT_BBOX")
    flatline_hours: int = Field(default=3, ge=1)
    base_url: str = "https://www.airnowapi.org/aq/data/"
    timeout_seconds: float = 30
    max_attempts: int = Field(default=3, ge=1)
    retry_backoff_seconds: float = Field(default=1.0, ge=0)
    chunk_hours: int = Field(default=24, ge=1)
    parameters: str = "PM25"
    data_type: str = "B"
    monitor_type: int = 0
    verbose: int = 1
    include_raw_concentrations: int = 1
    response_format: str = "application/json"
