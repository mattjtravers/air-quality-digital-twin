"""AirNow API contract and settings — AN-API, AN-CFG."""

from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from aqdt.airnow.client import fetch_rows
from aqdt.airnow.models import AirNowSettings
from aqdt.observation_store.schemas import DC_METRO, BoundingBox

from .conftest import FIXTURES, Replay, hour, load

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "aqdt" / "airnow"


def _package_source() -> str:
    return "\n".join(p.read_text() for p in PACKAGE.rglob("*.py"))


@pytest.fixture
def replay():
    return Replay(load("clean"))


# @spec AN-API-001
def test_request_targets_aq_data_with_api_key_parameter(settings, replay):
    fetch_rows(settings, hour(10), hour(14), transport=replay.transport)
    (request,) = replay.requests
    assert request.method == "GET"
    assert str(request.url).startswith("https://www.airnowapi.org/aq/data/")
    assert request.url.params["API_KEY"] == "test-airnow-key"
    assert "API_KEY" not in request.headers


# @spec AN-API-002
def test_bounding_box_is_min_lon_lat_max_lon_lat(settings, replay):
    fetch_rows(settings, hour(10), hour(14), transport=replay.transport)
    assert replay.requests[0].url.params["BBOX"] == "-77.5,38.7,-76.7,39.1"


# @spec AN-API-003
def test_half_open_window_renders_as_inclusive_hours(settings, replay):
    fetch_rows(settings, hour(10), hour(14), transport=replay.transport)
    params = replay.requests[0].url.params
    assert params["startDate"] == "2026-09-20T10"
    assert params["endDate"] == "2026-09-20T13"


# @spec AN-API-004
def test_fixed_query_parameters(settings, replay):
    fetch_rows(settings, hour(10), hour(14), transport=replay.transport)
    params = replay.requests[0].url.params
    assert params["parameters"] == "PM25"
    assert params["dataType"] == "B"
    assert params["monitorType"] == "0"
    assert params["verbose"] == "1"
    assert params["includerawconcentrations"] == "1"
    assert params["format"] == "application/json"


# @spec AN-API-005
def test_thirty_second_timeout(settings, replay):
    fetch_rows(settings, hour(10), hour(14), transport=replay.transport)
    assert replay.requests[0].extensions["timeout"] == {
        "connect": 30,
        "read": 30,
        "write": 30,
        "pool": 30,
    }


# @spec AN-API-006
@pytest.mark.parametrize("failure", [429, 500, 503, httpx.ConnectError("refused")])
def test_retryable_failures_are_retried_up_to_three_attempts(settings, failure):
    replay = Replay(load("clean"), failures=[failure, failure])
    rows = fetch_rows(settings, hour(10), hour(14), transport=replay.transport)
    assert len(replay.requests) == 3
    assert len(rows) == 8  # 4 hours x 2 sites


# @spec AN-API-006
def test_exhausted_retries_fail_with_the_last_status(settings):
    replay = Replay(load("clean"), failures=[500, 502, 503])
    with pytest.raises(Exception, match="503"):
        fetch_rows(settings, hour(10), hour(14), transport=replay.transport)
    assert len(replay.requests) == 3


# @spec AN-API-007
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_other_4xx_fail_immediately(settings, status):
    replay = Replay(load("clean"), failures=[status, status, status])
    with pytest.raises(Exception, match=str(status)):
        fetch_rows(settings, hour(10), hour(14), transport=replay.transport)
    assert len(replay.requests) == 1


# @spec AN-API-008
def test_windows_over_24_hours_are_chunked_without_gap_or_overlap(settings, replay):
    fetch_rows(settings, hour(-2), hour(48), transport=replay.transport)
    windows = [(r.url.params["startDate"], r.url.params["endDate"]) for r in replay.requests]
    assert windows == [
        ("2026-09-19T22", "2026-09-20T21"),
        ("2026-09-20T22", "2026-09-21T21"),
        ("2026-09-21T22", "2026-09-21T23"),
    ]


# @spec AN-API-008
def test_chunked_rows_are_concatenated_in_window_order(settings):
    rows = load("clean")  # hours 08..15 on 2026-09-20
    replay = Replay(rows)
    fetched = fetch_rows(settings, hour(-20), hour(16), transport=replay.transport)
    assert len(replay.requests) == 2
    assert len(fetched) == len(rows)
    assert [r["UTC"] for r in fetched] == sorted(r["UTC"] for r in fetched)


# @spec AN-API-008
def test_exactly_24_hours_is_a_single_request(settings, replay):
    fetch_rows(settings, hour(0), hour(24), transport=replay.transport)
    assert len(replay.requests) == 1


# @spec AN-API-009
def test_rows_are_returned_as_received(settings, replay):
    rows = fetch_rows(settings, hour(10), hour(11), transport=replay.transport)
    assert rows == [r for r in load("clean") if r["UTC"] == "2026-09-20T10:00"]


# --- Settings -----------------------------------------------------------------


# @spec AN-CFG-001
def test_settings_from_environment_with_overridable_defaults(monkeypatch):
    monkeypatch.setenv("AIRNOW_API_KEY", "k")
    monkeypatch.delenv("AQDT_BBOX", raising=False)
    s = AirNowSettings()
    assert s.api_key == "k"
    assert s.bbox == DC_METRO
    monkeypatch.setenv("AQDT_BBOX", "-78.0,39.5,-76.5,38.5")
    assert AirNowSettings().bbox == BoundingBox(nwlng=-78.0, nwlat=39.5, selng=-76.5, selat=38.5)
    assert s.flatline_hours == 3
    assert s.base_url == "https://www.airnowapi.org/aq/data/"
    assert s.timeout_seconds == 30
    assert s.max_attempts == 3
    assert s.chunk_hours == 24
    assert AirNowSettings(flatline_hours=6).flatline_hours == 6


# @spec AN-CFG-002
def test_missing_api_key_fails_naming_it(monkeypatch):
    monkeypatch.delenv("AIRNOW_API_KEY", raising=False)
    with pytest.raises(ValidationError, match="AIRNOW_API_KEY"):
        AirNowSettings()


# @spec AN-CFG-003
def test_no_configuration_files_are_read():
    source = _package_source()
    assert "env_file" not in source
    assert "dotenv" not in source


# @spec AN-CFG-004
def test_recorded_fixtures_cover_the_required_shapes():
    assert (FIXTURES / "clean.json").exists()
    assert (FIXTURES / "flatline.json").exists()
    assert (FIXTURES / "malformed_codes.json").exists()
    codes = {r["FullAQSCode"] for r in load("malformed_codes")}
    assert any(c is not None and len(c.strip()) == 8 for c in codes)
    assert any(c is not None and not c.strip().isdigit() for c in codes)
