"""Product schemas, frames, archive products, PostGIS tables, settings — CAL-STORE, CAL-CFG."""

import os
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest
from pandera.errors import SchemaError, SchemaErrors
from pydantic import ValidationError

from aqdt import registry
from aqdt.calibration import frames, schemas
from aqdt.calibration.fit import fit_calibrations
from aqdt.calibration.products import CALIBRATED_HOURLY, FITS, SENSOR_HOURLY
from aqdt.calibration.schemas import CalibrationSettings
from aqdt.observation_store.products import OBSERVATIONS, SITES, Product
from tests.observation_store.test_frames import _args, _compatible

from .conftest import AS_OF

FrameError = (SchemaError, SchemaErrors)
PACKAGE = Path(__file__).resolve().parents[2] / "src" / "aqdt" / "calibration"
PAIRS = [
    (schemas.SensorHourly, frames.SensorHourlyFrame),
    (schemas.CalibrationFit, frames.CalibrationFitFrame),
    (schemas.CalibratedHourly, frames.CalibratedHourlyFrame),
]


# @spec CAL-STORE-001
def test_record_models_have_the_product_fields():
    assert set(schemas.SensorHourly.model_fields) == {
        "site_id",
        "hour",
        "pm25_corrected_mean",
        "humidity_mean",
        "n_snapshots",
        "n_flagged",
    }
    assert set(schemas.CalibrationFit.model_fields) == {
        "site_id",
        "as_of",
        "status",
        "ref_site_id",
        "distance_m",
        "window_start",
        "window_end",
        "n_pairs",
        "slope",
        "intercept",
        "r2",
        "rmse",
    }
    assert set(schemas.CalibratedHourly.model_fields) == {
        "site_id",
        "hour",
        "pm25_calibrated",
        "pm25_corrected_mean",
        "n_snapshots",
        "fit_as_of",
        "fit_status",
    }
    assert {s.value for s in schemas.FitStatus} == {"fitted", "pooled", "uncalibrated"}
    with pytest.raises(ValidationError):
        schemas.SensorHourly(
            site_id="purpleair:1",
            hour=datetime(2026, 9, 20),  # naive
            pm25_corrected_mean=1.0,
            humidity_mean=50.0,
            n_snapshots=1,
            n_flagged=0,
        )


# @spec CAL-STORE-002
@pytest.mark.parametrize("record, frame_model", PAIRS)
def test_record_and_frame_models_conform(record, frame_model):
    columns = frame_model.to_schema().columns
    assert set(columns) == set(record.model_fields)
    for name, field in record.model_fields.items():
        assert columns[name].nullable == (type(None) in _args(field.annotation)), name
        assert _compatible(field.annotation, str(columns[name].dtype)), (name, columns[name].dtype)


# @spec CAL-STORE-003
def test_frames_reject_duplicate_keys(archive_uri, settings):
    fit_calibrations(archive_uri, AS_OF, settings)
    from aqdt.observation_store.archive import read_partitioned

    for product in (SENSOR_HOURLY, FITS, CALIBRATED_HOURLY):
        frame = read_partitioned(archive_uri, product)
        if len(frame) == 0:
            continue
        product.frame_model.validate(frame)
        with pytest.raises(FrameError):
            product.frame_model.validate(pd.concat([frame, frame.iloc[[0]]], ignore_index=True))


# @spec CAL-STORE-004
def test_products_are_laid_out_under_calibration(archive_uri, settings):
    assert isinstance(SENSOR_HOURLY, Product) and isinstance(FITS, Product)
    assert isinstance(CALIBRATED_HOURLY, Product)
    assert SENSOR_HOURLY.prefix == "calibration/sensor_hourly"
    assert FITS.prefix == "calibration/fits"
    assert CALIBRATED_HOURLY.prefix == "calibration/calibrated_hourly"
    assert SENSOR_HOURLY.key_cols == ["site_id", "hour"]
    assert FITS.key_cols == ["site_id", "as_of"]
    assert CALIBRATED_HOURLY.key_cols == ["site_id", "hour"]
    assert list(SENSOR_HOURLY.partition_keys) == ["date"]
    assert list(FITS.partition_keys) == ["as_of"]
    assert list(CALIBRATED_HOURLY.partition_keys) == ["date"]

    fit_calibrations(archive_uri, AS_OF, settings)
    root = Path(archive_uri)
    assert (root / "calibration/fits/as_of=2026-09-20T00/fits.parquet").exists()
    assert (root / "calibration/sensor_hourly/date=2026-09-19/sensor_hourly.parquet").exists()


# @spec CAL-STORE-005
def test_products_are_read_back_through_read_partitioned(archive_uri, settings):
    from aqdt.observation_store.archive import read_partitioned

    written = fit_calibrations(archive_uri, AS_OF, settings)
    stored = read_partitioned(archive_uri, FITS, as_of="2026-09-20T00")
    assert len(stored) == len(written)
    assert set(stored["site_id"]) == {f.site_id for f in written}


# @spec CAL-STORE-006
def test_sql_files_define_the_three_tables_referencing_sites():
    sql_dir = PACKAGE / "sql"
    files = sorted(sql_dir.glob("*.sql"))
    assert files, "no SQL files under src/aqdt/calibration/sql/"
    sql = "\n".join(p.read_text().lower() for p in files)
    for table, key in (
        ("sensor_hourly", "(site_id, hour)"),
        ("calibration_fits", "(site_id, as_of)"),
        ("calibrated_hourly", "(site_id, hour)"),
    ):
        assert f"create table if not exists {table}" in sql
        assert f"primary key {key}" in sql
    assert sql.count("references sites") == 3
    assert {SENSOR_HOURLY.table, FITS.table, CALIBRATED_HOURLY.table} == {
        "sensor_hourly",
        "calibration_fits",
        "calibrated_hourly",
    }
    assert all(p.sql_dir == sql_dir for p in (SENSOR_HOURLY, FITS, CALIBRATED_HOURLY))


# @spec CAL-STORE-007
def test_products_are_registered_after_sites_and_observations():
    order = registry.PRODUCTS
    assert order.index(SITES) < order.index(OBSERVATIONS)
    for product in (SENSOR_HOURLY, FITS, CALIBRATED_HOURLY):
        assert order.index(product) > order.index(OBSERVATIONS)
    source = "\n".join(p.read_text() for p in PACKAGE.rglob("*.py"))
    assert "psycopg" not in source and "insert into" not in source.lower()


# @spec CAL-STORE-006
# @spec CAL-STORE-007
def test_postgis_creates_and_loads_calibration_tables(archive_uri, settings):
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL is unset; PostGIS tests need a database")
    import psycopg

    from aqdt.observation_store.postgis import apply_schema, load_partitions, rebuild

    with psycopg.connect(url) as conn:
        with conn.cursor() as cur:
            for product in reversed(registry.PRODUCTS):
                cur.execute(f"drop table if exists {product.table} cascade")
        conn.commit()
        apply_schema(conn)
        rebuild(conn, archive_uri)  # sites and observations, as the ingesters would have loaded
        fits = fit_calibrations(archive_uri, AS_OF, settings, conn=conn)
        with conn.cursor() as cur:
            cur.execute("select count(*) from calibration_fits")
            assert cur.fetchone()[0] == len(fits)
            cur.execute("select count(*) from sensor_hourly")
            assert cur.fetchone()[0] > 0
        rebuild(conn, archive_uri)
        with conn.cursor() as cur:
            cur.execute("select count(*) from calibration_fits")
            assert cur.fetchone()[0] == len(fits)
        assert load_partitions is not None


# --- Settings -----------------------------------------------------------------


# @spec CAL-CFG-001
def test_settings_defaults_and_environment(monkeypatch):
    for name in ("AQDT_CAL_MAX_DISTANCE_M", "AQDT_CAL_WINDOW_DAYS", "AQDT_CAL_MIN_PAIRS"):
        monkeypatch.delenv(name, raising=False)
    s = CalibrationSettings()
    assert (s.max_distance_m, s.window_days, s.min_pairs) == (10_000, 30, 72)
    monkeypatch.setenv("AQDT_CAL_MAX_DISTANCE_M", "2500")
    monkeypatch.setenv("AQDT_CAL_WINDOW_DAYS", "7")
    monkeypatch.setenv("AQDT_CAL_MIN_PAIRS", "24")
    s = CalibrationSettings()
    assert (s.max_distance_m, s.window_days, s.min_pairs) == (2500, 7, 24)
    assert CalibrationSettings(window_days=14).window_days == 14


# @spec CAL-CFG-002
@pytest.mark.parametrize("name", ["max_distance_m", "window_days", "min_pairs"])
def test_non_positive_settings_fail_naming_the_setting(name):
    with pytest.raises(ValidationError, match=name):
        CalibrationSettings(**{name: 0})
    with pytest.raises(ValidationError, match=name):
        CalibrationSettings(**{name: -1})


# @spec CAL-CFG-003
def test_tests_need_neither_network_nor_database_except_the_postgis_test():
    here = Path(__file__).parent
    source = "\n".join(p.read_text() for p in here.glob("test_*.py") if p.name != "test_store.py")
    assert "import httpx" not in source and "import psycopg" not in source
    assert "DATABASE_URL" not in source
    conftest = (here / "conftest.py").read_text()
    assert "write_observations" in conftest and "json" not in conftest  # synthetic, not recorded
