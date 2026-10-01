"""The surface_fits product, laid out under ``fusion/`` in the archive."""

from __future__ import annotations

from pathlib import Path

from aqdt.fusion.frames import SurfaceFitFrame
from aqdt.observation_store.products import Product

SQL_DIR = Path(__file__).parent / "sql"

# @spec FUS-STORE-004, FUS-STORE-005
SURFACE_FITS = Product(
    prefix="fusion/surface_fits",
    partition_keys={"date": lambda frame: frame["hour"].dt.tz_convert("UTC").dt.date},
    key_cols=["hour"],
    frame_model=SurfaceFitFrame,
    filename="surface_fits.parquet",
    table="surface_fits",
    sql_dir=SQL_DIR,
)
