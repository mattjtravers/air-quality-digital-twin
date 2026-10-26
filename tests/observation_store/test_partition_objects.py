"""Listing partition objects without reading them — OBS-ARCHIVE-031."""

from datetime import UTC, datetime, timedelta

import pytest

from aqdt.observation_store.archive import (
    ObjectInfo,
    partition_objects,
    read_partitioned,
    write_observations,
)
from aqdt.observation_store.products import OBSERVATIONS
from aqdt.observation_store.schemas import Source

from .conftest import T0, make_observation

DAY = timedelta(days=1)


def _populate(uri):
    write_observations(
        [
            make_observation(observed_at=T0),
            make_observation(observed_at=T0 + DAY),
            make_observation(
                site_id="airnow:840110010001",
                source=Source.airnow,
                pm25_channel_a=None,
                pm25_channel_b=None,
                pm25_corrected=None,
                observed_at=T0,
            ),
        ],
        uri,
    )


# @spec OBS-ARCHIVE-031
def test_lists_the_objects_a_read_would_read_on_a_local_archive(tmp_path):
    uri = str(tmp_path / "archive")
    _populate(uri)
    objects = partition_objects(uri, OBSERVATIONS, source="purpleair")
    assert [o.key for o in objects] == [
        "source=purpleair/date=2026-09-20/observations.parquet",
        "source=purpleair/date=2026-09-21/observations.parquet",
    ]
    for o in objects:
        assert isinstance(o, ObjectInfo)
        assert o.size == (tmp_path / "archive" / o.key).stat().st_size
        assert o.etag is None and o.last_modified is None
    one_day = partition_objects(uri, OBSERVATIONS, source="purpleair", date="2026-09-21")
    assert [o.key for o in one_day] == [objects[1].key]
    assert partition_objects(uri, OBSERVATIONS, source="nothing") == []
    assert len(read_partitioned(uri, OBSERVATIONS, source="purpleair")) == 2


# @spec OBS-ARCHIVE-031
def test_s3_objects_carry_etag_and_last_modified(s3_archive_uri):
    before = datetime.now(UTC) - timedelta(minutes=5)
    _populate(s3_archive_uri)
    objects = partition_objects(s3_archive_uri, OBSERVATIONS, source="airnow")
    assert [o.key for o in objects] == ["source=airnow/date=2026-09-20/observations.parquet"]
    (o,) = objects
    assert o.size > 0 and o.etag
    assert o.last_modified is not None and o.last_modified.tzinfo is not None
    assert o.last_modified >= before


@pytest.mark.parametrize("filters", [{}, {"source": "airnow"}])
# @spec OBS-ARCHIVE-031
def test_listing_reads_no_object_contents(tmp_path, monkeypatch, filters):
    uri = str(tmp_path / "archive")
    _populate(uri)
    from aqdt.observation_store import archive

    def refuse(*args, **kwargs):
        raise AssertionError("partition_objects read an object's contents")

    monkeypatch.setattr(archive, "_read_partition", refuse)
    assert partition_objects(uri, OBSERVATIONS, **filters)
