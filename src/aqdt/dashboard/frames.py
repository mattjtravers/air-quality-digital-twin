"""A surface array as a Web Mercator image: reprojection, bounds, and PNG encoding."""

from __future__ import annotations

import io

import numpy as np
from PIL import Image
from pyproj import Transformer
from rasterio.transform import Affine, array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject

from aqdt.fusion.grid import CRS

WEB_MERCATOR = 3857
_TO_LONLAT = Transformer.from_crs(WEB_MERCATOR, 4326, always_xy=True)


# @spec DASH-FRAME-001
def to_web_mercator(array: np.ndarray, transform: Affine) -> tuple[np.ndarray, Affine]:
    """``array`` on the grid's EPSG:26918 ``transform``, reprojected cell for cell (nearest
    neighbour) to EPSG:3857; cells outside the source grid are NaN."""
    height, width = array.shape
    bounds = array_bounds(height, width, transform)
    dst_transform, dst_width, dst_height = calculate_default_transform(
        CRS, WEB_MERCATOR, width, height, *bounds
    )
    dst = np.full((dst_height, dst_width), np.nan, dtype=np.float32)
    reproject(
        np.asarray(array, dtype=np.float32),
        dst,
        src_transform=transform,
        src_crs=CRS,
        src_nodata=np.nan,
        dst_transform=dst_transform,
        dst_crs=WEB_MERCATOR,
        dst_nodata=np.nan,
        resampling=Resampling.nearest,
    )
    return dst, dst_transform


# @spec DASH-FRAME-004
def bounds_latlon(shape: tuple[int, int], transform: Affine) -> list[list[float]]:
    """The image's south-west and north-east corners in WGS84, ``[[S, W], [N, E]]``: the two
    corners Leaflet stretches an overlay between in Web Mercator."""
    west, south, east, north = array_bounds(*shape, transform)
    (w, e), (s, n) = _TO_LONLAT.transform([west, east], [south, north])
    return [[round(s, 5), round(w, 5)], [round(n, 5), round(e, 5)]]


# @spec DASH-FRAME-003
def encode_png(rgba: np.ndarray) -> bytes:
    """RGBA pixels as a PNG with no text, time, or other metadata chunks, so identical pixels
    always give identical bytes."""
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgba, dtype=np.uint8), "RGBA").save(
        buffer, format="PNG", compress_level=9
    )
    return buffer.getvalue()
