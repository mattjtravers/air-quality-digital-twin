"""The prediction grid: a fixed EPSG:26918 raster over the project's bounding box."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from pyproj import Transformer

from aqdt.observation_store.schemas import BoundingBox

#: UTM zone 18N, metres — the projection kriging and the grid work in.
CRS = 26918

_TO_UTM = Transformer.from_crs(4326, CRS, always_xy=True)


@dataclass(frozen=True)
class Grid:
    """``width`` × ``height`` cells of ``resolution_m`` metres, north-west corner at
    ``(x_min, y_max)``; cells are numbered east along each row, rows southward from the north."""

    x_min: float
    y_max: float
    width: int
    height: int
    resolution_m: float

    @property
    def n_cells(self) -> int:
        return self.width * self.height

    @property
    def transform(self) -> tuple[float, float, float, float, float, float]:
        """GDAL geotransform order: ``(x_min, res, 0, y_max, 0, -res)``."""
        return (self.x_min, self.resolution_m, 0.0, self.y_max, 0.0, -self.resolution_m)

    # @spec FUS-GRID-002
    def cell_centres(
        self, start: int = 0, stop: int | None = None
    ) -> tuple[np.ndarray, np.ndarray]:
        """Centres of cells ``start`` to ``stop`` (row-major), as EPSG:26918 x and y arrays."""
        index = np.arange(start, self.n_cells if stop is None else stop)
        columns, rows = index % self.width, index // self.width
        xs = self.x_min + (columns + 0.5) * self.resolution_m
        ys = self.y_max - (rows + 0.5) * self.resolution_m
        return xs, ys


# @spec FUS-GRID-001, FUS-GRID-003
def grid_for(bbox: BoundingBox, resolution_m: float) -> Grid:
    """The grid covering ``bbox``'s projected envelope, edges snapped to the resolution so that
    every grid at the same resolution aligns cell for cell."""
    corners = [
        (bbox.nwlng, bbox.nwlat),
        (bbox.selng, bbox.nwlat),
        (bbox.nwlng, bbox.selat),
        (bbox.selng, bbox.selat),
    ]
    xs, ys = zip(*(_TO_UTM.transform(lon, lat) for lon, lat in corners))
    min_x, min_y, max_x, max_y = min(xs), min(ys), max(xs), max(ys)
    x_min = math.floor(min_x / resolution_m) * resolution_m
    y_max = math.ceil(max_y / resolution_m) * resolution_m
    return Grid(
        x_min=x_min,
        y_max=y_max,
        width=math.ceil((max_x - x_min) / resolution_m),
        height=math.ceil((y_max - min_y) / resolution_m),
        resolution_m=resolution_m,
    )
