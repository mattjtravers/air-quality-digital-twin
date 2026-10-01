"""The surface_fits product, its PostGIS table, and settings — FUS-STORE, FUS-CFG."""

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from pandera.errors import SchemaError, SchemaErrors
from pydantic import ValidationError

from aqdt import registry
from aqdt.calibration.products import CALIBRATED_HOURLY, FITS, SENSOR_HOURLY
from aqdt.fusion import frames, schemas
from aqdt.fusion.products import SURFACE_FITS
from aqdt.fusion.run import krige_surfaces
from aqdt.fusion.schemas import DEFAULT_RESOLUTION_M, FusionSettings
from aqdt.observation_store.archive import read_partitioned
from aqdt.observation_store.products import Product
from aqdt.observation_store.schemas import DC_METRO
from tests.observation_store.test_frames import _args, _compatible

from .conftest import H10, H14, H, build_archive

FrameError = (SchemaError, SchemaErrors)
PACKAGE = Path(__file__).resolve().parents[2] / "src" / "aqdt" / "fusion"
FIELDS = {
    "hour",
    "status",
    "n_sensors",
    "n_points",
    "variogram_model",
    "sill",
    "range_m",
    "nugget",
    "q1",
    "q2",
    "cr",
    "resolution_m",
    "width",
    "height",
    "surface_uri",
    "surface_sha256",
}


# @spec FUS-STORE-001
def test_record_model_has_the_product_fields():
    assert set(schemas.SurfaceFit.model_fields) == FIELDS
    assert {s.value for s in schemas.SurfaceStatus} == {
        "kriged",
        "insufficient_points",
        "fit_failed",
    }
    with pytest.raises(ValidationError):
        schemas.SurfaceFit(
            hour=datetime(2026, 9, 20, 10),  # naive
            status=schemas.SurfaceStatus.insufficient_points,
            n_sensors=0,
            n_points=0,
            **{name: None for name in FIELDS - {"hour", "status", "n_sensors", "n_points"}},
        )


# @spec FUS-STORE-002
def test_record_and_frame_models_conform():
    columns = frames.SurfaceFitFrame.to_schema().columns
    assert set(columns) == set(schemas.SurfaceFit.model_fields)
    for name, field in schemas.SurfaceFit.model_fields.items():
        assert columns[name].nullable == (type(None) in _args(field.annotation)), name
        assert _compatible(field.annotation, str(columns[name].dtype)), (name, columns[name].dtype)


# @spec FUS-STORE-003
def test_frame_rejects_duplicate_hours(archive_uri, settings):
    frame = krige_surfaces(archive_uri, H10, H10 + 2 * H, settings)
    frames.SurfaceFitFrame.validate(frame)
    with pytest.raises(FrameError):
        frames.SurfaceFitFrame.validate(pd.concat([frame, frame.iloc[[0]]], ignore_index=True))


# @spec FUS-STORE-004
def test_product_is_laid_out_under_fusion(archive_uri, settings):
    assert isinstance(SURFACE_FITS, Product)
    assert SURFACE_FITS.prefix == "fusion/surface_fits"
    assert SURFACE_FITS.key_cols == ["hour"]
    assert list(SURFACE_FITS.partition_keys) == ["date"]
    assert SURFACE_FITS.filename == "surface_fits.parquet"
    assert SURFACE_FITS.table == "surface_fits"
    assert SURFACE_FITS.frame_model is frames.SurfaceFitFrame
    krige_surfaces(archive_uri, H10, H10 + H, settings)
    stored = read_partitioned(archive_uri, SURFACE_FITS, date="2026-09-20")
    assert list(stored["hour"]) == [H10]


# @spec FUS-STORE-005
def test_sql_defines_the_surface_fits_table():
    files = sorted((PACKAGE / "sql").glob("*.sql"))
    assert files, "no SQL files under src/aqdt/fusion/sql/"
    sql = "\n".join(p.read_text().lower() for p in files)
    assert "create table if not exists surface_fits" in sql
    assert "primary key (hour)" in sql
    for name in FIELDS:
        assert f"\n  {name} " in sql, name
    assert SURFACE_FITS.sql_dir == PACKAGE / "sql"


# @spec FUS-STORE-006
def test_product_is_registered_after_calibration():
    order = registry.PRODUCTS
    assert SURFACE_FITS in order
    for product in (SENSOR_HOURLY, FITS, CALIBRATED_HOURLY):
        assert order.index(SURFACE_FITS) > order.index(product)
    source = "\n".join(p.read_text() for p in PACKAGE.rglob("*.py"))
    assert "psycopg" not in source and "insert into" not in source.lower()


# @spec FUS-STORE-005
# @spec FUS-STORE-006
def test_postgis_creates_loads_and_rebuilds_surface_fits(tmp_path, settings):
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is unset; PostGIS tests need a database")
    import psycopg

    archive_uri = build_archive(str(tmp_path / "archive"), orphan=False)

    from aqdt.observation_store.postgis import apply_schema, rebuild

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            for product in reversed(registry.PRODUCTS):
                cur.execute(f"drop table if exists {product.table} cascade")
        conn.commit()
        apply_schema(conn)
        rebuild(conn, archive_uri)
        frame = krige_surfaces(archive_uri, H10, H14 + H, settings, conn=conn)
        with conn.cursor() as cur:
            cur.execute("select count(*), count(surface_uri) from surface_fits")
            assert cur.fetchone() == (len(frame), int((frame["status"] == "kriged").sum()))
        rebuild(conn, archive_uri)
        with conn.cursor() as cur:
            cur.execute("select count(*) from surface_fits")
            assert cur.fetchone()[0] == len(frame)


# --- Settings -----------------------------------------------------------------


# @spec FUS-CFG-001
def test_settings_defaults_and_environment(monkeypatch):
    for name in ("AQDT_FUS_RESOLUTION_M", "AQDT_FUS_MIN_POINTS", "AQDT_BBOX"):
        monkeypatch.delenv(name, raising=False)
    s = FusionSettings()
    assert (s.resolution_m, s.min_points, s.bbox) == (DEFAULT_RESOLUTION_M, 10, DC_METRO)
    assert DEFAULT_RESOLUTION_M > 0
    monkeypatch.setenv("AQDT_FUS_RESOLUTION_M", "250")
    monkeypatch.setenv("AQDT_FUS_MIN_POINTS", "20")
    s = FusionSettings()
    assert (s.resolution_m, s.min_points) == (250, 20)


# @spec FUS-CFG-002
@pytest.mark.parametrize(
    "overrides, name",
    [
        ({"resolution_m": 0}, "resolution_m"),
        ({"resolution_m": -100}, "resolution_m"),
        ({"min_points": 2}, "min_points"),
        ({"min_points": 0}, "min_points"),
    ],
)
def test_invalid_settings_fail_naming_the_setting(overrides, name):
    with pytest.raises(ValidationError, match=name):
        FusionSettings(**overrides)
    assert FusionSettings(min_points=3).min_points == 3


# @spec FUS-CFG-003
def test_tests_need_neither_network_nor_database_except_the_postgis_test():
    here = Path(__file__).parent
    source = "\n".join(p.read_text() for p in here.glob("test_*.py") if p.name != "test_store.py")
    assert "import httpx" not in source and "import psycopg" not in source
    assert "DATABASE_URL" not in source
    conftest = (here / "conftest.py").read_text()
    assert "write_partitioned" in conftest and "tmp_path" in conftest
    s3 = Path(__file__).resolve().parents[1] / "observation_store" / "test_replace_object.py"
    assert "s3_archive_uri" in s3.read_text()
