"""Guarded whole-object writes — OBS-ARCHIVE-027 to OBS-ARCHIVE-030, and partition discovery
ignoring neighbouring objects (OBS-ARCHIVE-015)."""

import os
import threading
import time

import boto3
import pytest

from aqdt.observation_store import archive
from aqdt.observation_store.archive import read_partitioned, replace_object, write_partitioned

from .conftest import TOY, toy_frame

KEY = "fusion/surfaces/date=2026-09-20/pm25_2026-09-20T10.tif"


def _s3_parts(s3_archive_uri: str) -> tuple[str, str]:
    bucket, prefix = s3_archive_uri[len("s3://") :].split("/", 1)
    return bucket, f"{prefix}/{KEY}"


def _s3_get(s3_archive_uri: str) -> bytes | None:
    bucket, key = _s3_parts(s3_archive_uri)
    try:
        return boto3.client("s3").get_object(Bucket=bucket, Key=key)["Body"].read()
    except boto3.client("s3").exceptions.NoSuchKey:
        return None


def _s3_put(s3_archive_uri: str, data: bytes) -> None:
    """Another writer, landing directly in S3 — not through the store."""
    bucket, key = _s3_parts(s3_archive_uri)
    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=data)


class Producer:
    """A ``produce`` callable that records each call and returns the next scripted value; an
    optional side effect runs inside the call, after the store has read its token."""

    def __init__(self, *values, during=None):
        self.values = list(values)
        self.calls = 0
        self.during = during

    def __call__(self):
        self.calls += 1
        if self.during is not None:
            self.during(self.calls)
        return self.values[min(self.calls, len(self.values)) - 1]


# --- OBS-ARCHIVE-027 -----------------------------------------------------------------------


# @spec OBS-ARCHIVE-027
def test_replace_object_writes_replaces_and_deletes_locally(tmp_path):
    root = str(tmp_path / "archive")
    path = os.path.join(root, KEY)

    assert replace_object(root, KEY, Producer(b"first")) == f"{root}/{KEY}"
    assert open(path, "rb").read() == b"first"

    assert replace_object(root, KEY, Producer(b"second")) == f"{root}/{KEY}"
    assert open(path, "rb").read() == b"second"

    assert replace_object(root, KEY, Producer(None)) is None
    assert not os.path.exists(path)

    # already absent: nothing to do, still None
    assert replace_object(root, KEY, Producer(None)) is None
    assert not os.path.exists(path)


# @spec OBS-ARCHIVE-027
def test_replace_object_resolves_and_checks_the_archive_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("AQDT_ARCHIVE_URI", str(tmp_path / "from-env"))
    replace_object(None, KEY, Producer(b"x"))
    assert (tmp_path / "from-env" / KEY).read_bytes() == b"x"

    monkeypatch.setenv("AQDT_ARCHIVE_URI", "s3://some-other-bucket/archive")
    produce = Producer(b"x")
    with pytest.raises(archive.MissingConfiguration, match="some-other-bucket"):
        replace_object(None, KEY, produce)
    assert produce.calls == 0


# @spec OBS-ARCHIVE-027
# @spec OBS-ARCHIVE-028
def test_replace_object_on_s3_writes_and_deletes(s3_archive_uri):
    assert replace_object(s3_archive_uri, KEY, Producer(b"first")) == f"{s3_archive_uri}/{KEY}"
    assert _s3_get(s3_archive_uri) == b"first"
    assert replace_object(s3_archive_uri, KEY, Producer(b"second")) is not None
    assert _s3_get(s3_archive_uri) == b"second"
    assert replace_object(s3_archive_uri, KEY, Producer(None)) is None
    assert _s3_get(s3_archive_uri) is None
    assert replace_object(s3_archive_uri, KEY, Producer(None)) is None


# --- OBS-ARCHIVE-028, OBS-ARCHIVE-029: refusal and retry ------------------------------------


# @spec OBS-ARCHIVE-028
# @spec OBS-ARCHIVE-029
@pytest.mark.parametrize("pre_existing", [True, False])
def test_s3_write_refused_when_object_changed_after_the_token_reproduces(
    s3_archive_uri, pre_existing
):
    if pre_existing:
        _s3_put(s3_archive_uri, b"old")

    # The competitor lands after our token was read (inside our first produce call).
    produce = Producer(
        b"ours-from-stale-inputs",
        b"ours-from-fresh-inputs",
        during=lambda n: _s3_put(s3_archive_uri, b"theirs") if n == 1 else None,
    )
    replace_object(s3_archive_uri, KEY, produce)
    assert produce.calls == 2
    assert _s3_get(s3_archive_uri) == b"ours-from-fresh-inputs"


# @spec OBS-ARCHIVE-028
# @spec OBS-ARCHIVE-029
def test_s3_delete_refused_when_object_changed_after_the_token(s3_archive_uri):
    _s3_put(s3_archive_uri, b"old")
    # first decision (delete) was made before a competitor wrote; the retry decides again
    produce = Producer(
        None,
        b"kept",
        during=lambda n: _s3_put(s3_archive_uri, b"theirs") if n == 1 else None,
    )
    assert replace_object(s3_archive_uri, KEY, produce) is not None
    assert produce.calls == 2
    assert _s3_get(s3_archive_uri) == b"kept"


# @spec OBS-ARCHIVE-029
def test_s3_gives_up_after_five_refusals_naming_the_object(s3_archive_uri):
    produce = Producer(b"ours", during=lambda n: _s3_put(s3_archive_uri, f"theirs-{n}".encode()))
    with pytest.raises(archive.ConcurrentWriteError, match="pm25_2026-09-20T10.tif"):
        replace_object(s3_archive_uri, KEY, produce)
    assert produce.calls == 5
    assert _s3_get(s3_archive_uri) == b"theirs-5"


# --- OBS-ARCHIVE-030: local lock ----------------------------------------------------------


# @spec OBS-ARCHIVE-030
def test_local_callers_on_one_object_are_serialized_across_produce(tmp_path):
    root = str(tmp_path / "archive")
    intervals: list[tuple[float, float]] = []
    started = threading.Barrier(2)
    errors: list[BaseException] = []

    def slow(label: bytes):
        def produce():
            begin = time.monotonic()
            time.sleep(0.3)
            intervals.append((begin, time.monotonic()))
            return label

        return produce

    def caller(label):
        try:
            started.wait()
            replace_object(root, KEY, slow(label))
        except BaseException as error:  # noqa: BLE001 - surfaced below
            errors.append(error)

    threads = [threading.Thread(target=caller, args=(x,)) for x in (b"a", b"b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    (a_start, a_end), (b_start, b_end) = sorted(intervals)
    assert a_end <= b_start, "produce calls overlapped: the lock was not held across produce"
    directory = os.path.dirname(os.path.join(root, KEY))
    assert ".pm25_2026-09-20T10.tif.lock" in os.listdir(directory)
    assert open(os.path.join(root, KEY), "rb").read() in (b"a", b"b")


# --- discovery ignores neighbouring objects (fusion keeps rasters beside surface_fits) ------


# @spec OBS-ARCHIVE-015
def test_partition_discovery_ignores_other_files_and_sibling_prefixes(tmp_path):
    root = tmp_path / "archive"
    write_partitioned(toy_frame([("a", "2026-09-20T10:00", 1.0)]), str(root), TOY)
    # a raster inside the product's own partition directory, and a sibling prefix sharing the
    # product prefix's leading characters, each holding a file with the product's file name
    (root / "toy/values/date=2026-09-20/pm25.tif").write_bytes(b"not parquet")
    stray = root / "toy/values_rasters/date=2026-09-20"
    stray.mkdir(parents=True)
    (stray / "values.parquet").write_bytes(b"not parquet either")
    stored = read_partitioned(str(root), TOY)
    assert list(stored["site_id"]) == ["a"]
