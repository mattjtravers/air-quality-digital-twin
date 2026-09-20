"""Payload boundary model, QC flags, and the EPA correction — PA-MODEL, PA-QC, PA-CORR."""

import inspect
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aqdt.observation_store.schemas import QcFlag
from aqdt.purpleair.models import PurpleAirSensorRecord
from aqdt.purpleair.qc import evaluate

from .conftest import row_dict


def record(snapshot, sensor_index, **overrides) -> PurpleAirSensorRecord:
    row = row_dict(snapshot, sensor_index)
    row.update(overrides)
    return PurpleAirSensorRecord.model_validate(row)


# --- Payload model ------------------------------------------------------------


# @spec PA-MODEL-001
def test_record_fields_and_aliases(snapshot):
    rec = record(snapshot, 101)
    assert rec.sensor_index == 101
    assert rec.name == "Healthy"
    assert (rec.latitude, rec.longitude) == (38.9010, -77.0300)
    assert rec.humidity == 55.0
    assert (rec.pm2_5_cf_1, rec.pm2_5_cf_1_a, rec.pm2_5_cf_1_b) == (8.1, 8.0, 8.2)
    assert set(PurpleAirSensorRecord.model_fields) == {
        "sensor_index",
        "name",
        "latitude",
        "longitude",
        "last_seen",
        "humidity",
        "pm2_5_cf_1",
        "pm2_5_cf_1_a",
        "pm2_5_cf_1_b",
    }


# @spec PA-MODEL-002
def test_last_seen_is_epoch_seconds_to_utc(snapshot):
    rec = record(snapshot, 101)
    assert rec.last_seen == datetime(2026, 9, 20, 11, 59, tzinfo=UTC)
    assert rec.last_seen.tzinfo is not None


# @spec PA-MODEL-003
@pytest.mark.parametrize(
    "overrides",
    [
        {"sensor_index": None},
        {"latitude": None},
        {"longitude": "east"},
        {"latitude": 91.0},
        {"longitude": -181.0},
        {"last_seen": None},
        {"last_seen": "yesterday"},
    ],
)
def test_boundary_rejections(snapshot, overrides):
    with pytest.raises(ValidationError):
        record(snapshot, 101, **overrides)


# @spec PA-MODEL-003
def test_missing_sensor_index_key_is_rejected(snapshot):
    row = row_dict(snapshot, 101)
    del row["sensor_index"]
    with pytest.raises(ValidationError):
        PurpleAirSensorRecord.model_validate(row)


# @spec PA-MODEL-004
def test_null_pm_and_humidity_are_valid_at_the_boundary(snapshot):
    nulls = {"humidity": None, "pm2.5_cf_1": None, "pm2.5_cf_1_a": None, "pm2.5_cf_1_b": None}
    rec = record(snapshot, 101, **nulls)
    assert rec.humidity is None and rec.pm2_5_cf_1 is None
    assert rec.pm2_5_cf_1_a is None and rec.pm2_5_cf_1_b is None


# --- QC -----------------------------------------------------------------------


# @spec PA-QC-001
def test_all_applicable_flags_are_raised_together(snapshot):
    rec = record(snapshot, 102, **{"pm2.5_cf_1": None})  # B fault + missing sensor value
    result = evaluate(rec)
    assert result.flags == [
        QcFlag.channel_disagreement,
        QcFlag.missing_value,
        QcFlag.out_of_range,
    ]


# @spec PA-QC-002
def test_missing_value_when_sensor_level_pm_is_null(snapshot):
    assert QcFlag.missing_value in evaluate(record(snapshot, 105)).flags
    assert QcFlag.missing_value not in evaluate(record(snapshot, 101)).flags


# @spec PA-QC-003
def test_channel_missing_when_exactly_one_channel_is_null(snapshot):
    assert evaluate(record(snapshot, 104)).flags == [QcFlag.channel_missing]
    both_null = record(snapshot, 101, **{"pm2.5_cf_1_a": None, "pm2.5_cf_1_b": None})
    assert QcFlag.channel_missing not in evaluate(both_null).flags


# @spec PA-QC-004
@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"pm2.5_cf_1_a": -1.0}, True),
        ({"pm2.5_cf_1_b": 1000.5}, True),
        ({"pm2.5_cf_1": 1200.0}, True),
        ({"humidity": 150.0}, True),
        ({"humidity": -0.1}, True),
        ({"pm2.5_cf_1": 1000.0, "pm2.5_cf_1_a": 0.0, "pm2.5_cf_1_b": 0.0}, False),
        ({"humidity": 100.0}, False),
        ({"last_seen": 1789905600 + 3600}, False),  # ahead of the snapshot: not a range check
    ],
)
def test_out_of_range_bounds(snapshot, overrides, expected):
    rec = record(snapshot, 101, **overrides)
    assert (QcFlag.out_of_range in evaluate(rec).flags) is expected


# @spec PA-QC-005
@pytest.mark.parametrize(
    "a, b, expected",
    [
        (6.0, 2400.0, True),  # both conditions
        (1.0, 3.0, False),  # relative 100% but absolute 2
        (500.0, 506.0, False),  # absolute 6 but relative 1.2%
        (10.0, 20.0, True),  # absolute 10, relative 66.7%
        (10.0, 18.0, False),  # absolute 8, relative 57.1%
        (8.0, 8.2, False),
    ],
)
def test_channel_disagreement_requires_both_criteria(snapshot, a, b, expected):
    rec = record(snapshot, 101, **{"pm2.5_cf_1_a": a, "pm2.5_cf_1_b": b, "pm2.5_cf_1": (a + b) / 2})
    assert (QcFlag.channel_disagreement in evaluate(rec).flags) is expected


# @spec PA-QC-006
def test_two_zero_channels_never_disagree_or_divide_by_zero(snapshot):
    result = evaluate(record(snapshot, 110))
    assert QcFlag.channel_disagreement not in result.flags
    assert result.flags == []


# @spec PA-QC-007
def test_purpleair_never_raises_flatline_or_unresolved(snapshot):
    for sensor_index in (101, 102, 103, 104, 105, 107, 108, 109, 110, 111):
        flags = evaluate(record(snapshot, sensor_index)).flags
        assert QcFlag.flatline not in flags
        assert QcFlag.site_id_unresolved not in flags


# --- Corrected value ----------------------------------------------------------


# @spec PA-CORR-001
def test_barkjohn_correction_uses_channel_mean_and_humidity(snapshot):
    assert evaluate(record(snapshot, 101)).pm25_corrected == pytest.approx(5.2534)
    assert evaluate(record(snapshot, 109)).pm25_corrected == pytest.approx(266.736)
    # the sensor-level field is not the input: changing it leaves the correction alone
    rec = record(snapshot, 101, **{"pm2.5_cf_1": 50.0})
    assert evaluate(rec).pm25_corrected == pytest.approx(5.2534)


# @spec PA-CORR-002
def test_negative_correction_is_clamped_to_zero(snapshot):
    result = evaluate(record(snapshot, 110))
    assert result.pm25_corrected == 0.0


# @spec PA-CORR-003
@pytest.mark.parametrize("sensor_index", [102, 103, 104, 107, 111])
def test_correction_is_withheld_when_inputs_missing_or_flagged(snapshot, sensor_index):
    assert evaluate(record(snapshot, sensor_index)).pm25_corrected is None


# @spec PA-CORR-003
def test_missing_value_alone_does_not_withhold_the_correction(snapshot):
    result = evaluate(record(snapshot, 105))
    assert result.flags == [QcFlag.missing_value]
    assert result.pm25_corrected == pytest.approx(4.5958)


# @spec PA-CORR-004
def test_correction_is_a_function_of_the_row_alone(snapshot):
    rec = record(snapshot, 101)
    assert evaluate(rec) == evaluate(rec)
    assert evaluate(rec).pm25_corrected == pytest.approx(0.524 * 8.1 - 0.0862 * 55.0 + 5.75)
    # no clock, reference monitor, or archive is consulted: the row is the only input
    assert list(inspect.signature(evaluate).parameters) == ["record"]
