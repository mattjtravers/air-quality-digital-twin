"""The GeoParquet archive: partitioned, deterministic, atomic writes and validated reads.

``write_partitioned`` / ``read_partitioned`` are the two primitives every product uses; the
observation and site functions are thin wrappers over them with their ``Product`` definitions.
"""

from __future__ import annotations

import contextlib
import fcntl
import io
import json
import logging
import os
import uuid
from collections.abc import Callable, Iterable, Iterator
from datetime import datetime
from typing import Any

import fsspec
import geopandas as gpd
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from fsspec.implementations.local import LocalFileSystem
from pandera.errors import SchemaError

from aqdt.observation_store.frames import (
    empty_frame,
    observations_to_frame,
    sites_to_frame,
    validate_partition,
)
from aqdt.observation_store.products import OBSERVATIONS, SITES, Product, render_partition_value
from aqdt.observation_store.schemas import Observation, Site, Source

log = logging.getLogger(__name__)

ARCHIVE_URI_VAR = "AQDT_ARCHIVE_URI"


class MissingConfiguration(RuntimeError):
    """A required environment variable is unset."""


class ConcurrentWriteError(RuntimeError):
    """A partition kept changing under this writer for every allowed attempt."""


MAX_WRITE_ATTEMPTS = 5


# @spec OBS-ENV-001, OBS-ENV-002
def resolve_archive_uri(archive_uri: str | None) -> str:
    """The archive to use: the explicit URI, else ``AQDT_ARCHIVE_URI``, else an error naming it."""
    if archive_uri:
        return archive_uri
    from_env = os.environ.get(ARCHIVE_URI_VAR)
    if not from_env:
        raise MissingConfiguration(f"{ARCHIVE_URI_VAR} is not set and no archive_uri was given")
    return from_env


# @spec OBS-ARCHIVE-002
def _filesystem(archive_uri: str) -> tuple[fsspec.AbstractFileSystem, str]:
    """Resolve the URI once; every read and write goes through the returned filesystem."""
    fs, root = fsspec.core.url_to_fs(archive_uri)
    return fs, root.rstrip("/")


def _join(first: str, *parts: str) -> str:
    """Join path segments; the first keeps its scheme or leading slash, the rest are trimmed."""
    tail = [p.strip("/") for p in parts if p and p.strip("/")]
    return "/".join([first.rstrip("/"), *tail]) if tail else first.rstrip("/")


# --- serialization at the archive boundary -------------------------------------------------


# @spec OBS-ARCHIVE-003, OBS-ARCHIVE-004
def _encode(frame: pd.DataFrame, product: Product) -> bytes:
    """Serialize one partition: GeoParquet when the product has geometry, plain Parquet otherwise.

    ``raw`` dicts become canonical JSON (sorted keys, default float formatting); ``qc_flags``
    becomes an Arrow ``list<string>`` so an all-empty column keeps its type.
    """
    out = frame.reset_index(drop=True).copy()
    if "raw" in out.columns:
        out["raw"] = out["raw"].map(lambda value: json.dumps(value, sort_keys=True))
    if "qc_flags" in out.columns:
        out["qc_flags"] = pd.Series(
            [list(v) for v in out["qc_flags"]],
            dtype=pd.ArrowDtype(pa.list_(pa.string())),
            index=out.index,
        )
    buffer = io.BytesIO()
    if product.has_geometry:
        gpd.GeoDataFrame(out, geometry="geometry", crs=frame.crs).to_parquet(buffer, index=False)
    else:
        pd.DataFrame(out).to_parquet(buffer, index=False)
    return _strip_pandas_metadata(buffer.getvalue())


def _strip_pandas_metadata(data: bytes) -> bytes:
    """Drop the pandas-specific schema metadata so the file depends on nothing but Arrow types
    (and GeoParquet's ``geo`` metadata); any reader reconstructs the frame from the schema."""
    table = pq.read_table(io.BytesIO(data))
    metadata = {k: v for k, v in (table.schema.metadata or {}).items() if k != b"pandas"}
    out = io.BytesIO()
    pq.write_table(table.replace_schema_metadata(metadata), out)
    return out.getvalue()


def _decode(frame: pd.DataFrame, product: Product) -> pd.DataFrame:
    """Undo ``_encode``: JSON strings back to dicts, Arrow lists back to Python lists."""
    if "raw" in frame.columns:
        frame["raw"] = frame["raw"].map(json.loads)
    if "qc_flags" in frame.columns:
        frame["qc_flags"] = pd.Series(
            [list(v) for v in frame["qc_flags"]], index=frame.index, dtype="object"
        )
    return frame


def _read_partition(fs: fsspec.AbstractFileSystem, path: str, product: Product) -> pd.DataFrame:
    if product.has_geometry:
        frame = gpd.read_parquet(path, filesystem=fs)
    else:
        frame = pd.read_parquet(path, filesystem=fs)
    return _decode(frame, product)


def _parse_partition(data: bytes, product: Product) -> pd.DataFrame:
    buffer = io.BytesIO(data)
    frame = gpd.read_parquet(buffer) if product.has_geometry else pd.read_parquet(buffer)
    return _decode(frame, product)


# @spec OBS-ARCHIVE-023
def _read_current(
    fs: fsspec.AbstractFileSystem, path: str, product: Product
) -> tuple[pd.DataFrame | None, str | None]:
    """The partition as stored right now, with the token a conditional write must present.

    On S3 the token is the object's ETag, taken from the same request that returned the bytes.
    Locally there is no token; the advisory lock makes the read-merge-write exclusive instead.
    Returns ``(None, None)`` when the partition does not exist.
    """
    if isinstance(fs, LocalFileSystem):
        return (_read_partition(fs, path, product) if os.path.exists(path) else None), None
    fs.invalidate_cache(path)
    try:
        with fs.open(path, "rb") as handle:
            data = handle.read()
            token = getattr(handle, "details", {}).get("ETag")
    except FileNotFoundError:
        return None, None
    return _parse_partition(data, product), token


def _is_precondition_failure(error: BaseException) -> bool:
    """Whether an exception (possibly wrapped by s3fs) is S3's 412 Precondition Failed."""
    seen: BaseException | None = error
    while seen is not None:
        response = getattr(seen, "response", None)
        if isinstance(response, dict):
            code = response.get("Error", {}).get("Code")
            if code in ("PreconditionFailed", "412"):
                return True
        if "pre-condition" in str(seen).lower() or "precondition" in str(seen).lower():
            return True
        seen = seen.__cause__ or seen.__context__
    return False


# @spec OBS-ARCHIVE-009, OBS-ARCHIVE-023
def _write_atomic(fs: fsspec.AbstractFileSystem, path: str, data: bytes, token: str | None) -> bool:
    """One object per partition, never partially visible; on S3, only if unchanged since read.

    Locally: a temporary file renamed over the target (the caller holds the partition lock).
    On S3: a single conditional PUT — ``If-Match: token`` when the partition existed,
    ``If-None-Match: *`` when it did not. Returns ``False`` when S3 refused the write because
    the object changed; any other failure propagates.
    """
    if isinstance(fs, LocalFileSystem):
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(directory, f".{os.path.basename(path)}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        return True
    bucket, key = path.split("/", 1)
    condition = {"IfMatch": token} if token else {"IfNoneMatch": "*"}
    try:
        fs.call_s3("put_object", Bucket=bucket, Key=key, Body=data, **condition)
    except OSError as error:
        if _is_precondition_failure(error):
            return False
        raise
    finally:
        fs.invalidate_cache(path)
    return True


# @spec OBS-ARCHIVE-025
@contextlib.contextmanager
def _partition_lock(fs: fsspec.AbstractFileSystem, path: str) -> Iterator[None]:
    """Exclusive access to one local partition across read-merge-write; a no-op on S3, where
    the conditional write provides the guarantee instead."""
    if not isinstance(fs, LocalFileSystem):
        yield
        return
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    lock_path = os.path.join(directory, f".{os.path.basename(path)}.lock")
    with open(lock_path, "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


# --- partition primitives ------------------------------------------------------------------


def _render_keys(frame: pd.DataFrame, product: Product) -> pd.DataFrame:
    rendered = {}
    for name, derive in product.partition_keys.items():
        values = derive(frame)
        rendered[name] = pd.Series(
            [render_partition_value(v) for v in values], index=frame.index, dtype="object"
        )
    return pd.DataFrame(rendered, index=frame.index)


def _partition_path(root: str, product: Product, keys: dict[str, str]) -> str:
    segments = [f"{name}={value}" for name, value in keys.items()]
    return _join(root, product.prefix, *segments, product.filename)


def _merge(
    existing: pd.DataFrame | None, incoming: pd.DataFrame, product: Product, crs: Any
) -> pd.DataFrame:
    """Incoming rows replace stored rows with the same key; the result is sorted and validated."""
    if existing is not None:
        incoming_keys = incoming[product.key_cols].apply(tuple, axis=1)
        existing_keys = existing[product.key_cols].apply(tuple, axis=1)
        kept = existing[~existing_keys.isin(set(incoming_keys))]
        merged = pd.concat([kept, incoming], ignore_index=True)
    else:
        merged = incoming
    merged = merged.sort_values(product.key_cols, kind="stable").reset_index(drop=True)
    if product.has_geometry:
        merged = gpd.GeoDataFrame(merged, geometry="geometry", crs=crs)
    return validate_partition(merged, product.frame_model)


# @spec OBS-ARCHIVE-024, OBS-ARCHIVE-026
def _write_partition(
    fs: fsspec.AbstractFileSystem, path: str, incoming: pd.DataFrame, product: Product, crs: Any
) -> None:
    """Read-merge-write one partition so that no concurrent writer's rows are lost.

    Locally the whole step runs under the partition lock. On S3 the write is conditional on
    the token from the read; a refusal means another writer landed first, so the partition is
    re-read and the merge redone, up to ``MAX_WRITE_ATTEMPTS`` times.
    """
    with _partition_lock(fs, path):
        for attempt in range(1, MAX_WRITE_ATTEMPTS + 1):
            existing, token = _read_current(fs, path, product)
            merged = _merge(existing, incoming, product, crs)
            if _write_atomic(fs, path, _encode(merged, product), token):
                return
            log.info("partition changed under writer (attempt %d): %s", attempt, path)
    raise ConcurrentWriteError(
        f"partition {path} changed under this writer on every one of {MAX_WRITE_ATTEMPTS} attempts"
    )


# @spec OBS-ARCHIVE-005, OBS-ARCHIVE-006, OBS-ARCHIVE-007, OBS-ARCHIVE-008, OBS-ARCHIVE-010
# @spec OBS-ARCHIVE-011, OBS-ARCHIVE-012, OBS-ARCHIVE-014
def write_partitioned(frame: pd.DataFrame, archive_uri: str | None, product: Product) -> list[str]:
    """Validate, group by rendered partition keys, and read-merge-write each partition.

    Incoming rows replace stored rows with the same ``key_cols``; a duplicate key within one call
    fails validation before any file is touched. Returns the partition URIs written.
    """
    archive_uri = resolve_archive_uri(archive_uri)
    validated = product.frame_model.validate(frame)
    if len(validated) == 0:
        return []
    duplicated = validated.duplicated(product.key_cols, keep=False)
    if duplicated.any():
        raise SchemaError(
            product.frame_model.to_schema(),
            validated[duplicated],
            f"duplicate {product.key_cols} within one write batch (an ingester defect)",
        )
    fs, root = _filesystem(archive_uri)
    keys = _render_keys(validated, product)
    key_names = list(product.partition_keys)
    written: list[str] = []
    if key_names:
        groups = [(tuple(values), group.index) for values, group in keys.groupby(key_names)]
    else:
        groups = [((), keys.index)]
    for values, index in sorted(groups, key=lambda item: item[0]):
        partition_keys = dict(zip(key_names, values, strict=True))
        path = _partition_path(root, product, partition_keys)
        incoming = validated.loc[index]
        crs = validated.crs if product.has_geometry else None
        _write_partition(fs, path, incoming, product, crs)
        written.append(
            _join(
                archive_uri,
                product.prefix,
                *[f"{k}={v}" for k, v in partition_keys.items()],
                product.filename,
            )
        )
    return written


Filter = Any | Callable[[str], bool]


def _matches(value: str, condition: Filter) -> bool:
    if callable(condition):
        return bool(condition(value))
    return value == render_partition_value(condition)


# @spec OBS-ARCHIVE-015, OBS-ARCHIVE-019
def read_partitioned(archive_uri: str | None, product: Product, **filters: Filter) -> pd.DataFrame:
    """Read every partition whose rendered keys satisfy the filters into one validated frame.

    A filter is a literal (equality against the rendered key) or a predicate on the rendered key.
    No matching partition is not an error: an empty typed frame is returned and the request logged.
    """
    archive_uri = resolve_archive_uri(archive_uri)
    fs, root = _filesystem(archive_uri)
    base = _join(root, product.prefix)
    key_names = list(product.partition_keys)
    paths: list[str] = []
    if fs.exists(base):
        for path in sorted(fs.find(base)):
            if os.path.basename(path) != product.filename:
                continue
            relative = path[len(base) :].strip("/").split("/")[:-1]
            if len(relative) != len(key_names):
                continue
            keys = {}
            for segment, name in zip(relative, key_names, strict=True):
                if "=" not in segment or segment.split("=", 1)[0] != name:
                    break
                keys[name] = segment.split("=", 1)[1]
            else:
                if all(_matches(keys[name], condition) for name, condition in filters.items()):
                    paths.append(path)
    if not paths:
        log.info(
            "no partition matched: archive=%s product=%s filters=%s",
            archive_uri,
            product.prefix or product.filename,
            {k: (v if not callable(v) else "<predicate>") for k, v in filters.items()},
        )
        return empty_frame(product.frame_model)
    frames = [_read_partition(fs, path, product) for path in paths]
    combined = pd.concat(frames, ignore_index=True)
    if product.has_geometry:
        combined = gpd.GeoDataFrame(combined, geometry="geometry", crs=frames[0].crs)
    return product.frame_model.validate(combined)


# --- observation and site wrappers ---------------------------------------------------------


# @spec OBS-ARCHIVE-001, OBS-ARCHIVE-016
def write_observations(records: Iterable[Observation], archive_uri: str | None = None) -> list[str]:
    return write_partitioned(observations_to_frame(records), archive_uri, OBSERVATIONS)


# @spec OBS-ARCHIVE-013, OBS-ARCHIVE-016
def write_sites(records: Iterable[Site], archive_uri: str | None = None) -> list[str]:
    return write_partitioned(sites_to_frame(records), archive_uri, SITES)


# @spec OBS-ARCHIVE-017, OBS-SCHEMA-015
def read_observations(
    archive_uri: str | None = None,
    source: Source | str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> gpd.GeoDataFrame:
    """Observations from every partition intersecting the half-open window ``[start, end)``,
    filtered to ``start <= observed_at < end``."""
    filters: dict[str, Filter] = {}
    if source is not None:
        filters["source"] = Source(source).value
    if start is not None or end is not None:
        first = render_partition_value(start.date()) if start else None
        last = render_partition_value((end - pd.Timedelta(microseconds=1)).date()) if end else None
        filters["date"] = lambda d: (first is None or d >= first) and (last is None or d <= last)
    frame = read_partitioned(archive_uri, OBSERVATIONS, **filters)
    if len(frame) == 0:
        return frame
    mask = pd.Series(True, index=frame.index)
    if start is not None:
        mask &= frame["observed_at"] >= pd.Timestamp(start)
    if end is not None:
        mask &= frame["observed_at"] < pd.Timestamp(end)
    return frame[mask].reset_index(drop=True)


# @spec OBS-ARCHIVE-018
def read_sites(
    archive_uri: str | None = None, source: Source | str | None = None
) -> gpd.GeoDataFrame:
    filters: dict[str, Filter] = {}
    if source is not None:
        filters["source"] = Source(source).value
    return read_partitioned(archive_uri, SITES, **filters)
