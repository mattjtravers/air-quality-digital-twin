"""The hourly surface raster — FUS-RASTER-001..005."""

import math
import subprocess
import sys
from datetime import UTC, datetime

import numpy as np
import pytest

from aqdt.fusion.grid import grid_for
from aqdt.fusion.raster import encode_surface, surface_key
from aqdt.fusion.schemas import SurfaceFit, SurfaceStatus
from aqdt.observation_store.schemas import DC_METRO

from .conftest import TEST_RESOLUTION_M

HOUR = datetime(2026, 9, 20, 10, tzinfo=UTC)


@pytest.fixture
def grid():
    return grid_for(DC_METRO, TEST_RESOLUTION_M)


@pytest.fixture
def surface(grid):
    rng = np.random.default_rng(7)
    estimate = rng.uniform(5, 20, (grid.height, grid.width)).astype(np.float32)
    variance = rng.uniform(0, 4, (grid.height, grid.width)).astype(np.float32)
    return estimate, variance


@pytest.fixture
def fit(grid):
    return SurfaceFit(
        hour=HOUR,
        status=SurfaceStatus.kriged,
        n_sensors=63,
        n_points=62,
        variogram_model="spherical",
        sill=9.5,
        range_m=21_000.0,
        nugget=0.25,
        q1=0.01,
        q2=1.02,
        cr=3.3,
        resolution_m=float(TEST_RESOLUTION_M),
        width=grid.width,
        height=grid.height,
        surface_uri="file:///archive/" + surface_key(HOUR),
        surface_sha256="0" * 64,
    )


def _open(tmp_path, data: bytes):
    import rasterio

    path = tmp_path / "surface.tif"
    path.write_bytes(data)
    return rasterio.open(path)


# @spec FUS-RASTER-001
def test_raster_key_is_partitioned_by_utc_date_and_named_by_hour():
    assert surface_key(HOUR) == "fusion/surfaces/date=2026-09-20/pm25_2026-09-20T10.tif"
    late = datetime(2026, 9, 20, 23, tzinfo=UTC)
    assert surface_key(late) == "fusion/surfaces/date=2026-09-20/pm25_2026-09-20T23.tif"
    from datetime import timedelta, timezone

    eastern = datetime(2026, 9, 20, 20, tzinfo=timezone(timedelta(hours=-4)))  # 00:00 UTC next day
    assert surface_key(eastern) == "fusion/surfaces/date=2026-09-21/pm25_2026-09-21T00.tif"


# @spec FUS-RASTER-002
def test_raster_is_a_two_band_float32_cog_on_the_grid(tmp_path, grid, surface, fit):
    estimate, variance = surface
    with _open(tmp_path, encode_surface(estimate, variance, grid, fit)) as src:
        assert src.tags(ns="IMAGE_STRUCTURE").get("LAYOUT") == "COG"
        assert src.count == 2
        assert src.dtypes == ("float32", "float32")
        assert src.descriptions == ("pm25", "pm25_variance")
        assert src.units == ("µg/m³", "(µg/m³)²")
        assert src.crs.to_epsg() == 26918
        assert (src.width, src.height) == (grid.width, grid.height)
        t = src.transform
        assert (t.c, t.a, t.b, t.f, t.d, t.e) == (
            grid.x_min,
            TEST_RESOLUTION_M,
            0,
            grid.y_max,
            0,
            -TEST_RESOLUTION_M,
        )
        assert math.isnan(src.nodata)
        assert src.compression.name.lower() == "deflate"
        assert src.block_shapes[0] == (512, 512)
        np.testing.assert_array_equal(src.read(1), estimate)
        np.testing.assert_array_equal(src.read(2), variance)


# @spec FUS-RASTER-003
def test_raster_tags_describe_the_fit(tmp_path, grid, surface, fit):
    with _open(tmp_path, encode_surface(*surface, grid, fit)) as src:
        tags = src.tags()
    assert tags["hour"] == "2026-09-20T10:00:00Z"
    assert tags["variogram_model"] == "spherical"
    assert float(tags["sill"]) == fit.sill
    assert float(tags["range_m"]) == fit.range_m
    assert float(tags["nugget"]) == fit.nugget
    assert int(tags["n_points"]) == fit.n_points


# @spec FUS-RASTER-004
def test_encoding_is_deterministic_and_timestamp_free(tmp_path, grid, surface, fit):
    first = encode_surface(*surface, grid, fit)
    second = encode_surface(*surface, grid, fit)
    assert first == second
    with _open(tmp_path, first) as src:
        tags = src.tags()
    assert "TIFFTAG_DATETIME" not in tags
    # AREA_OR_POINT is GDAL's report of the GeoTIFF raster-type key, fixed at "Area"
    assert tags.pop("AREA_OR_POINT", "Area") == "Area"
    assert set(tags) == {"hour", "variogram_model", "sill", "range_m", "nugget", "n_points"}


# @spec FUS-RASTER-005
def test_raster_opens_with_rasterio_and_no_project_code(tmp_path, grid, surface, fit):
    path = tmp_path / "surface.tif"
    path.write_bytes(encode_surface(*surface, grid, fit))
    script = (
        "import rasterio, sys\n"
        "src = rasterio.open(sys.argv[1])\n"
        "assert 'aqdt' not in sys.modules\n"
        "print(src.tags(ns='IMAGE_STRUCTURE')['LAYOUT'], src.count, src.crs.to_epsg(),"
        " src.tags()['variogram_model'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(path)], capture_output=True, text=True, check=True
    )
    assert result.stdout.split() == ["COG", "2", "26918", "spherical"]
