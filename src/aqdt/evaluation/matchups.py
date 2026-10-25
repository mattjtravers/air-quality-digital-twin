"""Sampling a surface raster at a monitor: the grid cell that contains it."""

from __future__ import annotations

import math

from rasterio.transform import Affine


# @spec EVAL-MATCH-002
def cell_index(
    transform: Affine, width: int, height: int, x: float, y: float
) -> tuple[int, int] | None:
    """``(row, column)`` of the cell containing ``(x, y)``, or ``None`` outside the grid.

    Column ``i`` covers ``[x_min + i·res, x_min + (i+1)·res)`` and row ``j`` covers
    ``(y_max − (j+1)·res, y_max − j·res]``, so a point on the grid's east or south edge is
    outside it.
    """
    column_f, row_f = ~transform @ (x, y)
    column, row = math.floor(column_f), math.floor(row_f)
    if not (0 <= column < width and 0 <= row < height):
        return None
    return row, column
