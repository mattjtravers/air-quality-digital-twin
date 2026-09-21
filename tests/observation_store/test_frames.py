"""Pandera frame models — OBS-FRAME."""

from datetime import timedelta

import pandas as pd
import pytest
from pandera.errors import SchemaError, SchemaErrors
from shapely.geometry import Point

from aqdt.observation_store.frames import (
    ObservationsFrame,
    SitesFrame,
    observations_to_frame,
    sites_to_frame,
    validate_partition,
)
from aqdt.observation_store.schemas import Observation, QcFlag, Site

from .conftest import T0, make_observation, make_site

FrameError = (SchemaError, SchemaErrors)


@pytest.fixture
def observations():
    return observations_to_frame(
        [
            make_observation(site_id="purpleair:1", observed_at=T0),
            make_observation(site_id="purpleair:1", observed_at=T0 + timedelta(minutes=2)),
            make_observation(site_id="purpleair:2", observed_at=T0, latitude=38.95),
        ]
    )


@pytest.fixture
def sites():
    return sites_to_frame(
        [
            make_site(site_id="purpleair:1"),
            make_site(site_id="purpleair:2", latitude=38.95),
        ]
    )


# @spec OBS-FRAME-001
def test_frame_columns_are_record_fields_plus_geometry(observations, sites):
    assert set(observations.columns) == set(Observation.model_fields) | {"geometry"}
    assert set(sites.columns) == set(Site.model_fields) | {"geometry"}
    assert ObservationsFrame.validate(observations) is not None
    assert SitesFrame.validate(sites) is not None


# @spec OBS-FRAME-002
@pytest.mark.parametrize(
    "record, frame_model",
    [(Site, SitesFrame), (Observation, ObservationsFrame)],
)
def test_record_and_frame_models_conform(record, frame_model):
    """Every record field is a frame column with matching nullability; no extra data columns."""
    columns = frame_model.to_schema().columns
    assert set(columns) == set(record.model_fields) | {"geometry"}
    for name, field in record.model_fields.items():
        record_nullable = type(None) in _args(field.annotation)
        assert columns[name].nullable == record_nullable, name
        assert _compatible(field.annotation, str(columns[name].dtype)), (
            name,
            field.annotation,
            columns[name].dtype,
        )


def _args(annotation):
    import types
    import typing

    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        return typing.get_args(annotation)
    return (annotation,)


def _compatible(annotation, dtype: str) -> bool:
    import datetime
    import enum

    inner = [a for a in _args(annotation) if a is not type(None)][0]
    origin = getattr(inner, "__origin__", inner)
    if inner is float:
        return "float" in dtype
    if inner is int:
        return "int" in dtype
    if inner is str or (isinstance(inner, type) and issubclass(inner, enum.Enum)):
        return dtype == "object" or dtype.startswith(("str", "string"))
    if inner is datetime.datetime:
        return "datetime64" in dtype and "UTC" in dtype
    if origin in (list, dict):
        return dtype == "object"
    return False


# @spec OBS-FRAME-003
def test_duplicate_site_id_observed_at_is_rejected(observations):
    dup = pd.concat([observations, observations.iloc[[0]]], ignore_index=True)
    with pytest.raises(FrameError):
        ObservationsFrame.validate(dup)


# @spec OBS-FRAME-004
def test_duplicate_site_id_in_sites_is_rejected(sites):
    dup = pd.concat([sites, sites.iloc[[0]]], ignore_index=True)
    with pytest.raises(FrameError):
        SitesFrame.validate(dup)


# @spec OBS-FRAME-005
def test_sortedness_is_required_for_partitions_only(observations, sites):
    unsorted_obs = observations.iloc[::-1].reset_index(drop=True)
    unsorted_sites = sites.iloc[::-1].reset_index(drop=True)
    # downstream frames: order is free
    ObservationsFrame.validate(unsorted_obs)
    SitesFrame.validate(unsorted_sites)
    # partitions: order is an invariant
    validate_partition(observations, ObservationsFrame)
    validate_partition(sites, SitesFrame)
    with pytest.raises(FrameError):
        validate_partition(unsorted_obs, ObservationsFrame)
    with pytest.raises(FrameError):
        validate_partition(unsorted_sites, SitesFrame)


# @spec OBS-FRAME-006
def test_geometry_must_be_wgs84_points_matching_coordinates(observations, sites):
    wrong_crs = observations.set_crs("EPSG:3857", allow_override=True)
    with pytest.raises(FrameError):
        ObservationsFrame.validate(wrong_crs)

    moved = observations.copy()
    moved.loc[0, "geometry"] = Point(0.0, 0.0)
    with pytest.raises(FrameError):
        ObservationsFrame.validate(moved)

    moved_site = sites.copy()
    moved_site.loc[0, "geometry"] = Point(0.0, 0.0)
    with pytest.raises(FrameError):
        SitesFrame.validate(moved_site)


# @spec OBS-FRAME-007
def test_observed_at_must_be_tz_aware_utc(observations):
    naive = observations.copy()
    naive["observed_at"] = naive["observed_at"].dt.tz_localize(None)
    with pytest.raises(FrameError):
        ObservationsFrame.validate(naive)


# @spec OBS-FRAME-008
def test_qc_flags_elements_must_be_vocabulary_members(observations):
    bad = observations.copy()
    bad.at[0, "qc_flags"] = ["bogus_flag"]
    with pytest.raises(FrameError):
        ObservationsFrame.validate(bad)


# @spec OBS-FRAME-009
def test_single_channel_requires_channel_missing_flag():
    one_channel = observations_to_frame(
        [make_observation(pm25_channel_b=None, pm25_corrected=None, qc_flags=[])]
    )
    with pytest.raises(FrameError):
        ObservationsFrame.validate(one_channel)

    flagged = observations_to_frame(
        [
            make_observation(
                pm25_channel_b=None, pm25_corrected=None, qc_flags=[QcFlag.channel_missing]
            )
        ]
    )
    ObservationsFrame.validate(flagged)

    both_null = observations_to_frame(
        [make_observation(pm25_channel_a=None, pm25_channel_b=None, pm25_corrected=None)]
    )
    ObservationsFrame.validate(both_null)


# @spec OBS-FRAME-010
def test_coordinate_bounds_are_checked_on_frames(observations, sites):
    for frame, model in ((observations, ObservationsFrame), (sites, SitesFrame)):
        bad = frame.copy()
        bad.loc[0, "latitude"] = 95.0
        with pytest.raises(FrameError):
            model.validate(bad)


# @spec OBS-FRAME-011
def test_validation_error_names_the_check_and_rows(observations):
    bad = observations.copy()
    bad.loc[1, "latitude"] = 95.0
    with pytest.raises(FrameError) as excinfo:
        ObservationsFrame.validate(bad)
    message = str(excinfo.value)
    assert "latitude" in message
    assert "95" in message
