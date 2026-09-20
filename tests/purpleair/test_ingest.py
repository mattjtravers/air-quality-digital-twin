"""Mapping and the ingest_purpleair run — PA-MAP, PA-RUN."""

import copy
from datetime import UTC, datetime

import pandas as pd
import pytest
from pandera.errors import SchemaError, SchemaErrors

from aqdt.observation_store.archive import read_observations, read_sites
from aqdt.observation_store.schemas import QcFlag, Source
from aqdt.purpleair import ingest as ingest_module
from aqdt.purpleair.ingest import ingest_purpleair

from .conftest import T0, Recorder, row_dict

FrameError = (SchemaError, SchemaErrors)


@pytest.fixture
def archive_uri(tmp_path):
    return str(tmp_path / "archive")


@pytest.fixture
def run(settings, recorder, archive_uri):
    summary = ingest_purpleair(settings, archive_uri, transport=recorder.transport)
    return summary, read_sites(archive_uri).set_index("site_id"), read_observations(archive_uri)


# --- Mapping ------------------------------------------------------------------


# @spec PA-MAP-001
def test_site_mapping(run):
    _, sites, _ = run
    site = sites.loc["purpleair:101"]
    assert site["source"] == "purpleair"
    assert site["source_native_id"] == "101"
    assert site["site_type"] == "low_cost_sensor"
    assert site["name"] == "Healthy"
    assert (site["latitude"], site["longitude"]) == (38.9010, -77.0300)
    assert "purpleair:106" not in sites.index  # rejected at the boundary


# @spec PA-MAP-002
def test_observation_mapping(run):
    _, _, observations = run
    obs = observations.set_index("site_id").loc["purpleair:101"]
    assert obs["source"] == "purpleair"
    assert obs["observed_at"] == datetime(2026, 9, 20, 11, 59, tzinfo=UTC)
    assert (obs["latitude"], obs["longitude"]) == (38.9010, -77.0300)
    assert obs["pm25_raw"] == 8.1
    assert (obs["pm25_channel_a"], obs["pm25_channel_b"]) == (8.0, 8.2)
    assert obs["humidity"] == 55.0
    assert obs["pm25_corrected"] == pytest.approx(5.2534)
    assert obs["qc_flags"] == []

    fault = observations.set_index("site_id").loc["purpleair:102"]
    assert fault["qc_flags"] == ["channel_disagreement", "out_of_range"]
    assert fault["pm25_channel_b"] == 2400.0  # raw channel values are kept
    assert pd.isna(fault["pm25_corrected"])


# @spec PA-MAP-003
def test_raw_is_the_zipped_row_unmodified(run, snapshot):
    _, _, observations = run
    by_site = observations.set_index("site_id")
    for sensor_index in (101, 102, 104, 105):
        assert by_site.loc[f"purpleair:{sensor_index}", "raw"] == row_dict(snapshot, sensor_index)


# --- Run ----------------------------------------------------------------------


# @spec PA-RUN-001
def test_entry_point_runs_end_to_end(run):
    summary, sites, observations = run
    assert len(sites) == 10
    assert len(observations) == 10
    assert set(observations["site_id"]) == set(sites.index)


# @spec PA-RUN-001
def test_conn_triggers_load_of_written_partitions(settings, recorder, archive_uri, monkeypatch):
    loaded = []

    def fake_load(conn, uri, uris):
        loaded.append((conn, uri, list(uris)))

    monkeypatch.setattr(ingest_module, "load_partitions", fake_load)
    conn = object()
    summary = ingest_purpleair(settings, archive_uri, conn=conn, transport=recorder.transport)
    assert loaded == [(conn, archive_uri, summary.partitions)]
    assert len(summary.partitions) == 2  # sites + one date partition


# @spec PA-RUN-001
def test_without_conn_nothing_is_loaded(settings, recorder, archive_uri, monkeypatch):
    monkeypatch.setattr(
        ingest_module, "load_partitions", lambda *a, **k: pytest.fail("must not load")
    )
    ingest_purpleair(settings, archive_uri, transport=recorder.transport)


# @spec PA-RUN-002
def test_sites_are_written_before_observations(settings, recorder, archive_uri, monkeypatch):
    order = []
    real_sites, real_obs = ingest_module.write_sites, ingest_module.write_observations

    def sites(records, uri):
        order.append("sites")
        return real_sites(records, uri)

    def observations(records, uri):
        order.append("observations")
        return real_obs(records, uri)

    monkeypatch.setattr(ingest_module, "write_sites", sites)
    monkeypatch.setattr(ingest_module, "write_observations", observations)
    ingest_purpleair(settings, archive_uri, transport=recorder.transport)
    assert order == ["sites", "observations"]


# @spec PA-RUN-003
def test_summary_counts(run, archive_uri):
    summary, _, _ = run
    assert summary.source == Source.purpleair
    assert summary.fetched == 11
    assert summary.rejected == {"latitude": 1}
    assert summary.written == 10
    assert summary.flagged == {
        QcFlag.out_of_range: 3,
        QcFlag.channel_disagreement: 1,
        QcFlag.channel_missing: 1,
        QcFlag.missing_value: 1,
    }
    assert summary.partitions == [
        f"{archive_uri}/source=purpleair/sites.parquet",
        f"{archive_uri}/source=purpleair/date=2026-09-20/observations.parquet",
    ]
    assert summary.snapshot_at == T0
    assert summary.window_start is None and summary.window_end is None
    assert summary.fetched == summary.written + sum(summary.rejected.values())


# @spec PA-RUN-004
def test_empty_snapshot_is_a_successful_no_op(settings, snapshot, archive_uri, tmp_path):
    empty = copy.deepcopy(snapshot)
    empty["data"] = []
    summary = ingest_purpleair(settings, archive_uri, transport=Recorder([empty]).transport)
    assert (summary.fetched, summary.written, summary.partitions) == (0, 0, [])
    assert summary.rejected == {} and summary.flagged == {}
    assert not (tmp_path / "archive").exists() or not any((tmp_path / "archive").rglob("*.parquet"))


# @spec PA-RUN-005
def test_duplicate_sensor_in_snapshot_fails_the_run(settings, snapshot, archive_uri, tmp_path):
    dup = copy.deepcopy(snapshot)
    dup["data"].append(list(dup["data"][0]))
    with pytest.raises(FrameError):
        ingest_purpleair(settings, archive_uri, transport=Recorder([dup]).transport)
    assert not any((tmp_path / "archive").rglob("date=*/observations.parquet"))


# @spec PA-RUN-006
def test_unchanged_sensor_across_runs_produces_no_new_observation(settings, snapshot, archive_uri):
    first_transport = Recorder([copy.deepcopy(snapshot)]).transport
    first = ingest_purpleair(settings, archive_uri, transport=first_transport)
    later = copy.deepcopy(snapshot)
    later["data_time_stamp"] += 120
    second = ingest_purpleair(settings, archive_uri, transport=Recorder([later]).transport)
    observations = read_observations(archive_uri)
    assert len(observations) == 10
    assert first.written == second.written == 10
    assert observations.groupby("site_id").size().max() == 1


# @spec PA-RUN-007
def test_identical_responses_produce_identical_archives(settings, snapshot, tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    ingest_purpleair(settings, a, transport=Recorder([copy.deepcopy(snapshot)]).transport)
    ingest_purpleair(settings, b, transport=Recorder([copy.deepcopy(snapshot)]).transport)
    for rel in (
        "source=purpleair/sites.parquet",
        "source=purpleair/date=2026-09-20/observations.parquet",
    ):
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()
