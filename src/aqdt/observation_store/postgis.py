"""The PostGIS serving layer: schema, partition load, and rebuild, driven by the product registry.

Data flows one way, archive → PostGIS. Nothing is written here that was not first read from the
archive, and no other module writes to the database.
"""

# @spec OBS-PG-010
from __future__ import annotations

import math
import os
from collections.abc import Iterable
from typing import Any

import pandas as pd
import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from aqdt import registry
from aqdt.observation_store.archive import _filesystem, _read_partition, resolve_archive_uri
from aqdt.observation_store.products import Product

DATABASE_URL_VAR = "DATABASE_URL"


# @spec OBS-PG-011
def connect() -> psycopg.Connection:
    """Open the PostGIS connection named by ``DATABASE_URL``."""
    url = os.environ.get(DATABASE_URL_VAR)
    if not url:
        raise RuntimeError(f"{DATABASE_URL_VAR} is not set")
    return psycopg.connect(url)


# @spec OBS-PG-003
def apply_schema(conn: psycopg.Connection) -> None:
    """Apply every registered product's SQL files, in registry order; idempotent."""
    seen: set = set()
    with conn.cursor() as cur:
        for product in registry.PRODUCTS:
            if product.sql_dir is None or product.sql_dir in seen:
                continue
            seen.add(product.sql_dir)
            for path in sorted(product.sql_dir.glob("*.sql")):
                cur.execute(path.read_text())
    conn.commit()


def _product_for(archive_uri: str, partition_uri: str) -> tuple[Product, str]:
    """Identify a partition's product from its path; return it with the path within the archive."""
    root = archive_uri.rstrip("/")
    if not partition_uri.startswith(root + "/"):
        raise ValueError(f"{partition_uri} is not under {archive_uri}")
    relative = partition_uri[len(root) + 1 :]
    for product in registry.PRODUCTS:
        prefix = product.prefix.strip("/")
        if prefix:
            if not relative.startswith(prefix + "/"):
                continue
            within = relative[len(prefix) + 1 :]
        else:
            within = relative
        segments = within.split("/")
        keys, filename = segments[:-1], segments[-1]
        if filename != product.filename or len(keys) != len(product.partition_keys):
            continue
        if all("=" in s for s in keys):
            return product, relative
    raise ValueError(f"no registered product owns {partition_uri}")


def _to_sql_value(value: Any) -> Any:
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        return None if pd.isna(value) else value.to_pydatetime()
    if isinstance(value, dict):
        return Jsonb(value)
    if isinstance(value, (list, tuple)) or hasattr(value, "tolist") and not isinstance(value, str):
        return [_to_sql_value(v) for v in (value.tolist() if hasattr(value, "tolist") else value)]
    if value is pd.NaT or value is None:
        return None
    return value


# @spec OBS-PG-006, OBS-PG-007, OBS-PG-009
def _upsert(conn: psycopg.Connection, product: Product, frame: pd.DataFrame) -> None:
    """``insert ... on conflict do update`` of one partition into its table, one transaction."""
    columns = [c for c in frame.columns if c != "geometry"]
    has_geometry = "geometry" in frame.columns
    insert_columns = [*columns, "geom"] if has_geometry else columns
    placeholders = [sql.Placeholder()] * len(columns)
    if has_geometry:
        placeholders.append(sql.SQL("ST_SetSRID(ST_GeomFromWKB(%s), 4326)"))
    updates = [c for c in insert_columns if c not in product.key_cols]
    statement = sql.SQL(
        "insert into {table} ({cols}) values ({vals}) on conflict ({keys}) do update set {sets}"
    ).format(
        table=sql.Identifier(product.table),
        cols=sql.SQL(", ").join(map(sql.Identifier, insert_columns)),
        vals=sql.SQL(", ").join(placeholders),
        keys=sql.SQL(", ").join(map(sql.Identifier, product.key_cols)),
        sets=sql.SQL(", ").join(
            sql.SQL("{c} = excluded.{c}").format(c=sql.Identifier(c)) for c in updates
        ),
    )
    rows = []
    for record in frame.to_dict("records"):
        values = [_to_sql_value(record[c]) for c in columns]
        if has_geometry:
            values.append(record["geometry"].wkb)
        rows.append(values)
    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(statement, rows)


# @spec OBS-PG-004, OBS-PG-008
def load_partitions(
    conn: psycopg.Connection, archive_uri: str | None, partition_uris: Iterable[str]
) -> None:
    """Upsert the named archive partitions into their tables, in registry order."""
    archive_uri = resolve_archive_uri(archive_uri)
    fs, root = _filesystem(archive_uri)
    resolved = [(_product_for(archive_uri, uri), uri) for uri in partition_uris]
    order = {id(p): i for i, p in enumerate(registry.PRODUCTS)}
    resolved.sort(key=lambda item: order[id(item[0][0])])
    for (product, relative), _uri in resolved:
        frame = _read_partition(fs, f"{root}/{relative}", product)
        _upsert(conn, product, frame)


# @spec OBS-PG-005
def rebuild(conn: psycopg.Connection, archive_uri: str | None) -> None:
    """Truncate every registered table and reload every partition of every product."""
    archive_uri = resolve_archive_uri(archive_uri)
    fs, root = _filesystem(archive_uri)
    with conn.transaction():
        with conn.cursor() as cur:
            for product in reversed(registry.PRODUCTS):
                cur.execute(
                    sql.SQL("truncate table {} cascade").format(sql.Identifier(product.table))
                )
    for product in registry.PRODUCTS:
        base = "/".join(p for p in (root, product.prefix.strip("/")) if p)
        if not fs.exists(base):
            continue
        for path in sorted(fs.find(base)):
            if os.path.basename(path) != product.filename:
                continue
            segments = path[len(base) :].strip("/").split("/")[:-1]
            if len(segments) != len(product.partition_keys) or not all("=" in s for s in segments):
                continue
            _upsert(conn, product, _read_partition(fs, path, product))
