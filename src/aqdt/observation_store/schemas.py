"""Canonical record models, enumerations, and the shared QC flag vocabulary.

These Pydantic models are the source of truth for column names, types, and nullability. The
Pandera frame models in ``frames.py`` restate them for dataframes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    import geopandas as gpd


# @spec OBS-SCHEMA-001
class Source(StrEnum):
    purpleair = "purpleair"
    airnow = "airnow"


class SiteType(StrEnum):
    low_cost_sensor = "low_cost_sensor"
    reference_monitor = "reference_monitor"


# @spec OBS-FLAG-001, OBS-FLAG-002, OBS-FLAG-003
class QcFlag(StrEnum):
    """The registry of what a flag means. Ingesters raise only the flags that apply to them.

    Every flag is a function of the observation and its neighbours in the archive, never of when
    the run happened; staleness is a query-time property, not a flag.
    """

    missing_value = "missing_value"  # the primary PM2.5 value is absent
    out_of_range = "out_of_range"  # a value lies outside physically plausible bounds
    channel_missing = "channel_missing"  # a dual-channel sensor reported only one channel
    channel_disagreement = "channel_disagreement"  # channels differ beyond published criteria
    flatline = "flatline"  # identical values across a run at least the source's threshold long
    site_id_unresolved = "site_id_unresolved"  # the site identifier could not be normalized


def _require_utc(value: datetime) -> datetime:
    """Reject naive datetimes; convert tz-aware non-UTC ones to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


# @spec OBS-SCHEMA-002, OBS-SCHEMA-004, OBS-SCHEMA-006, OBS-SCHEMA-009
class Site(BaseModel):
    """A fixed measurement location; one row per ``site_id`` holding the latest metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    site_id: str
    source: Source
    source_native_id: str
    site_type: SiteType
    name: str | None
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


# @spec OBS-SCHEMA-003, OBS-SCHEMA-004, OBS-SCHEMA-005, OBS-SCHEMA-006, OBS-SCHEMA-007
# @spec OBS-SCHEMA-008, OBS-SCHEMA-009, OBS-SCHEMA-013
class Observation(BaseModel):
    """One measurement of PM2.5 at one site at one instant. Key: ``(site_id, observed_at)``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    site_id: str
    source: Source
    observed_at: datetime
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    pm25_raw: float | None
    pm25_channel_a: float | None
    pm25_channel_b: float | None
    humidity: float | None
    pm25_corrected: float | None
    qc_flags: list[QcFlag]
    raw: dict[str, Any]

    @field_validator("observed_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("qc_flags")
    @classmethod
    def _sorted_unique(cls, value: list[QcFlag]) -> list[QcFlag]:
        return sorted(set(value), key=lambda flag: flag.value)

    @property
    def is_trusted(self) -> bool:
        return not self.qc_flags

    @classmethod
    def from_frame(cls, frame: gpd.GeoDataFrame) -> list[Observation]:
        """The records for every row of a validated ``ObservationsFrame``."""
        columns = [name for name in cls.model_fields]
        records = []
        for row in frame[columns].itertuples(index=False):
            data = dict(zip(columns, row, strict=True))
            for key in (
                "pm25_raw",
                "pm25_channel_a",
                "pm25_channel_b",
                "humidity",
                "pm25_corrected",
            ):
                if data[key] != data[key]:  # NaN -> None
                    data[key] = None
            data["observed_at"] = data["observed_at"].to_pydatetime()
            data["qc_flags"] = list(data["qc_flags"])
            records.append(cls(**data))
        return records


# @spec OBS-SCHEMA-010, OBS-SCHEMA-014, OBS-SCHEMA-015
class IngestSummary(BaseModel):
    """What every ingester returns from a run, so callers see one shape.

    Invariant: ``fetched == written + sum(rejected.values())`` — every source row is either an
    observation handed to the store or a counted boundary rejection. ``written`` includes any
    hours re-emitted for lookback. Windows are half-open ``[window_start, window_end)``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Source
    fetched: int = Field(ge=0)
    rejected: dict[str, int]
    written: int = Field(ge=0)
    flagged: dict[QcFlag, int]
    partitions: list[str]
    snapshot_at: datetime | None
    window_start: datetime | None
    window_end: datetime | None

    @model_validator(mode="after")
    def _accounted_for(self) -> IngestSummary:
        rejected = sum(self.rejected.values())
        if self.fetched != self.written + rejected:
            raise ValueError(
                f"fetched ({self.fetched}) != written ({self.written}) + rejected ({rejected})"
            )
        if (
            self.window_start is not None
            and self.window_end is not None
            and self.window_end <= self.window_start
        ):
            raise ValueError("window_end must be after window_start (half-open window)")
        return self


# @spec OBS-SCHEMA-011, OBS-SCHEMA-012
class BoundingBox(BaseModel):
    """The spatial extent every ingester queries: NW and SE corners in WGS84 decimal degrees."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    nwlng: float = Field(ge=-180, le=180)
    nwlat: float = Field(ge=-90, le=90)
    selng: float = Field(ge=-180, le=180)
    selat: float = Field(ge=-90, le=90)

    @model_validator(mode="before")
    @classmethod
    def _from_string(cls, value: Any) -> Any:
        if isinstance(value, str):
            parts = [p.strip() for p in value.split(",")]
            if len(parts) != 4:
                raise ValueError("bounding box must be 'nwlng,nwlat,selng,selat'")
            return dict(zip(("nwlng", "nwlat", "selng", "selat"), parts, strict=True))
        return value

    @model_validator(mode="after")
    def _oriented(self) -> BoundingBox:
        if not self.nwlat > self.selat:
            raise ValueError("nwlat must be greater than selat")
        if not self.nwlng < self.selng:
            raise ValueError("nwlng must be less than selng")
        return self

    @classmethod
    def parse(cls, text: str) -> BoundingBox:
        """Parse the ``AQDT_BBOX`` form ``nwlng,nwlat,selng,selat``."""
        return cls.model_validate(text)


# @spec OBS-SCHEMA-016
DC_METRO = BoundingBox(nwlng=-77.5, nwlat=39.1, selng=-76.7, selat=38.7)
"""The project's extent: the Washington, D.C. metro. Ingester settings default to it; ``AQDT_BBOX``
overrides it for an experiment over another extent."""


# @spec OBS-ENV-009
ARCHIVE_BUCKET = "air-quality-digital-twin-585949919812-us-east-1-archive"
"""The bucket the archive lives in, declared by the foundation stack and asserted equal to this
constant by a test. A code default for the same reason as ``DC_METRO``: which bucket holds the
system of record is a fact about the project, not a deployment detail."""
