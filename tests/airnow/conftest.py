"""AirNow test scaffolding: recorded responses, a window-aware mock transport, settings."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from aqdt.airnow.models import AirNowSettings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "airnow"
DAY0 = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
H = timedelta(hours=1)


def hour(n: int) -> datetime:
    return DAY0 + n * H


def load(name: str) -> list[dict]:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def row(**overrides) -> dict:
    """One AirNow response row in the shape the API returns with verbose=1."""
    base = {
        "Latitude": 38.9218,
        "Longitude": -77.0132,
        "UTC": "2026-09-20T12:00",
        "Parameter": "PM2.5",
        "Unit": "UG/M3",
        "Value": 7.2,
        "RawConcentration": 7.0,
        "AQI": 30,
        "Category": 1,
        "SiteName": "McMillan",
        "AgencyName": "District of Columbia",
        "FullAQSCode": "110010043",
        "IntlAQSCode": "840110010043",
    }
    base.update(overrides)
    return base


class Replay:
    """Serves fixture rows filtered to each request's inclusive [startDate, endDate] hours.

    A scripted list of failures (status codes or exceptions) is consumed first, one per request.
    """

    def __init__(self, rows: list[dict], failures=()):
        self.rows = rows
        self.failures = list(failures)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.failures:
            failure = self.failures.pop(0)
            if isinstance(failure, Exception):
                raise failure
            return httpx.Response(failure, text="scripted failure")
        params = request.url.params
        start, end = params["startDate"], params["endDate"]
        served = []
        for r in self.rows:
            stamp = r.get("UTC")
            well_formed = isinstance(stamp, str) and len(stamp) == 16
            if not well_formed or start <= stamp[:13] <= end:
                served.append(r)
        return httpx.Response(200, json=served)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def settings(monkeypatch) -> AirNowSettings:
    monkeypatch.setenv("AIRNOW_API_KEY", "test-airnow-key")
    monkeypatch.setenv("AQDT_BBOX", "-77.5,39.1,-76.7,38.7")
    return AirNowSettings(retry_backoff_seconds=0)


@pytest.fixture
def archive_uri(tmp_path):
    return str(tmp_path / "archive")
