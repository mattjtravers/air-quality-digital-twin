"""GeoParquet archive — OBS-ARCHIVE."""

import json
import logging
import os
import stat
from datetime import UTC, date, datetime, timedelta

import geopandas as gpd
import pandas as pd
import pyarrow.parquet as pq
import pytest
from pandera.errors import SchemaError, SchemaErrors

from aqdt.observation_store import archive
from aqdt.observation_store.archive import (
    read_observations,
    read_partitioned,
    read_sites,
    write_observations,
    write_partitioned,
    write_sites,
)
from aqdt.observation_store.frames import ObservationsFrame, SitesFrame
from aqdt.observation_store.products import OBSERVATIONS, SITES, render_partition_value
from aqdt.observation_store.schemas import QcFlag, Source

from .conftest import T0, TOY, make_observation, make_site, toy_frame

FrameError = (SchemaError, SchemaErrors)
DAY = timedelta(days=1)


def _obs_files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.parquet"))


@pytest.fixture
def archive_dir(tmp_path):
    return tmp_path / "archive"


# --- Layout and file format ---------------------------------------------------


# @spec OBS-ARCHIVE-001
def test_layout_is_hive_style_by_source_and_date(archive_dir):
    write_sites([make_site()], str(archive_dir))
    write_observations(
        [
            make_observation(observed_at=T0),
            make_observation(observed_at=T0 + DAY),
        ],
        str(archive_dir),
    )
    assert _obs_files(archive_dir) == [
        "source=purpleair/date=2026-09-20/observations.parquet",
        "source=purpleair/date=2026-09-21/observations.parquet",
        "source=purpleair/sites.parquet",
    ]


# @spec OBS-ARCHIVE-002
def test_bare_path_and_file_uri_address_the_same_archive(archive_dir):
    write_observations([make_observation()], str(archive_dir))
    frame = read_observations(f"file://{archive_dir}")
    assert len(frame) == 1
    write_observations([make_observation(site_id="purpleair:2")], f"file://{archive_dir}")
    assert len(read_observations(str(archive_dir))) == 2


# @spec OBS-ARCHIVE-002
def test_s3_uri_is_read_and_written_through_the_same_code_path(s3_archive_uri):
    written = write_observations([make_observation()], s3_archive_uri)
    assert written == [f"{s3_archive_uri}/source=purpleair/date=2026-09-20/observations.parquet"]
    write_sites([make_site()], s3_archive_uri)
    assert len(read_observations(s3_archive_uri)) == 1
    assert len(read_sites(s3_archive_uri)) == 1


# @spec OBS-ARCHIVE-003
def test_observation_and_site_files_are_geoparquet_with_plain_columns(archive_dir):
    write_observations([make_observation(qc_flags=[QcFlag.out_of_range])], str(archive_dir))
    write_sites([make_site()], str(archive_dir))
    obs_path = archive_dir / "source=purpleair/date=2026-09-20/observations.parquet"
    schema = pq.read_schema(obs_path)
    assert b"geo" in schema.metadata
    assert str(schema.field("qc_flags").type).startswith("list<")
    assert str(schema.field("raw").type) in ("string", "large_string")
    for name in ("site_id", "observed_at", "pm25_raw", "humidity"):
        assert name in schema.names
    assert b"geo" in pq.read_schema(archive_dir / "source=purpleair/sites.parquet").metadata


# @spec OBS-ARCHIVE-004
def test_raw_json_is_canonical(archive_dir, tmp_path):
    a = make_observation(raw={"b": 1, "a": 2.5, "c": {"y": 1, "x": 2}})
    b = make_observation(raw={"c": {"x": 2, "y": 1}, "a": 2.5, "b": 1})
    first, second = tmp_path / "one", tmp_path / "two"
    write_observations([a], str(first))
    write_observations([b], str(second))
    path = "source=purpleair/date=2026-09-20/observations.parquet"
    assert (first / path).read_bytes() == (second / path).read_bytes()
    raw = pq.read_table(first / path).column("raw")[0].as_py()
    assert raw == '{"a": 2.5, "b": 1, "c": {"x": 2, "y": 1}}'
    assert json.loads(raw) == a.raw


# @spec OBS-ARCHIVE-020
def test_files_open_in_geopandas_without_project_code(archive_dir):
    records = [make_observation(site_id="purpleair:1"), make_observation(site_id="purpleair:2")]
    write_observations(records, str(archive_dir))
    frame = gpd.read_parquet(archive_dir / "source=purpleair/date=2026-09-20/observations.parquet")
    assert frame.crs.to_epsg() == 4326
    assert list(frame["site_id"]) == ["purpleair:1", "purpleair:2"]
    assert frame.geometry.iloc[0].x == records[0].longitude


# --- Write semantics ----------------------------------------------------------


# @spec OBS-ARCHIVE-005
# @spec OBS-ARCHIVE-006
def test_duplicate_keys_within_a_batch_fail_before_any_write(archive_dir):
    with pytest.raises(FrameError):
        write_observations([make_observation(), make_observation()], str(archive_dir))
    assert not archive_dir.exists() or _obs_files(archive_dir) == []


# @spec OBS-ARCHIVE-007
def test_rows_are_grouped_by_source_and_utc_date(archive_dir):
    write_observations(
        [
            make_observation(observed_at=datetime(2026, 9, 20, 23, 59, tzinfo=UTC)),
            make_observation(observed_at=datetime(2026, 9, 21, 0, 0, tzinfo=UTC)),
            make_observation(
                site_id="airnow:840110010043",
                source=Source.airnow,
                observed_at=T0,
                pm25_channel_a=None,
                pm25_channel_b=None,
                humidity=None,
                pm25_corrected=None,
            ),
        ],
        str(archive_dir),
    )
    assert _obs_files(archive_dir) == [
        "source=airnow/date=2026-09-20/observations.parquet",
        "source=purpleair/date=2026-09-20/observations.parquet",
        "source=purpleair/date=2026-09-21/observations.parquet",
    ]


# @spec OBS-ARCHIVE-008
def test_rewrite_merges_with_incoming_row_winning(archive_dir):
    write_observations([make_observation(site_id="purpleair:1", pm25_raw=1.0)], str(archive_dir))
    write_observations(
        [
            make_observation(site_id="purpleair:1", pm25_raw=2.0, qc_flags=[QcFlag.flatline]),
            make_observation(site_id="purpleair:0"),
        ],
        str(archive_dir),
    )
    frame = read_observations(str(archive_dir))
    assert list(frame["site_id"]) == ["purpleair:0", "purpleair:1"]
    row = frame.set_index("site_id").loc["purpleair:1"]
    assert row["pm25_raw"] == 2.0
    assert row["qc_flags"] == ["flatline"]


# @spec OBS-ARCHIVE-009
def test_failed_write_leaves_previous_partition_intact(archive_dir):
    if os.geteuid() == 0:
        pytest.skip("permission-based failure injection needs a non-root user")
    write_observations([make_observation(pm25_raw=1.0)], str(archive_dir))
    partition = archive_dir / "source=purpleair/date=2026-09-20"
    before = (partition / "observations.parquet").read_bytes()
    partition.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        with pytest.raises(OSError):
            write_observations([make_observation(pm25_raw=2.0)], str(archive_dir))
    finally:
        partition.chmod(stat.S_IRWXU)
    assert (partition / "observations.parquet").read_bytes() == before
    # only the partition and its lock file (OBS-ARCHIVE-025); no temporary file survives
    assert {p.name for p in partition.iterdir()} == {
        "observations.parquet",
        ".observations.parquet.lock",
    }


# @spec OBS-ARCHIVE-009
def test_no_temporary_files_remain_after_a_write(archive_dir):
    records = [make_observation(), make_observation(observed_at=T0 + DAY)]
    write_observations(records, str(archive_dir))
    write_sites([make_site()], str(archive_dir))
    leftovers = [
        p
        for p in archive_dir.rglob("*")
        if p.is_file() and p.suffix not in (".parquet", ".lock")  # lock files: OBS-ARCHIVE-025
    ]
    assert leftovers == []


# @spec OBS-ARCHIVE-010
def test_writes_return_the_partition_uris_written(archive_dir):
    uris = write_observations(
        [make_observation(), make_observation(observed_at=T0 + DAY)], str(archive_dir)
    )
    assert uris == [
        f"{archive_dir}/source=purpleair/date=2026-09-20/observations.parquet",
        f"{archive_dir}/source=purpleair/date=2026-09-21/observations.parquet",
    ]
    assert write_sites([make_site()], str(archive_dir)) == [
        f"{archive_dir}/source=purpleair/sites.parquet"
    ]


# @spec OBS-ARCHIVE-011
def test_empty_record_set_writes_nothing(archive_dir):
    assert write_observations([], str(archive_dir)) == []
    assert write_sites([], str(archive_dir)) == []
    assert not archive_dir.exists() or _obs_files(archive_dir) == []


# @spec OBS-ARCHIVE-012
def test_identical_input_produces_identical_bytes(tmp_path):
    records = [
        make_observation(site_id="purpleair:2", qc_flags=[QcFlag.out_of_range]),
        make_observation(site_id="purpleair:1", observed_at=T0 + timedelta(minutes=1)),
    ]
    write_observations(records, str(tmp_path / "a"))
    write_observations(list(reversed(records)), str(tmp_path / "b"))
    path = "source=purpleair/date=2026-09-20/observations.parquet"
    assert (tmp_path / "a" / path).read_bytes() == (tmp_path / "b" / path).read_bytes()


# @spec OBS-ARCHIVE-013
def test_write_sites_merges_on_site_id_per_source(archive_dir):
    write_sites([make_site(site_id="purpleair:1", name="old")], str(archive_dir))
    write_sites(
        [make_site(site_id="purpleair:1", name="new"), make_site(site_id="purpleair:0")],
        str(archive_dir),
    )
    frame = read_sites(str(archive_dir))
    assert list(frame["site_id"]) == ["purpleair:0", "purpleair:1"]
    assert frame.set_index("site_id").loc["purpleair:1", "name"] == "new"
    with pytest.raises(FrameError):
        write_sites([make_site(), make_site()], str(archive_dir))


# --- Partition primitives -----------------------------------------------------


# @spec OBS-ARCHIVE-014
def test_write_partitioned_lays_out_by_rendered_keys_and_merges(archive_dir):
    frame = toy_frame([("s1", T0, 1.0), ("s1", T0 + DAY, 2.0)])
    uris = write_partitioned(frame, str(archive_dir), TOY)
    assert uris == [
        f"{archive_dir}/toy/values/date=2026-09-20/values.parquet",
        f"{archive_dir}/toy/values/date=2026-09-21/values.parquet",
    ]
    write_partitioned(toy_frame([("s1", T0, 9.0), ("s2", T0, 3.0)]), str(archive_dir), TOY)
    frame = read_partitioned(str(archive_dir), TOY, date="2026-09-20")
    assert list(frame["site_id"]) == ["s1", "s2"]
    assert list(frame["value"]) == [9.0, 3.0]
    with pytest.raises(FrameError):
        write_partitioned(toy_frame([("s1", T0, 1.0), ("s1", T0, 1.0)]), str(archive_dir), TOY)


# @spec OBS-ARCHIVE-003
def test_product_without_geometry_is_plain_parquet(archive_dir):
    write_partitioned(toy_frame([("s1", T0, 1.0)]), str(archive_dir), TOY)
    schema = pq.read_schema(archive_dir / "toy/values/date=2026-09-20/values.parquet")
    assert schema.metadata is None or b"geo" not in schema.metadata
    plain = pd.read_parquet(archive_dir / "toy/values/date=2026-09-20/values.parquet")
    assert plain.shape == (1, 3)


# @spec OBS-ARCHIVE-015
def test_read_partitioned_accepts_literal_and_predicate_filters(archive_dir):
    write_partitioned(
        toy_frame([("s1", T0, 1.0), ("s1", T0 + DAY, 2.0), ("s1", T0 + 2 * DAY, 3.0)]),
        str(archive_dir),
        TOY,
    )
    assert list(read_partitioned(str(archive_dir), TOY, date="2026-09-21")["value"]) == [2.0]
    later = read_partitioned(str(archive_dir), TOY, date=lambda d: d >= "2026-09-21")
    assert list(later["value"]) == [2.0, 3.0]
    assert list(read_partitioned(str(archive_dir), TOY)["value"]) == [1.0, 2.0, 3.0]


# @spec OBS-ARCHIVE-016
def test_observation_and_site_functions_delegate_to_the_primitives(archive_dir, monkeypatch):
    calls = []
    real_write, real_read = archive.write_partitioned, archive.read_partitioned

    def spy_write(frame, uri, product):
        calls.append(("write", product))
        return real_write(frame, uri, product)

    def spy_read(uri, product, **filters):
        calls.append(("read", product))
        return real_read(uri, product, **filters)

    monkeypatch.setattr(archive, "write_partitioned", spy_write)
    monkeypatch.setattr(archive, "read_partitioned", spy_read)
    write_observations([make_observation()], str(archive_dir))
    write_sites([make_site()], str(archive_dir))
    read_observations(str(archive_dir))
    read_sites(str(archive_dir))
    assert calls == [
        ("write", OBSERVATIONS),
        ("write", SITES),
        ("read", OBSERVATIONS),
        ("read", SITES),
    ]


# @spec OBS-ARCHIVE-022
def test_partition_values_render_deterministically():
    assert render_partition_value(date(2026, 9, 20)) == "2026-09-20"
    assert render_partition_value(datetime(2026, 9, 20, 7, 0, tzinfo=UTC)) == "2026-09-20T07"
    assert render_partition_value(pd.Timestamp("2026-09-20T07:00", tz="UTC")) == "2026-09-20T07"
    assert render_partition_value("purpleair") == "purpleair"
    assert render_partition_value(Source.purpleair) == "purpleair"


# --- Read semantics -----------------------------------------------------------


@pytest.fixture
def populated(archive_dir):
    write_observations(
        [
            make_observation(site_id="purpleair:1", observed_at=T0 - DAY),
            make_observation(site_id="purpleair:1", observed_at=T0),
            make_observation(site_id="purpleair:1", observed_at=T0 + timedelta(hours=6)),
            make_observation(site_id="purpleair:1", observed_at=T0 + DAY),
            make_observation(
                site_id="airnow:840110010043",
                source=Source.airnow,
                observed_at=T0,
                pm25_channel_a=None,
                pm25_channel_b=None,
                humidity=None,
                pm25_corrected=None,
            ),
        ],
        str(archive_dir),
    )
    write_sites(
        [
            make_site(site_id="purpleair:1"),
            make_site(
                site_id="airnow:840110010043",
                source=Source.airnow,
                source_native_id="110010043",
                site_type="reference_monitor",
            ),
        ],
        str(archive_dir),
    )
    return str(archive_dir)


# @spec OBS-ARCHIVE-017
def test_read_observations_filters_by_source_and_half_open_window(populated):
    everything = read_observations(populated)
    assert len(everything) == 5
    assert ObservationsFrame.validate(everything) is not None

    purpleair = read_observations(populated, source=Source.purpleair)
    assert set(purpleair["source"]) == {"purpleair"}
    assert len(purpleair) == 4

    window = read_observations(populated, start=T0, end=T0 + DAY)
    assert sorted(window["observed_at"]) == [T0, T0, T0 + timedelta(hours=6)]

    just_start = read_observations(populated, source=Source.purpleair, start=T0 + DAY)
    assert list(just_start["observed_at"]) == [T0 + DAY]


# @spec OBS-ARCHIVE-018
def test_read_sites_filters_by_source(populated):
    assert len(read_sites(populated)) == 2
    airnow = read_sites(populated, source=Source.airnow)
    assert list(airnow["site_id"]) == ["airnow:840110010043"]
    assert SitesFrame.validate(airnow) is not None


# @spec OBS-ARCHIVE-019
def test_empty_read_returns_typed_empty_frame_and_logs(populated, caplog):
    with caplog.at_level(logging.INFO, logger="aqdt"):
        frame = read_observations(populated, start=T0 + 10 * DAY, end=T0 + 11 * DAY)
    assert len(frame) == 0
    assert set(frame.columns) == set(ObservationsFrame.to_schema().columns)
    assert any("no partition" in r.getMessage().lower() for r in caplog.records)
    assert len(read_sites(populated + "/does-not-exist")) == 0


# --- One writer per partition (OBS-ARCHIVE-023..026) ---------------------------


def _race(monkeypatch, competing, archive_uri, times=1):
    """Let a competing writer land between the first writer's read and its write.

    Wraps ``archive._read_current``: each of the outer writer's first ``times`` reads is followed
    by a competing ``write_partitioned`` of ``competing(k)`` (``k`` = 0, 1, …) to the same
    partition. Returns the list of outer reads, for counting retries.
    """
    real = archive._read_current
    reads: list = []
    state = {"inner": False}

    def hooked(fs, path, product):
        result = real(fs, path, product)
        if state["inner"]:
            return result
        reads.append(path)
        if len(reads) <= times:
            state["inner"] = True
            try:
                write_partitioned(toy_frame(competing(len(reads) - 1)), archive_uri, TOY)
            finally:
                state["inner"] = False
        return result

    monkeypatch.setattr(archive, "_read_current", hooked)
    return reads


OURS = [("a", "2026-09-20T10:00", 1.0), ("a", "2026-09-20T11:00", 2.0)]
THEIRS = [("b", "2026-09-20T10:00", 9.0)]


# @spec OBS-ARCHIVE-023
# @spec OBS-ARCHIVE-024
# @spec OBS-ARCHIVE-026
@pytest.mark.parametrize("pre_existing", [True, False])
def test_s3_write_refused_when_partition_changed_is_re_merged(
    s3_archive_uri, monkeypatch, pre_existing
):
    if pre_existing:
        write_partitioned(toy_frame([("c", "2026-09-20T09:00", 0.0)]), s3_archive_uri, TOY)
    reads = _race(monkeypatch, lambda k: THEIRS, s3_archive_uri)
    write_partitioned(toy_frame(OURS), s3_archive_uri, TOY)
    stored = read_partitioned(s3_archive_uri, TOY)
    expected = {"a", "b"} | ({"c"} if pre_existing else set())
    assert set(stored["site_id"]) == expected
    assert len(stored) == len(OURS) + len(THEIRS) + (1 if pre_existing else 0)
    assert len(reads) == 2  # the initial read and exactly one re-read after the refusal


# @spec OBS-ARCHIVE-024
def test_s3_write_gives_up_after_five_refusals_naming_the_partition(s3_archive_uri, monkeypatch):
    # every competing write adds a new row, so the object changes before each of our attempts
    reads = _race(
        monkeypatch, lambda k: [(f"x{k}", "2026-09-20T10:00", float(k))], s3_archive_uri, times=10
    )
    with pytest.raises(archive.ConcurrentWriteError, match="toy/values/date=2026-09-20"):
        write_partitioned(toy_frame(OURS), s3_archive_uri, TOY)
    assert len(reads) == 5
    stored = read_partitioned(s3_archive_uri, TOY)
    assert "a" not in set(stored["site_id"])  # ours never landed; the competitors' rows did
    assert len(stored) == 5


# @spec OBS-ARCHIVE-025
# @spec OBS-ARCHIVE-026
def test_local_concurrent_writers_are_serialized_by_the_lock(archive_dir, monkeypatch):
    import threading
    import time

    archive_dir = str(archive_dir)

    real = archive._read_current
    started = threading.Barrier(2)

    def slow_read(fs, path, product):
        result = real(fs, path, product)
        time.sleep(0.3)  # widen the read-to-write gap so an unlocked writer would overlap
        return result

    monkeypatch.setattr(archive, "_read_current", slow_read)
    errors: list[BaseException] = []

    def writer(rows):
        try:
            started.wait()
            write_partitioned(toy_frame(rows), archive_dir, TOY)
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [
        threading.Thread(target=writer, args=(OURS,)),
        threading.Thread(target=writer, args=(THEIRS,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    stored = read_partitioned(archive_dir, TOY)
    assert len(stored) == len(OURS) + len(THEIRS)
    assert set(stored["site_id"]) == {"a", "b"}


# @spec OBS-ARCHIVE-025
def test_lock_file_is_beside_the_partition_and_never_read(archive_dir):
    archive_dir = str(archive_dir)
    write_partitioned(toy_frame(OURS), archive_dir, TOY)
    directory = os.path.join(archive_dir, "toy/values/date=2026-09-20")
    assert ".values.parquet.lock" in os.listdir(directory)
    assert len(read_partitioned(archive_dir, TOY)) == len(OURS)
    write_sites([make_site()], archive_dir)
    assert ".sites.parquet.lock" in os.listdir(os.path.join(archive_dir, "source=purpleair"))
    assert len(read_sites(archive_dir)) == 1
