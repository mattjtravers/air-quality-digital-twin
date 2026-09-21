"""Pandera frame models: the GeoDataFrame shape of the canonical records, plus frame invariants.

The models never check row order; ``validate_partition`` adds the sortedness check that applies
to archive partitions only. ``observations_to_frame`` / ``sites_to_frame`` build the GeoDataFrame
from records (``raw`` stays a dict column; JSON serialization happens at the archive boundary),
and ``Observation.from_frame`` is the inverse.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import geopandas as gpd
import pandas as pd
import pandera.pandas as pa
from pandera.errors import SchemaError
from pandera.typing import Series
from pandera.typing.geopandas import GeoSeries
from shapely.geometry import Point

from aqdt.observation_store.schemas import Observation, QcFlag, Site

CRS = "EPSG:4326"
UTC_NS = {"unit": "ns", "tz": "UTC"}
_QC_FLAGS = {flag.value for flag in QcFlag}


def _geometry_matches_coordinates(frame: pd.DataFrame) -> bool:
    if not isinstance(frame, gpd.GeoDataFrame) or frame.crs is None or frame.crs.to_epsg() != 4326:
        return False
    geometry = frame.geometry
    if len(frame) == 0:
        return True
    if not (geometry.geom_type == "Point").all():
        return False
    return bool(
        (geometry.x == frame["longitude"]).all() and (geometry.y == frame["latitude"]).all()
    )


# @spec OBS-FRAME-001, OBS-FRAME-004, OBS-FRAME-006, OBS-FRAME-010
class SitesFrame(pa.DataFrameModel):
    site_id: Series[str] = pa.Field(unique=True, coerce=True)
    source: Series[str] = pa.Field(coerce=True)
    source_native_id: Series[str] = pa.Field(coerce=True)
    site_type: Series[str] = pa.Field(coerce=True)
    name: Series[str] = pa.Field(nullable=True, coerce=True)
    latitude: Series[float] = pa.Field(ge=-90, le=90, coerce=True)
    longitude: Series[float] = pa.Field(ge=-180, le=180, coerce=True)
    geometry: GeoSeries

    class Config:
        strict = True

    @pa.dataframe_check(error="geometry must be EPSG:4326 points equal to (longitude, latitude)")
    def geometry_matches(cls, frame: pd.DataFrame) -> bool:
        return _geometry_matches_coordinates(frame)


# @spec OBS-FRAME-001, OBS-FRAME-003, OBS-FRAME-006, OBS-FRAME-007, OBS-FRAME-008
# @spec OBS-FRAME-009, OBS-FRAME-010
class ObservationsFrame(pa.DataFrameModel):
    site_id: Series[str] = pa.Field(coerce=True)
    source: Series[str] = pa.Field(coerce=True)
    # never coerced: coercion would silently localize a naive column as UTC (OBS-FRAME-007)
    observed_at: Series[pd.DatetimeTZDtype] = pa.Field(dtype_kwargs=UTC_NS)
    latitude: Series[float] = pa.Field(ge=-90, le=90, coerce=True)
    longitude: Series[float] = pa.Field(ge=-180, le=180, coerce=True)
    pm25_raw: Series[float] = pa.Field(nullable=True, coerce=True)
    pm25_channel_a: Series[float] = pa.Field(nullable=True, coerce=True)
    pm25_channel_b: Series[float] = pa.Field(nullable=True, coerce=True)
    humidity: Series[float] = pa.Field(nullable=True, coerce=True)
    pm25_corrected: Series[float] = pa.Field(nullable=True, coerce=True)
    qc_flags: Series[object]
    raw: Series[object]
    geometry: GeoSeries

    class Config:
        strict = True
        unique = ["site_id", "observed_at"]

    @pa.check("qc_flags", element_wise=True, error="qc_flags elements must be QcFlag members")
    def flags_are_vocabulary(cls, flags: Any) -> bool:
        return all(flag in _QC_FLAGS for flag in flags)

    @pa.dataframe_check(error="geometry must be EPSG:4326 points equal to (longitude, latitude)")
    def geometry_matches(cls, frame: pd.DataFrame) -> bool:
        return _geometry_matches_coordinates(frame)

    @pa.dataframe_check(error="exactly one channel present requires the channel_missing flag")
    def single_channel_is_flagged(cls, frame: pd.DataFrame) -> pd.Series:
        one_channel = frame["pm25_channel_a"].isna() != frame["pm25_channel_b"].isna()
        flagged = frame["qc_flags"].map(lambda flags: QcFlag.channel_missing.value in flags)
        return ~one_channel | flagged


_SORT_KEYS: dict[type[pa.DataFrameModel], list[str]] = {
    SitesFrame: ["site_id"],
    ObservationsFrame: ["site_id", "observed_at"],
}


def sort_key(model: type[pa.DataFrameModel]) -> list[str]:
    """The columns an archive partition of this model is sorted by (its merge key)."""
    return _SORT_KEYS.get(model) or list(model.to_schema().unique or [])


# @spec OBS-FRAME-005, OBS-FRAME-011
def validate_partition(frame: pd.DataFrame, model: type[pa.DataFrameModel]) -> pd.DataFrame:
    """Validate a frame that is (or will be) an archive partition: the model plus row order."""
    validated = model.validate(frame)
    keys = sort_key(model)
    if keys and len(validated) > 1:
        expected = validated.sort_values(keys, kind="stable").index
        if not validated.index.equals(expected):
            raise SchemaError(
                model.to_schema(),
                validated,
                f"partition rows must be sorted by {keys}",
            )
    return validated


def _with_geometry(frame: pd.DataFrame) -> gpd.GeoDataFrame:
    points = [Point(x, y) for x, y in zip(frame["longitude"], frame["latitude"], strict=True)]
    return gpd.GeoDataFrame(frame, geometry=points, crs=CRS)


def _empty(model: type[pa.DataFrameModel]) -> gpd.GeoDataFrame:
    schema = model.to_schema()
    columns = {}
    for name, column in schema.columns.items():
        if name == "geometry":
            continue
        dtype = str(column.dtype)
        columns[name] = pd.Series([], dtype="object" if dtype == "object" else dtype)
    frame = pd.DataFrame(columns)
    return gpd.GeoDataFrame(frame, geometry=gpd.GeoSeries([], crs=CRS), crs=CRS)


def empty_frame(model: type[pa.DataFrameModel]) -> pd.DataFrame:
    """An empty frame with the model's columns and dtypes (a GeoDataFrame when it has geometry)."""
    if "geometry" in model.to_schema().columns:
        return _empty(model)
    columns = {
        name: pd.Series([], dtype=str(column.dtype))
        for name, column in model.to_schema().columns.items()
    }
    return pd.DataFrame(columns)


def sites_to_frame(records: Iterable[Site]) -> gpd.GeoDataFrame:
    rows = [record.model_dump(mode="json") for record in records]
    if not rows:
        return _empty(SitesFrame)
    frame = pd.DataFrame(rows, columns=list(Site.model_fields))
    return _with_geometry(frame)


def observations_to_frame(records: Iterable[Observation]) -> gpd.GeoDataFrame:
    rows = []
    for record in records:
        row = record.model_dump(mode="python")
        row["source"] = record.source.value
        row["qc_flags"] = [flag.value for flag in record.qc_flags]
        rows.append(row)
    if not rows:
        return _empty(ObservationsFrame)
    frame = pd.DataFrame(rows, columns=list(Observation.model_fields))
    for name in ("pm25_raw", "pm25_channel_a", "pm25_channel_b", "humidity", "pm25_corrected"):
        frame[name] = frame[name].astype("float64")
    frame["observed_at"] = pd.to_datetime(frame["observed_at"], utc=True).astype(
        "datetime64[ns, UTC]"
    )
    return _with_geometry(frame)
