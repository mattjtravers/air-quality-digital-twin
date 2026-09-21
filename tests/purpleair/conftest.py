"""PurpleAir test scaffolding: recorded snapshot, mock HTTP transport, settings."""

import copy
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from aqdt.purpleair.models import PurpleAirSettings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "purpleair"
T0 = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)  # the recorded snapshot's data_time_stamp


@pytest.fixture
def snapshot() -> dict:
    return json.loads((FIXTURES / "snapshot.json").read_text())


def row_dict(snapshot: dict, sensor_index: int) -> dict:
    """The zipped row for one sensor, as the client would produce it."""
    for row in snapshot["data"]:
        if row[0] == sensor_index:
            return dict(zip(snapshot["fields"], row, strict=True))
    raise KeyError(sensor_index)


class Recorder:
    """A mock transport that records requests and replays a scripted list of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, int):
            return httpx.Response(nxt, json={"error": "scripted"})
        return httpx.Response(200, json=nxt)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


@pytest.fixture
def recorder(snapshot):
    return Recorder([copy.deepcopy(snapshot)])


@pytest.fixture
def settings(monkeypatch) -> PurpleAirSettings:
    monkeypatch.setenv("PURPLEAIR_API_KEY", "test-read-key")
    monkeypatch.delenv("AQDT_BBOX", raising=False)
    return PurpleAirSettings(retry_backoff_seconds=0)
