"""Payload boundary model and QC — AN-MODEL, AN-QC."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from aqdt.airnow.models import AirNowRow
from aqdt.airnow.qc import flags_for_site
from aqdt.observation_store.schemas import QcFlag

from .conftest import row


def parse(**overrides) -> AirNowRow:
    return AirNowRow.model_validate(row(**overrides))


# --- Payload model ------------------------------------------------------------


# @spec AN-MODEL-001
def test_row_fields_and_aliases():
    r = parse()
    assert r.parameter == "PM2.5"
    assert (r.latitude, r.longitude) == (38.9218, -77.0132)
    assert r.value == 7.2
    assert r.raw_concentration == 7.0
    assert r.aqi == 30
    assert r.category == 1
    assert r.unit == "UG/M3"
    assert r.site_name == "McMillan"
    assert r.agency_name == "District of Columbia"
    assert r.full_aqs_code == "110010043"
    assert r.intl_aqs_code == "840110010043"
    assert set(AirNowRow.model_fields) == {
        "utc",
        "parameter",
        "latitude",
        "longitude",
        "value",
        "raw_concentration",
        "aqi",
        "category",
        "unit",
        "site_name",
        "agency_name",
        "full_aqs_code",
        "intl_aqs_code",
    }


# @spec AN-MODEL-002
def test_utc_is_parsed_as_tz_aware_utc():
    assert parse().utc == datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


# @spec AN-MODEL-003
def test_minus_999_sentinels_become_none():
    r = parse(Value=-999, RawConcentration=-999, AQI=-999)
    assert r.value is None and r.raw_concentration is None and r.aqi is None
    assert parse(Value=-999.0).value is None
    assert parse(Value=0.0).value == 0.0


# @spec AN-MODEL-004
def test_aqs_codes_are_strings_with_whitespace_stripped_and_zeros_kept():
    r = parse(FullAQSCode=" 110010044 ", IntlAQSCode="  840110010044")
    assert (r.full_aqs_code, r.intl_aqs_code) == ("110010044", "840110010044")
    numeric = parse(FullAQSCode=110010044, IntlAQSCode=840110010044)
    assert (numeric.full_aqs_code, numeric.intl_aqs_code) == ("110010044", "840110010044")
    padded = parse(FullAQSCode="011001004")
    assert padded.full_aqs_code == "011001004"


# @spec AN-MODEL-005
@pytest.mark.parametrize(
    "overrides",
    [
        {"UTC": None},
        {"UTC": "not a time"},
        {"Latitude": None},
        {"Longitude": None},
        {"Latitude": 95.0},
        {"Parameter": "OZONE"},
        {"Unit": "PPB"},
    ],
)
def test_boundary_rejections(overrides):
    with pytest.raises(ValidationError):
        parse(**overrides)


# @spec AN-MODEL-007
def test_null_value_is_valid_at_the_boundary():
    assert parse(Value=-999).value is None


# --- QC -----------------------------------------------------------------------


def series(values, start_hour=8):
    """A site's rows for consecutive hours; None leaves that hour unreported."""
    rows = []
    for offset, value in enumerate(values):
        if value is None:
            continue
        rows.append(parse(UTC=f"2026-09-20T{start_hour + offset:02d}:00", Value=value))
    return rows


# @spec AN-QC-001
def test_all_applicable_flags_are_raised_together():
    flags = flags_for_site(series([1500.0, 1500.0, 1500.0]), flatline_hours=3)
    assert flags == [[QcFlag.flatline, QcFlag.out_of_range]] * 3


# @spec AN-QC-002
def test_null_value_is_missing_not_out_of_range():
    (flags,) = flags_for_site(series([-999]), flatline_hours=3)
    assert flags == [QcFlag.missing_value]


# @spec AN-QC-003
@pytest.mark.parametrize(
    "value, expected", [(-0.1, True), (1000.1, True), (0.0, False), (1000.0, False)]
)
def test_out_of_range_bounds(value, expected):
    (flags,) = flags_for_site(series([value]), flatline_hours=3)
    assert (QcFlag.out_of_range in flags) is expected


# @spec AN-QC-004
def test_flatline_flags_every_hour_of_a_run_at_least_flatline_hours_long():
    flags = flags_for_site(series([7.0, 0.0, 0.0, 0.0, 0.0, 7.0]), flatline_hours=3)
    assert flags == [
        [],
        [QcFlag.flatline],
        [QcFlag.flatline],
        [QcFlag.flatline],
        [QcFlag.flatline],
        [],
    ]
    exactly_three = flags_for_site(series([3.3, 3.3, 3.3]), flatline_hours=3)
    assert exactly_three == [[QcFlag.flatline]] * 3


# @spec AN-QC-004
def test_flatline_threshold_is_configurable():
    values = [3.3, 3.3, 3.3, 3.3]
    assert all(QcFlag.flatline in f for f in flags_for_site(series(values), flatline_hours=4))
    assert all(QcFlag.flatline not in f for f in flags_for_site(series(values), flatline_hours=5))


# @spec AN-QC-005
def test_runs_are_broken_by_change_null_and_gap():
    # value change: two runs of 2 -> nothing
    assert flags_for_site(series([1.0, 1.0, 2.0, 2.0]), flatline_hours=3) == [[]] * 4
    # a null splits 3.3,3.3 | null | 3.3,3.3 -> nothing but missing_value
    flags = flags_for_site(series([3.3, 3.3, -999, 3.3, 3.3]), flatline_hours=3)
    assert flags == [[], [], [QcFlag.missing_value], [], []]
    # a gap (unreported hour) splits the run
    flags = flags_for_site(series([0.0, 0.0, None, 0.0, 0.0]), flatline_hours=3)
    assert flags == [[]] * 4


# @spec AN-QC-005
def test_rows_out_of_order_are_evaluated_in_hour_order():
    rows = series([0.0, 0.0, 0.0])
    shuffled = [rows[2], rows[0], rows[1]]
    assert flags_for_site(shuffled, flatline_hours=3) == [[QcFlag.flatline]] * 3


# @spec AN-QC-007
def test_airnow_never_raises_channel_flags():
    for flags in flags_for_site(series([-999, 5.0, 2000.0, 1.0, 1.0, 1.0, 1.0]), flatline_hours=3):
        assert QcFlag.channel_missing not in flags
        assert QcFlag.channel_disagreement not in flags


# @spec AN-QC-008
def test_no_future_timestamp_check():
    far_future = parse(UTC="2099-01-01T00:00")
    assert flags_for_site([far_future], flatline_hours=3) == [[]]
