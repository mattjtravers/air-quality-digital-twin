"""Record models and enumerations — OBS-SCHEMA and OBS-FLAG."""

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from aqdt.observation_store.schemas import (
    BoundingBox,
    IngestSummary,
    Observation,
    QcFlag,
    Site,
    SiteType,
    Source,
)

from .conftest import T0, make_observation, make_site

# --- Enumerations -------------------------------------------------------------


# @spec OBS-SCHEMA-001
def test_source_and_site_type_members():
    assert {m.value for m in Source} == {"purpleair", "airnow"}
    assert {m.value for m in SiteType} == {"low_cost_sensor", "reference_monitor"}


# @spec OBS-FLAG-001
def test_qc_flag_members_are_exactly_the_vocabulary():
    assert {m.value for m in QcFlag} == {
        "missing_value",
        "out_of_range",
        "channel_missing",
        "channel_disagreement",
        "flatline",
        "site_id_unresolved",
    }


# @spec OBS-FLAG-002
def test_qc_flag_serializes_as_its_name():
    obs = make_observation(qc_flags=[QcFlag.channel_disagreement])
    assert obs.model_dump(mode="json")["qc_flags"] == ["channel_disagreement"]
    assert QcFlag.channel_disagreement == "channel_disagreement"


# @spec OBS-FLAG-003
def test_no_staleness_flag_and_no_latest_timestamp_on_site():
    assert not any("stale" in m.value for m in QcFlag)
    assert "last_observed_at" not in Site.model_fields


# --- Site ---------------------------------------------------------------------


# @spec OBS-SCHEMA-002
def test_site_fields_are_exactly_the_schema():
    assert set(Site.model_fields) == {
        "site_id",
        "source",
        "source_native_id",
        "site_type",
        "name",
        "latitude",
        "longitude",
    }
    assert make_site(name=None).name is None


# --- Observation --------------------------------------------------------------


# @spec OBS-SCHEMA-003
def test_observation_fields_are_exactly_the_schema():
    assert set(Observation.model_fields) == {
        "site_id",
        "source",
        "observed_at",
        "latitude",
        "longitude",
        "pm25_raw",
        "pm25_channel_a",
        "pm25_channel_b",
        "humidity",
        "pm25_corrected",
        "qc_flags",
        "raw",
    }
    obs = make_observation(
        pm25_raw=None,
        pm25_channel_a=None,
        pm25_channel_b=None,
        humidity=None,
        pm25_corrected=None,
        qc_flags=[QcFlag.missing_value],
    )
    assert obs.pm25_raw is None and obs.humidity is None


# @spec OBS-SCHEMA-004
@pytest.mark.parametrize(
    "field, value",
    [("latitude", 90.0001), ("latitude", -91), ("longitude", 180.5), ("longitude", -181)],
)
def test_out_of_range_coordinates_are_rejected(field, value):
    with pytest.raises(ValidationError):
        make_site(**{field: value})
    with pytest.raises(ValidationError):
        make_observation(**{field: value})


# @spec OBS-SCHEMA-005
def test_naive_observed_at_is_rejected():
    with pytest.raises(ValidationError):
        make_observation(observed_at=datetime(2026, 9, 20, 12, 0))


# @spec OBS-SCHEMA-005
def test_non_utc_observed_at_is_converted_to_utc():
    eastern = timezone(timedelta(hours=-4))
    obs = make_observation(observed_at=datetime(2026, 9, 20, 8, 0, tzinfo=eastern))
    assert obs.observed_at.utcoffset() == timedelta(0)
    assert obs.observed_at == T0


# @spec OBS-SCHEMA-006
def test_coordinates_keep_full_source_precision():
    lat, lon = 38.907192345678901, -77.036871234567890
    site = make_site(latitude=lat, longitude=lon)
    obs = make_observation(latitude=lat, longitude=lon)
    assert (site.latitude, site.longitude) == (lat, lon)
    assert (obs.latitude, obs.longitude) == (lat, lon)


# @spec OBS-SCHEMA-007
def test_qc_flags_are_deduplicated_and_sorted_by_name():
    obs = make_observation(
        qc_flags=[QcFlag.out_of_range, QcFlag.channel_missing, QcFlag.out_of_range]
    )
    assert obs.qc_flags == [QcFlag.channel_missing, QcFlag.out_of_range]


# @spec OBS-SCHEMA-008
def test_is_trusted_iff_no_flags():
    assert make_observation(qc_flags=[]).is_trusted is True
    assert make_observation(qc_flags=[QcFlag.flatline]).is_trusted is False


# @spec OBS-SCHEMA-009
def test_records_carry_no_run_identity_and_compare_equal():
    for model in (Site, Observation):
        for name in model.model_fields:
            assert "ingested" not in name and "run" not in name and "host" not in name
    assert make_observation() == make_observation()
    assert make_site() == make_site()


# --- IngestSummary ------------------------------------------------------------


def _summary(**overrides):
    data = {
        "source": Source.purpleair,
        "fetched": 5,
        "rejected": {"missing_sensor_index": 1},
        "written": 4,
        "flagged": {QcFlag.out_of_range: 2},
        "partitions": ["file:///tmp/a"],
        "snapshot_at": T0,
        "window_start": None,
        "window_end": None,
    }
    data.update(overrides)
    return IngestSummary(**data)


# @spec OBS-SCHEMA-010
def test_ingest_summary_fields():
    assert set(IngestSummary.model_fields) == {
        "source",
        "fetched",
        "rejected",
        "written",
        "flagged",
        "partitions",
        "snapshot_at",
        "window_start",
        "window_end",
    }
    summary = _summary()
    assert summary.flagged[QcFlag.out_of_range] == 2
    assert summary.rejected == {"missing_sensor_index": 1}


# @spec OBS-SCHEMA-014
def test_ingest_summary_enforces_fetched_equals_written_plus_rejected():
    assert _summary(fetched=5, written=4, rejected={"a": 1}).fetched == 5
    assert _summary(fetched=0, written=0, rejected={}).written == 0
    with pytest.raises(ValidationError):
        _summary(fetched=5, written=4, rejected={"a": 2})


# @spec OBS-SCHEMA-015
def test_ingest_summary_window_is_half_open():
    end = T0 + timedelta(hours=24)
    summary = _summary(snapshot_at=None, window_start=T0, window_end=end)
    assert summary.window_start == T0 and summary.window_end == end
    with pytest.raises(ValidationError):
        _summary(snapshot_at=None, window_start=end, window_end=T0)


# --- BoundingBox --------------------------------------------------------------


# @spec OBS-SCHEMA-011
def test_bounding_box_parses_from_nwlng_nwlat_selng_selat():
    bbox = BoundingBox.parse("-77.5,39.1,-76.7,38.7")
    assert (bbox.nwlng, bbox.nwlat, bbox.selng, bbox.selat) == (-77.5, 39.1, -76.7, 38.7)
    assert BoundingBox(nwlng=-77.5, nwlat=39.1, selng=-76.7, selat=38.7) == bbox


# @spec OBS-SCHEMA-012
@pytest.mark.parametrize(
    "text",
    [
        "-77.5,38.7,-76.7,39.1",  # nwlat <= selat
        "-76.7,39.1,-77.5,38.7",  # nwlng >= selng
        "-77.5,91,-76.7,38.7",  # latitude out of range
        "-181,39.1,-76.7,38.7",  # longitude out of range
        "-77.5,39.1,-76.7",  # wrong arity
    ],
)
def test_invalid_bounding_box_is_rejected(text):
    with pytest.raises(ValidationError):
        BoundingBox.parse(text)


# --- from_frame ---------------------------------------------------------------


# @spec OBS-SCHEMA-013
def test_observation_from_frame_round_trips():
    from aqdt.observation_store.frames import observations_to_frame

    records = [
        make_observation(site_id="purpleair:1", qc_flags=[QcFlag.out_of_range]),
        make_observation(site_id="purpleair:2", observed_at=T0 + timedelta(minutes=2)),
    ]
    frame = observations_to_frame(records)
    assert Observation.from_frame(frame) == records
