"""Pipeline test scaffolding: every run function replaced by a recording stub, a fake
connection, and a fixed clock. No network, no database."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import pytest

from aqdt.observation_store.schemas import IngestSummary, QcFlag, Source
from aqdt.pipeline import cli

NOW = datetime(2026, 9, 21, 14, 37, 12, tzinfo=UTC)
NOW_H = datetime(2026, 9, 21, 14, 0, tzinfo=UTC)
MIDNIGHT = datetime(2026, 9, 21, 0, 0, tzinfo=UTC)
ARCHIVE = "/tmp/aqdt-test-archive"


@dataclass
class Call:
    name: str
    args: tuple
    kwargs: dict


@dataclass
class Stubs:
    """Records every run invocation; each stub returns ``results[name]`` or raises
    ``errors[name]``."""

    calls: list[Call] = field(default_factory=list)
    results: dict[str, Any] = field(default_factory=dict)
    errors: dict[str, Exception] = field(default_factory=dict)

    def make(self, name):
        def stub(*args, **kwargs):
            self.calls.append(Call(name, args, kwargs))
            if name in self.errors:
                raise self.errors[name]
            return self.results.get(name)

        return stub

    def only(self, name) -> Call:
        matching = [c for c in self.calls if c.name == name]
        assert len(matching) == 1, [c.name for c in self.calls]
        return matching[0]


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def summary() -> IngestSummary:
    return IngestSummary(
        source=Source.purpleair,
        fetched=3,
        rejected={"latitude": 1},
        written=2,
        flagged={QcFlag.out_of_range: 1},
        partitions=[f"{ARCHIVE}/source=purpleair/sites.parquet"],
        snapshot_at=NOW_H,
        window_start=None,
        window_end=None,
    )


@pytest.fixture
def stubs(monkeypatch) -> Stubs:
    s = Stubs()
    s.results["ingest_purpleair"] = summary()
    s.results["ingest_airnow"] = summary().model_copy(
        update={
            "source": Source.airnow,
            "snapshot_at": None,
            "window_start": NOW_H,
            "window_end": NOW_H,
        }
    )
    s.results["fit_calibrations"] = []
    s.results["apply_calibrations"] = pd.DataFrame({"site_id": [], "hour": []})
    for name in (
        "ingest_purpleair",
        "ingest_airnow",
        "fit_calibrations",
        "apply_calibrations",
        "apply_schema",
        "rebuild",
    ):
        monkeypatch.setattr(cli, name, s.make(name))
    s.connection = FakeConnection()
    monkeypatch.setattr(cli, "connect", s.make("connect"))
    s.results["connect"] = s.connection
    return s


@pytest.fixture
def env(monkeypatch):
    """A complete environment for the ingesters and the archive, without PostGIS."""
    monkeypatch.setenv("PURPLEAIR_API_KEY", "pa-key")
    monkeypatch.setenv("AIRNOW_API_KEY", "an-key")
    monkeypatch.setenv("AQDT_ARCHIVE_URI", ARCHIVE)
    monkeypatch.delenv("AQDT_BBOX", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def run(argv: list[str], now: datetime = NOW) -> int:
    return cli.main(argv, now=now)
