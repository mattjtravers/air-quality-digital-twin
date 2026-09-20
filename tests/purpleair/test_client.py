"""PurpleAir API contract and settings — PA-API, PA-CFG."""

import copy
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from aqdt.observation_store.schemas import BoundingBox
from aqdt.purpleair.client import fetch_sensors
from aqdt.purpleair.models import PurpleAirSettings

from .conftest import FIXTURES, T0, Recorder

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "aqdt" / "purpleair"


def _package_source() -> str:
    return "\n".join(p.read_text() for p in PACKAGE.rglob("*.py"))


# @spec PA-API-001
def test_request_targets_v1_sensors_with_read_key_header(settings, recorder):
    fetch_sensors(settings, transport=recorder.transport)
    (request,) = recorder.requests
    assert request.method == "GET"
    assert str(request.url).startswith("https://api.purpleair.com/v1/sensors")
    assert request.headers["X-API-Key"] == "test-read-key"


# @spec PA-API-002
def test_bounding_box_is_passed_as_nw_se_corners(settings, recorder):
    fetch_sensors(settings, transport=recorder.transport)
    params = recorder.requests[0].url.params
    assert (params["nwlng"], params["nwlat"], params["selng"], params["selat"]) == (
        "-77.5",
        "39.1",
        "-76.7",
        "38.7",
    )


# @spec PA-API-003
def test_outdoor_only_and_max_age(settings, recorder):
    fetch_sensors(settings, transport=recorder.transport)
    params = recorder.requests[0].url.params
    assert params["location_type"] == "0"
    assert params["max_age"] == "86400"


# @spec PA-API-004
def test_exact_field_list_is_requested(settings, recorder):
    fetch_sensors(settings, transport=recorder.transport)
    fields = recorder.requests[0].url.params["fields"].split(",")
    assert fields == [
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


# @spec PA-API-005
def test_thirty_second_timeout(settings, recorder):
    fetch_sensors(settings, transport=recorder.transport)
    assert recorder.requests[0].extensions["timeout"] == {
        "connect": 30,
        "read": 30,
        "write": 30,
        "pool": 30,
    }


# @spec PA-API-006
@pytest.mark.parametrize("failure", [429, 500, 503, httpx.ConnectError("refused")])
def test_retryable_failures_are_retried_up_to_three_attempts(settings, snapshot, failure):
    recorder = Recorder([failure, copy.deepcopy(failure), copy.deepcopy(snapshot)])
    result = fetch_sensors(settings, transport=recorder.transport)
    assert len(recorder.requests) == 3
    assert len(result.rows) == len(snapshot["data"])


# @spec PA-API-006
def test_exhausted_retries_fail_with_the_last_status(settings):
    recorder = Recorder([500, 502, 503])
    with pytest.raises(Exception, match="503"):
        fetch_sensors(settings, transport=recorder.transport)
    assert len(recorder.requests) == 3

    recorder = Recorder([httpx.ReadTimeout("slow")] * 3)
    with pytest.raises(Exception, match="slow"):
        fetch_sensors(settings, transport=recorder.transport)
    assert len(recorder.requests) == 3


# @spec PA-API-007
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_4xx_fail_immediately(settings, status):
    recorder = Recorder([status, status, status])
    with pytest.raises(Exception, match=str(status)):
        fetch_sensors(settings, transport=recorder.transport)
    assert len(recorder.requests) == 1


# @spec PA-API-008
def test_rows_are_zipped_with_fields_and_snapshot_timestamp_kept(settings, recorder, snapshot):
    result = fetch_sensors(settings, transport=recorder.transport)
    assert result.data_time_stamp == T0
    assert len(result.rows) == len(snapshot["data"])
    first = result.rows[0]
    assert set(first) == set(snapshot["fields"])
    assert first["sensor_index"] == 101
    assert first["pm2.5_cf_1_b"] == 8.2


# @spec PA-API-009
def test_only_the_snapshot_endpoint_is_used(settings, recorder):
    fetch_sensors(settings, transport=recorder.transport)
    assert all("/history" not in str(r.url) for r in recorder.requests)
    assert "/history" not in _package_source()


# --- Settings -----------------------------------------------------------------


# @spec PA-CFG-001
def test_settings_read_key_and_bbox_from_environment(monkeypatch):
    monkeypatch.setenv("PURPLEAIR_API_KEY", "k")
    monkeypatch.setenv("AQDT_BBOX", "-77.5,39.1,-76.7,38.7")
    s = PurpleAirSettings()
    assert s.api_key == "k"
    assert s.bbox == BoundingBox(nwlng=-77.5, nwlat=39.1, selng=-76.7, selat=38.7)
    assert s.base_url == "https://api.purpleair.com/v1/sensors"
    assert s.timeout_seconds == 30
    assert s.max_attempts == 3
    assert s.max_age == 86400
    assert s.location_type == 0
    assert s.fields[0] == "sensor_index"
    assert PurpleAirSettings(max_attempts=5).max_attempts == 5


# @spec PA-CFG-002
@pytest.mark.parametrize("missing", ["PURPLEAIR_API_KEY", "AQDT_BBOX"])
def test_missing_required_variable_fails_naming_it(monkeypatch, missing):
    monkeypatch.setenv("PURPLEAIR_API_KEY", "k")
    monkeypatch.setenv("AQDT_BBOX", "-77.5,39.1,-76.7,38.7")
    monkeypatch.delenv(missing)
    with pytest.raises(ValidationError, match=missing):
        PurpleAirSettings()


# @spec PA-CFG-003
def test_no_configuration_files_are_read():
    source = _package_source()
    assert "env_file" not in source
    assert "dotenv" not in source


# @spec PA-CFG-004
def test_recorded_fixture_includes_a_channel_b_fault(snapshot):
    assert (FIXTURES / "snapshot.json").exists()
    fields = snapshot["fields"]
    a, b = fields.index("pm2.5_cf_1_a"), fields.index("pm2.5_cf_1_b")
    faults = [r for r in snapshot["data"] if None not in (r[a], r[b]) and abs(r[a] - r[b]) > 1000]
    assert faults
