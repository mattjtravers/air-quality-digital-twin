"""The three calibration products, laid out under ``calibration/`` in the archive."""

from __future__ import annotations

from pathlib import Path

from aqdt.calibration.frames import CalibratedHourlyFrame, CalibrationFitFrame, SensorHourlyFrame
from aqdt.observation_store.products import Product

SQL_DIR = Path(__file__).parent / "sql"


def _utc_date(column: str):
    return lambda frame: frame[column].dt.tz_convert("UTC").dt.date


# @spec CAL-STORE-004, CAL-STORE-006
SENSOR_HOURLY = Product(
    prefix="calibration/sensor_hourly",
    partition_keys={"date": _utc_date("hour")},
    key_cols=["site_id", "hour"],
    frame_model=SensorHourlyFrame,
    filename="sensor_hourly.parquet",
    table="sensor_hourly",
    sql_dir=SQL_DIR,
)

FITS = Product(
    prefix="calibration/fits",
    partition_keys={"as_of": lambda frame: frame["as_of"]},
    key_cols=["site_id", "as_of"],
    frame_model=CalibrationFitFrame,
    filename="fits.parquet",
    table="calibration_fits",
    sql_dir=SQL_DIR,
)

CALIBRATED_HOURLY = Product(
    prefix="calibration/calibrated_hourly",
    partition_keys={"date": _utc_date("hour")},
    key_cols=["site_id", "hour"],
    frame_model=CalibratedHourlyFrame,
    filename="calibrated_hourly.parquet",
    table="calibrated_hourly",
    sql_dir=SQL_DIR,
)
