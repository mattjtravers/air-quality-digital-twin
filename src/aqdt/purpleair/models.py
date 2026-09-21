"""PurpleAir boundary model and settings.

``PurpleAirSensorRecord`` validates one zipped ``/v1/sensors`` row; a row that fails it is a
boundary rejection. ``PurpleAirSettings`` is the environment-only configuration of the ingester.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from aqdt.observation_store.schemas import DC_METRO, BoundingBox

FIELDS = [
    "sensor_index",
    "name",
    "latitude",
    "longitude",
    "last_seen",
    "humidity",
    "pm2.5_cf_1",
    "pm2.5_cf_1_a",
    "pm2.5_cf_1_b",
]


# @spec PA-MODEL-001, PA-MODEL-002, PA-MODEL-003, PA-MODEL-004
class PurpleAirSensorRecord(BaseModel):
    """One zipped ``/v1/sensors`` row.

    Nulls in the PM and humidity fields are valid here — they become QC flags downstream. A
    missing ``sensor_index``, bad coordinates, or an unparseable ``last_seen`` fail validation.
    """

    model_config = ConfigDict(frozen=True)

    sensor_index: int
    name: str | None = None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    last_seen: datetime
    humidity: float | None = None
    pm2_5_cf_1: float | None = Field(default=None, alias="pm2.5_cf_1")
    pm2_5_cf_1_a: float | None = Field(default=None, alias="pm2.5_cf_1_a")
    pm2_5_cf_1_b: float | None = Field(default=None, alias="pm2.5_cf_1_b")

    @field_validator("last_seen", mode="before")
    @classmethod
    def _epoch_seconds(cls, value: Any) -> datetime:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("last_seen must be epoch seconds")
        return datetime.fromtimestamp(value, UTC)


# @spec PA-CFG-001, PA-CFG-002, PA-CFG-003
class PurpleAirSettings(BaseSettings):
    """Environment-only configuration: the two required variables plus overridable defaults.

    Defaults are overridable from the environment as ``AQDT_PURPLEAIR_<FIELD>`` or by keyword.
    """

    model_config = SettingsConfigDict(
        env_prefix="AQDT_PURPLEAIR_",
        validate_by_name=True,
        validate_by_alias=True,
        extra="ignore",
        frozen=True,
    )

    api_key: str = Field(validation_alias="PURPLEAIR_API_KEY")
    bbox: Annotated[BoundingBox, NoDecode] = Field(default=DC_METRO, validation_alias="AQDT_BBOX")
    base_url: str = "https://api.purpleair.com/v1/sensors"
    timeout_seconds: float = 30
    max_attempts: int = Field(default=3, ge=1)
    retry_backoff_seconds: float = Field(default=1.0, ge=0)
    max_age: int = 86400
    location_type: int = 0
    fields: list[str] = FIELDS
