"""PostGIS serving layer — OBS-PG. Needs DATABASE_URL; skipped otherwise (OBS-ENV-006)."""

import dataclasses
import os
from datetime import timedelta
from pathlib import Path

import pytest

from aqdt import registry
from aqdt.observation_store import postgis
from aqdt.observation_store.archive import write_observations, write_partitioned, write_sites
from aqdt.observation_store.postgis import apply_schema, load_partitions, rebuild
from aqdt.observation_store.products import OBSERVATIONS, SITES, Product
from aqdt.observation_store.schemas import QcFlag, Source

from .conftest import T0, TOY, make_observation, make_site, toy_frame

DAY = timedelta(days=1)


# @spec OBS-ENV-006
@pytest.fixture
def conn():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is unset; PostGIS tests need a database")
    import psycopg

    with psycopg.connect(url) as connection:
        with connection.cursor() as cur:
            for product in reversed(registry.PRODUCTS):
                cur.execute(f"drop table if exists {product.table} cascade")
            cur.execute("drop table if exists toy_values cascade")
        connection.commit()
        apply_schema(connection)
        yield connection


def _columns(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            select column_name, is_nullable, udt_name
            from information_schema.columns
            where table_name = %s
            """,
            (table,),
        )
        return {name: (nullable == "YES", udt) for name, nullable, udt in cur.fetchall()}


def _primary_key(conn, table):
    with conn.cursor() as cur:
        cur.execute(
            """
            select a.attname
            from pg_index i
            join pg_attribute a on a.attrelid = i.indrelid and a.attnum = any(i.indkey)
            where i.indrelid = %s::regclass and i.indisprimary
            order by array_position(i.indkey, a.attnum)
            """,
            (table,),
        )
        return [row[0] for row in cur.fetchall()]


def _indexdefs(conn, table):
    with conn.cursor() as cur:
        cur.execute("select indexdef from pg_indexes where tablename = %s", (table,))
        return [row[0].lower() for row in cur.fetchall()]


def _rows(conn, table, order):
    with conn.cursor() as cur:
        cur.execute(f"select * from {table} order by {order}")
        return cur.fetchall()


@pytest.fixture
def archive(tmp_path):
    uri = str(tmp_path / "archive")
    sites = write_sites(
        [
            make_site(site_id="purpleair:1"),
            make_site(
                site_id="airnow:840110010043",
                source=Source.airnow,
                source_native_id="110010043",
                site_type="reference_monitor",
                latitude=38.92,
                longitude=-77.01,
            ),
        ],
        uri,
    )
    observations = write_observations(
        [
            make_observation(site_id="purpleair:1", observed_at=T0, qc_flags=[QcFlag.flatline]),
            make_observation(site_id="purpleair:1", observed_at=T0 + DAY),
            make_observation(
                site_id="airnow:840110010043",
                source=Source.airnow,
                observed_at=T0,
                latitude=38.92,
                longitude=-77.01,
                pm25_channel_a=None,
                pm25_channel_b=None,
                humidity=None,
                pm25_corrected=None,
            ),
        ],
        uri,
    )
    return uri, sites, observations


# --- Schema -------------------------------------------------------------------


# @spec OBS-PG-001
def test_sites_and_observations_tables_match_the_records(conn):
    sites = _columns(conn, "sites")
    assert set(sites) == {
        "site_id",
        "source",
        "source_native_id",
        "site_type",
        "name",
        "latitude",
        "longitude",
        "geom",
    }
    assert sites["name"][0] is True
    assert all(not sites[c][0] for c in sites if c != "name")
    assert sites["geom"][1] == "geometry"
    assert _primary_key(conn, "sites") == ["site_id"]

    obs = _columns(conn, "observations")
    assert set(obs) == {
        "site_id",
        "source",
        "observed_at",
        "latitude",
        "longitude",
        "pm25_raw",
        "pm25_channel_a",
        "pm25_channel_b",
        "humidity",
        "pm25_corrected",
        "qc_flags",
        "raw",
        "geom",
    }
    nullable = {c for c, (n, _) in obs.items() if n}
    assert nullable == {
        "pm25_raw",
        "pm25_channel_a",
        "pm25_channel_b",
        "humidity",
        "pm25_corrected",
    }
    assert obs["observed_at"][1] == "timestamptz"
    assert obs["qc_flags"][1] == "_text"
    assert obs["raw"][1] == "jsonb"
    assert obs["geom"][1] == "geometry"
    assert _primary_key(conn, "observations") == ["site_id", "observed_at"]

    with conn.cursor() as cur:
        cur.execute(
            """
            select count(*) from information_schema.table_constraints
            where table_name = 'observations' and constraint_type = 'FOREIGN KEY'
            """
        )
        assert cur.fetchone()[0] == 1


# @spec OBS-PG-002
def test_indexes(conn):
    sites = _indexdefs(conn, "sites")
    assert any("using gist (geom)" in d for d in sites)
    obs = _indexdefs(conn, "observations")
    assert any("using gist (geom)" in d for d in obs)
    assert any("using btree (observed_at)" in d for d in obs)
    assert any("using btree (source, observed_at)" in d for d in obs)


# @spec OBS-PG-003
def test_apply_schema_is_idempotent_and_covers_every_registered_product(conn):
    before = {p.table: _columns(conn, p.table) for p in registry.PRODUCTS}
    assert all(before.values())
    apply_schema(conn)
    apply_schema(conn)
    assert {p.table: _columns(conn, p.table) for p in registry.PRODUCTS} == before


# @spec OBS-PG-012
def test_registry_lists_sites_then_observations_then_downstream_products():
    assert registry.PRODUCTS[0] is SITES
    assert registry.PRODUCTS[1] is OBSERVATIONS
    assert all(isinstance(p, Product) for p in registry.PRODUCTS)
    assert len({p.table for p in registry.PRODUCTS}) == len(registry.PRODUCTS)


# @spec OBS-PG-012
def test_postgis_layer_is_driven_by_the_registry(conn, tmp_path, monkeypatch):
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "001_toy.sql").write_text(
        "create table if not exists toy_values ("
        " site_id text not null, hour timestamptz not null, value double precision not null,"
        " primary key (site_id, hour));"
    )
    toy = dataclasses.replace(TOY, sql_dir=sql_dir)
    monkeypatch.setattr(registry, "PRODUCTS", [*registry.PRODUCTS, toy])

    apply_schema(conn)
    assert set(_columns(conn, "toy_values")) == {"site_id", "hour", "value"}

    uri = str(tmp_path / "archive")
    uris = write_partitioned(toy_frame([("s1", T0, 1.0), ("s1", T0 + DAY, 2.0)]), uri, toy)
    load_partitions(conn, uri, uris)
    assert [r[2] for r in _rows(conn, "toy_values", "hour")] == [1.0, 2.0]

    with conn.cursor() as cur:
        cur.execute("insert into toy_values values ('stray', %s, 9.0)", (T0,))
    conn.commit()
    rebuild(conn, uri)
    assert [r[0] for r in _rows(conn, "toy_values", "hour")] == ["s1", "s1"]


# --- Load ---------------------------------------------------------------------


# @spec OBS-PG-004
def test_load_partitions_upserts_each_partition_in_registry_order(conn, archive):
    uri, sites, observations = archive
    # observation partitions listed first: the FK still holds because sites load first
    load_partitions(conn, uri, [*observations, *sites])
    assert len(_rows(conn, "sites", "site_id")) == 2
    assert len(_rows(conn, "observations", "site_id, observed_at")) == 3

    with conn.cursor() as cur:
        cur.execute("select qc_flags, raw from observations where observed_at = %s", (T0,))
        rows = {tuple(flags): raw for flags, raw in cur.fetchall()}
    assert ("flatline",) in rows
    assert rows[("flatline",)]["sensor_index"] == 12345


# @spec OBS-PG-004
def test_each_partition_is_its_own_transaction(conn, archive):
    uri, sites, observations = archive
    missing = f"{uri}/source=purpleair/date=2030-01-01/observations.parquet"
    with pytest.raises(Exception):
        load_partitions(conn, uri, [*sites, observations[0], missing])
    conn.rollback()
    assert len(_rows(conn, "sites", "site_id")) == 2
    assert len(_rows(conn, "observations", "site_id")) >= 1


# @spec OBS-PG-004
def test_load_partitions_updates_existing_rows(conn, archive):
    uri, sites, observations = archive
    load_partitions(conn, uri, [*sites, *observations])
    revised = make_observation(site_id="purpleair:1", observed_at=T0, pm25_raw=42.0)
    revised_partitions = write_observations([revised], uri)
    load_partitions(conn, uri, revised_partitions)
    with conn.cursor() as cur:
        cur.execute(
            "select pm25_raw, qc_flags from observations where site_id = %s and observed_at = %s",
            ("purpleair:1", T0),
        )
        assert cur.fetchone() == (42.0, [])


# @spec OBS-PG-005
def test_rebuild_makes_the_database_equal_the_archive(conn, archive):
    uri, sites, observations = archive
    load_partitions(conn, uri, [*sites, *observations])
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into sites values ('purpleair:stray','purpleair','stray','low_cost_sensor',
                                      null, 0, 0, ST_SetSRID(ST_MakePoint(0, 0), 4326))
            """
        )
    conn.commit()
    rebuild(conn, uri)
    assert [r[0] for r in _rows(conn, "sites", "site_id")] == [
        "airnow:840110010043",
        "purpleair:1",
    ]
    assert len(_rows(conn, "observations", "site_id")) == 3


# @spec OBS-PG-007
def test_geom_is_populated_as_wgs84_points(conn, archive):
    uri, sites, observations = archive
    load_partitions(conn, uri, [*sites, *observations])
    with conn.cursor() as cur:
        for table in ("sites", "observations"):
            cur.execute(
                f"select count(*) from {table} where ST_SRID(geom) <> 4326 "
                "or ST_X(geom) <> longitude or ST_Y(geom) <> latitude"
            )
            assert cur.fetchone()[0] == 0


# @spec OBS-PG-008
def test_reloading_unchanged_partitions_changes_nothing(conn, archive):
    uri, sites, observations = archive
    load_partitions(conn, uri, [*sites, *observations])
    first = (_rows(conn, "sites", "site_id"), _rows(conn, "observations", "site_id, observed_at"))
    load_partitions(conn, uri, [*sites, *observations])
    second = (_rows(conn, "sites", "site_id"), _rows(conn, "observations", "site_id, observed_at"))
    assert first == second


# @spec OBS-PG-011
def test_connect_uses_database_url(monkeypatch):
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is unset; PostGIS tests need a database")
    with postgis.connect() as connection:
        with connection.cursor() as cur:
            cur.execute("select 1")
            assert cur.fetchone() == (1,)
    monkeypatch.setenv("DATABASE_URL", "postgresql://nobody:nothing@127.0.0.1:1/nowhere")
    with pytest.raises(Exception):
        postgis.connect()


# --- Structure ----------------------------------------------------------------

SRC = Path(__file__).resolve().parents[2] / "src" / "aqdt"


# @spec OBS-PG-006
# @spec OBS-PG-009
def test_only_the_postgis_module_talks_to_the_database():
    offenders = [
        p
        for p in SRC.rglob("*.py")
        if p.name != "postgis.py"
        and ("psycopg" in p.read_text() or "insert into" in p.read_text().lower())
    ]
    assert offenders == []


# @spec OBS-PG-010
def test_explicit_sql_with_psycopg_and_no_orm():
    source = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    assert "psycopg" in source
    assert "sqlalchemy" not in source.lower()
    assert "to_postgis" not in source
