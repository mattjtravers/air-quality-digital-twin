"""Reading a surface raster back: the two bands, the geotransform, and the tags."""

from __future__ import annotations

from dataclasses import dataclass

import fsspec
import numpy as np
from rasterio.io import MemoryFile
from rasterio.transform import Affine


@dataclass(frozen=True, eq=False)
class SurfaceArrays:
    pm25: np.ndarray  # band 1, µg/m³
    variance: np.ndarray  # band 2, (µg/m³)²
    transform: Affine
    tags: dict[str, str]
    data: bytes  # the raster's bytes, as stored


# @spec MAP-READ-001
def read_surface(uri: str) -> SurfaceArrays:
    """Open the surface raster at ``uri`` (a local path or an S3 URI)."""
    with fsspec.open(uri, "rb") as handle:
        data = handle.read()
    with MemoryFile(data) as memory, memory.open() as dataset:
        return SurfaceArrays(
            pm25=dataset.read(1),
            variance=dataset.read(2),
            transform=dataset.transform,
            tags=dataset.tags(),
            data=data,
        )
