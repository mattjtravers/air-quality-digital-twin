"""Mapping and the ingest_airnow run — AN-MAP, AN-RUN, plus AN-MODEL-006 and AN-QC-006."""

from datetime import datetime, timedelta

import pandas as pd
import pytest

from aqdt.airnow import ingest as ingest_module
from aqdt.airnow.ingest import ingest_airnow
from aqdt.observation_store.archive import read_observations, read_sites
from aqdt.observation_store.schemas import QcFlag, Source

from .conftest import Replay, hour, load, row


def run(settings, archive_uri, rows, start, end, **kwargs):
    replay = Replay(rows)
    summary = ingest_airnow(settings, archive_uri, start, end, transport=replay.transport, **kwargs)
    sites = read_sites(archive_uri).set_index("site_id") if summary.written else None
    observations = read_observations(archive_uri) if summary.written else None
    return summary, sites, observations, replay


def flags_by_hour(observations, site_id):
    frame = observations[observations["site_id"] == site_id].sort_values("observed_at")
    return {
        ts.hour: list(flags)
        for ts, flags in zip(frame["observed_at"], frame["qc_flags"], strict=True)
    }


# --- Mapping ------------------------------------------------------------------


# @spec AN-MAP-001
def test_site_mapping(settings, archive_uri):
    _, sites, _, _ = run(settings, archive_uri, load("clean"), hour(10), hour(14))
    site = sites.loc["airnow:840110010043"]
    assert site["source"] == "airnow"
    assert site["source_native_id"] == "110010043"
    assert site["site_type"] == "reference_monitor"
    assert site["name"] == "McMillan"
    assert (site["latitude"], site["longitude"]) == (38.9218, -77.0132)


# @spec AN-MAP-001
def test_source_native_id_falls_back_to_intl_code_then_name(settings, archive_uri):
    _, sites, _, _ = run(settings, archive_uri, load("malformed_codes"), hour(12), hour(13))
    assert sites.loc["airnow:840110010041", "source_native_id"] == "840110010041"
    assert sites.loc["airnow:unresolved:Nameless codes", "source_native_id"] == "Nameless codes"
    assert sites.loc["airnow:840110010044", "source_native_id"] == "110010044"


# @spec AN-MAP-002
def test_observation_mapping(settings, archive_uri):
    _, _, observations, _ = run(settings, archive_uri, load("clean"), hour(10), hour(14))
    frame = observations[observations["site_id"] == "airnow:840110010043"].set_index("observed_at")
    obs = frame.loc[hour(12)]
    assert obs["source"] == "airnow"
    assert obs.name == hour(12)  # observed_at == UTC, no shift
    assert (obs["latitude"], obs["longitude"]) == (38.9218, -77.0132)
    assert obs["pm25_raw"] == 9.6
    assert pd.isna(obs["pm25_channel_a"]) and pd.isna(obs["pm25_channel_b"])
    assert pd.isna(obs["humidity"]) and pd.isna(obs["pm25_corrected"])
    assert obs["qc_flags"] == []


# @spec AN-MAP-002
def test_missing_value_maps_to_null_pm25_raw(settings, archive_uri):
    _, _, observations, _ = run(settings, archive_uri, load("flatline"), hour(10), hour(11))
    arlington = observations[observations["site_id"] == "airnow:840510130020"].set_index(
        "observed_at"
    )
    assert pd.isna(arlington.loc[hour(10), "pm25_raw"])
    assert arlington.loc[hour(10), "qc_flags"] == ["missing_value"]


# @spec AN-MAP-003
def test_raw_is_the_response_row_unmodified(settings, archive_uri):
    rows = load("flatline")
    _, _, observations, _ = run(settings, archive_uri, rows, hour(10), hour(11))
    arlington = observations[observations["site_id"] == "airnow:840510130020"].set_index(
        "observed_at"
    )
    expected = next(
        r for r in rows if r["SiteName"] == "Arlington" and r["UTC"] == "2026-09-20T10:00"
    )
    assert arlington.loc[hour(10), "raw"] == expected
    assert arlington.loc[hour(10), "raw"]["Value"] == -999  # the sentinel survives in raw


# --- Boundary duplicates and lookback (AN-MODEL-006, AN-QC-006) ---------------


# @spec AN-MODEL-006
def test_duplicate_site_hour_keeps_first_and_counts_the_rest(settings, archive_uri):
    rows = load("clean")
    rows.append(row(UTC="2026-09-20T12:00", Value=99.0))  # same codes
    rows.append(
        row(UTC="2026-09-20T12:00", Value=98.0, IntlAQSCode=None)
    )  # same after normalization
    summary, _, observations, _ = run(settings, archive_uri, rows, hour(10), hour(14))
    assert summary.rejected == {"duplicate_site_hour": 2}
    assert summary.fetched == 14 and summary.written == 12
    mcmillan = observations[observations["site_id"] == "airnow:840110010043"].set_index(
        "observed_at"
    )
    assert mcmillan.loc[hour(12), "pm25_raw"] == 9.6


# @spec AN-MODEL-005
def test_boundary_rejections_are_counted_by_field(settings, archive_uri):
    rows = load("clean")
    rows.append(row(UTC="2026-09-20T11:00", Parameter="OZONE", FullAQSCode="110010099"))
    rows.append(row(UTC="2026-09-20T11:00", Unit="PPB", FullAQSCode="110010098"))
    rows.append(row(UTC="2026-09-20T11:00", Latitude=None, FullAQSCode="110010097"))
    rows.append(row(UTC="bad", FullAQSCode="110010096"))
    summary, _, _, _ = run(settings, archive_uri, rows, hour(10), hour(14))
    assert summary.rejected == {"Parameter": 1, "Unit": 1, "Latitude": 1, "UTC": 1}
    assert summary.written == 12


# @spec AN-QC-006
# @spec AN-RUN-002
def test_lookback_detects_a_flatline_that_starts_before_the_window(settings, archive_uri):
    summary, _, observations, replay = run(
        settings, archive_uri, load("flatline"), hour(13), hour(16)
    )
    params = replay.requests[0].url.params
    assert (params["startDate"], params["endDate"]) == ("2026-09-20T11", "2026-09-20T15")
    mcmillan = flags_by_hour(observations, "airnow:840110010043")
    assert mcmillan == {11: ["flatline"], 12: ["flatline"], 13: ["flatline"], 15: []}
    arlington = flags_by_hour(observations, "airnow:840510130020")
    assert arlington == {11: [], 12: [], 13: ["flatline"], 14: ["flatline"], 15: ["flatline"]}


# @spec AN-RUN-003
def test_lookback_hours_are_written_with_their_new_flags(settings, archive_uri):
    # First run: hours 10-13 alone, the run at McMillan 10-13 is 4 long and flagged.
    run(settings, archive_uri, load("flatline"), hour(12), hour(14))
    before = flags_by_hour(read_observations(archive_uri), "airnow:840110010043")
    assert before == {10: ["flatline"], 11: ["flatline"], 12: ["flatline"], 13: ["flatline"]}
    # Second run over 8-10 re-emits hours 6-9; McMillan 8-9 are 7.0 and stay clean.
    run(settings, archive_uri, load("flatline"), hour(8), hour(10))
    after = flags_by_hour(read_observations(archive_uri), "airnow:840110010043")
    assert after == {
        8: [],
        9: [],
        10: ["flatline"],
        11: ["flatline"],
        12: ["flatline"],
        13: ["flatline"],
    }


# --- Run ----------------------------------------------------------------------


# @spec AN-RUN-001
def test_entry_point_runs_end_to_end(settings, archive_uri):
    summary, sites, observations, _ = run(settings, archive_uri, load("clean"), hour(10), hour(14))
    assert len(sites) == 2
    assert len(observations) == 12
    assert set(observations["site_id"]) == set(sites.index)


# @spec AN-RUN-001
def test_conn_triggers_load_of_written_partitions(settings, archive_uri, monkeypatch):
    loaded = []

    def fake_load(conn, uri, uris):
        loaded.append((conn, uri, list(uris)))

    monkeypatch.setattr(ingest_module, "load_partitions", fake_load)
    conn = object()
    summary, _, _, _ = run(settings, archive_uri, load("clean"), hour(10), hour(14), conn=conn)
    assert loaded == [(conn, archive_uri, summary.partitions)]


# @spec AN-RUN-001
def test_without_conn_nothing_is_loaded(settings, archive_uri, monkeypatch):
    monkeypatch.setattr(
        ingest_module, "load_partitions", lambda *a, **k: pytest.fail("must not load")
    )
    run(settings, archive_uri, load("clean"), hour(10), hour(14))


# @spec AN-RUN-004
def test_sites_are_written_before_observations(settings, archive_uri, monkeypatch):
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
    run(settings, archive_uri, load("clean"), hour(10), hour(14))
    assert order == ["sites", "observations"]


# @spec AN-RUN-005
def test_window_validation(settings, archive_uri):
    with pytest.raises(Exception):
        ingest_airnow(settings, archive_uri, datetime(2026, 9, 20, 10), hour(14))
    with pytest.raises(Exception):
        ingest_airnow(settings, archive_uri, hour(14), hour(10))
    with pytest.raises(Exception):
        ingest_airnow(settings, archive_uri, hour(10), hour(10))
    # sub-hour bounds are truncated to the hour
    replay = Replay(load("clean"))
    ingest_airnow(
        settings,
        archive_uri,
        hour(10) + timedelta(minutes=17),
        hour(14) + timedelta(minutes=45),
        transport=replay.transport,
    )
    params = replay.requests[0].url.params
    assert (params["startDate"], params["endDate"]) == ("2026-09-20T08", "2026-09-20T13")


# @spec AN-RUN-007
def test_summary(settings, archive_uri):
    summary, _, _, _ = run(settings, archive_uri, load("flatline"), hour(13), hour(16))
    assert summary.source == Source.airnow
    assert summary.fetched == 9
    assert summary.rejected == {}
    assert summary.written == 9
    assert summary.flagged == {QcFlag.flatline: 6}
    assert summary.partitions == [
        f"{archive_uri}/source=airnow/sites.parquet",
        f"{archive_uri}/source=airnow/date=2026-09-20/observations.parquet",
    ]
    assert summary.window_start == hour(13) and summary.window_end == hour(16)
    assert summary.snapshot_at is None
    assert summary.fetched == summary.written + sum(summary.rejected.values())


# @spec AN-RUN-007
def test_summary_counts_unresolved_sites(settings, archive_uri):
    summary, sites, observations, _ = run(
        settings, archive_uri, load("malformed_codes"), hour(12), hour(13)
    )
    assert summary.fetched == summary.written == 8
    assert summary.flagged == {QcFlag.site_id_unresolved: 3}
    unresolved = observations[observations["site_id"].str.startswith("airnow:unresolved:")]
    assert all(flags == ["site_id_unresolved"] for flags in unresolved["qc_flags"])
    assert len(sites) == 8


# @spec AN-RUN-008
def test_empty_response_is_a_successful_no_op(settings, archive_uri, tmp_path):
    summary, _, _, _ = run(settings, archive_uri, [], hour(10), hour(14))
    assert (summary.fetched, summary.written, summary.partitions) == (0, 0, [])
    assert summary.window_start == hour(10)
    assert not (tmp_path / "archive").exists() or not any((tmp_path / "archive").rglob("*.parquet"))


# @spec AN-RUN-009
def test_identical_responses_produce_identical_archives(settings, tmp_path):
    a, b = str(tmp_path / "a"), str(tmp_path / "b")
    run(settings, a, load("flatline"), hour(10), hour(16))
    run(settings, b, load("flatline"), hour(10), hour(16))
    for rel in (
        "source=airnow/sites.parquet",
        "source=airnow/date=2026-09-20/observations.parquet",
    ):
        assert (tmp_path / "a" / rel).read_bytes() == (tmp_path / "b" / rel).read_bytes()
