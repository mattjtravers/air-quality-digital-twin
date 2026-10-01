"""The hourly surface raster: a two-band Cloud-Optimized GeoTIFF (estimate, variance)."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import rasterio
import rasterio.shutil
from rasterio.io import MemoryFile
from rasterio.transform import Affine

from aqdt.fusion.grid import CRS, Grid
from aqdt.fusion.schemas import SurfaceFit

BANDS = (("pm25", "µg/m³"), ("pm25_variance", "(µg/m³)²"))


# @spec FUS-RASTER-001
def surface_key(hour: datetime) -> str:
    """The raster's key under the archive root, partitioned by the hour's UTC date."""
    h = hour.astimezone(UTC)
    return f"fusion/surfaces/date={h:%Y-%m-%d}/pm25_{h:%Y-%m-%dT%H}.tif"


def _tags(fit: SurfaceFit) -> dict[str, str]:
    return {
        "hour": fit.hour.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "variogram_model": str(fit.variogram_model),
        "sill": repr(fit.sill),
        "range_m": repr(fit.range_m),
        "nugget": repr(fit.nugget),
        "n_points": str(fit.n_points),
    }


# @spec FUS-RASTER-002, FUS-RASTER-003, FUS-RASTER-004, FUS-RASTER-005
def encode_surface(
    estimate: np.ndarray, variance: np.ndarray, grid: Grid, fit: SurfaceFit
) -> bytes:
    """The hour's raster as COG bytes: identical inputs always give identical bytes."""
    profile = {
        "driver": "GTiff",
        "width": grid.width,
        "height": grid.height,
        "count": 2,
        "dtype": "float32",
        "crs": f"EPSG:{CRS}",
        "transform": Affine.from_gdal(*grid.transform),
        "nodata": float("nan"),
    }
    with MemoryFile() as staging:
        with staging.open(**profile) as dataset:
            for band, (array, (description, unit)) in enumerate(
                zip((estimate, variance), BANDS, strict=True), start=1
            ):
                dataset.write(np.asarray(array, dtype=np.float32), band)
                dataset.set_band_description(band, description)
                dataset.set_band_unit(band, unit)
            dataset.update_tags(**_tags(fit))
        with staging.open() as source, MemoryFile() as output:
            rasterio.shutil.copy(
                source, output.name, driver="COG", COMPRESS="DEFLATE", BLOCKSIZE=512
            )
            return bytes(output.getbuffer())
