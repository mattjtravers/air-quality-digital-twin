"""Kriging points and the prediction grid — FUS-IN-002..004, FUS-PTS, FUS-GRID."""

import math

import numpy as np
import pytest
from pyproj import Transformer

from aqdt.calibration.products import CALIBRATED_HOURLY
from aqdt.fusion.grid import grid_for
from aqdt.fusion.points import hour_points
from aqdt.observation_store.archive import read_partitioned, read_sites
from aqdt.observation_store.schemas import DC_METRO, BoundingBox

from .conftest import (
    COLOCATED,
    COLOCATED_AT,
    H10,
    H11,
    H13,
    MONITOR,
    OUTSIDE,
    POINTS_AT_10,
    USED_AT_10,
)

TO_UTM = Transformer.from_crs(4326, 26918, always_xy=True)


@pytest.fixture
def inputs(archive_uri):
    return read_partitioned(archive_uri, CALIBRATED_HOURLY), read_sites(archive_uri)


# --- Inputs and points ----------------------------------------------------------------------


# @spec FUS-IN-002
# @spec FUS-IN-004
# @spec FUS-PTS-003
def test_points_use_only_fitted_or_pooled_rows_with_values_and_sites(inputs):
    calibrated, sites = inputs
    points = hour_points(calibrated, sites, H10)
    assert points.n_sensors == USED_AT_10
    assert points.n_points == POINTS_AT_10
    assert len(points.x) == len(points.y) == len(points.z) == POINTS_AT_10
    assert 50.0 not in set(np.round(points.z, 6))  # the row with no site record

    assert hour_points(calibrated, sites, H11).n_sensors == 5
    empty = hour_points(calibrated, sites, H13)
    assert (empty.n_sensors, empty.n_points, len(empty.z)) == (0, 0, 0)


# @spec FUS-IN-003
def test_reference_monitors_are_never_points(inputs):
    calibrated, sites = inputs
    points = hour_points(calibrated, sites, H10)
    mx, my = TO_UTM.transform(MONITOR[2], MONITOR[1])
    assert not np.any((np.round(points.x) == round(mx)) & (np.round(points.y) == round(my)))
    assert points.z.max() < 100.0  # the monitor's 500 µg/m³ is not among them


# @spec FUS-PTS-001
def test_points_are_projected_to_utm_18n(inputs):
    calibrated, sites = inputs
    points = hour_points(calibrated, sites, H10)
    ox, oy = TO_UTM.transform(OUTSIDE[2], OUTSIDE[1])
    distances = np.hypot(points.x - ox, points.y - oy)
    assert distances.min() < 1.0


# @spec FUS-PTS-002
def test_co_located_sensors_merge_to_one_point_with_the_mean_value(inputs):
    calibrated, sites = inputs
    points = hour_points(calibrated, sites, H10)
    cx, cy = TO_UTM.transform(COLOCATED_AT[1], COLOCATED_AT[0])
    at = (points.x == round(cx)) & (points.y == round(cy))
    assert at.sum() == 1
    assert points.z[at][0] == pytest.approx(np.mean([v for _, v in COLOCATED]))


# @spec FUS-PTS-002
def test_sensors_metres_apart_stay_separate(inputs):
    import geopandas as gpd

    calibrated, sites = inputs
    sites = sites.copy()
    moved = sites["site_id"] == COLOCATED[1][0]
    sites.loc[moved, "longitude"] = sites.loc[moved, "longitude"] + 6e-5  # ~5 m east
    sites = gpd.GeoDataFrame(
        sites.drop(columns="geometry"),
        geometry=gpd.points_from_xy(sites["longitude"], sites["latitude"]),
        crs=4326,
    )
    points = hour_points(calibrated, sites, H10)
    assert points.n_points == POINTS_AT_10 + 1


# @spec FUS-PTS-004
def test_sensors_outside_the_grid_extent_are_kept(inputs):
    calibrated, sites = inputs
    points = hour_points(calibrated, sites, H10)
    _, y_north_edge = TO_UTM.transform(-77.05, DC_METRO.nwlat)
    assert (points.y > y_north_edge).sum() == 1


# --- Grid -------------------------------------------------------------------------------------


def _envelope(bbox: BoundingBox):
    corners = [
        (bbox.nwlng, bbox.nwlat),
        (bbox.selng, bbox.nwlat),
        (bbox.nwlng, bbox.selat),
        (bbox.selng, bbox.selat),
    ]
    xs, ys = zip(*(TO_UTM.transform(lon, lat) for lon, lat in corners))
    return min(xs), min(ys), max(xs), max(ys)


# @spec FUS-GRID-001
@pytest.mark.parametrize("resolution_m", [500, 200, 2000, 333])
def test_grid_envelope_snapped_to_the_resolution(resolution_m):
    grid = grid_for(DC_METRO, resolution_m)
    min_x, min_y, max_x, max_y = _envelope(DC_METRO)
    assert grid.x_min == math.floor(min_x / resolution_m) * resolution_m
    assert grid.y_max == math.ceil(max_y / resolution_m) * resolution_m
    assert grid.width == math.ceil((max_x - grid.x_min) / resolution_m)
    assert grid.height == math.ceil((grid.y_max - min_y) / resolution_m)
    assert grid.resolution_m == resolution_m
    # the grid covers the whole envelope
    assert grid.x_min <= min_x and grid.x_min + grid.width * resolution_m >= max_x
    assert grid.y_max >= max_y and grid.y_max - grid.height * resolution_m <= min_y


# @spec FUS-GRID-001
def test_dc_metro_cell_counts():
    assert grid_for(DC_METRO, 500).width * grid_for(DC_METRO, 500).height == pytest.approx(
        12_972, rel=0.02
    )
    assert grid_for(DC_METRO, 50).width * grid_for(DC_METRO, 50).height == pytest.approx(
        1_296_280, rel=0.02
    )


# @spec FUS-GRID-002
def test_cell_centres_count_east_then_south_from_the_north_west_corner():
    grid = grid_for(DC_METRO, 2000)
    xs, ys = grid.cell_centres()
    assert len(xs) == len(ys) == grid.width * grid.height
    for i, j in [(0, 0), (grid.width - 1, 0), (0, grid.height - 1), (5, 7)]:
        k = j * grid.width + i
        assert xs[k] == grid.x_min + (i + 0.5) * 2000
        assert ys[k] == grid.y_max - (j + 0.5) * 2000


# @spec FUS-GRID-002
def test_cell_centres_can_be_taken_in_slices():
    grid = grid_for(DC_METRO, 2000)
    xs, ys = grid.cell_centres()
    part_x, part_y = grid.cell_centres(start=100, stop=250)
    assert np.array_equal(part_x, xs[100:250]) and np.array_equal(part_y, ys[100:250])


# @spec FUS-GRID-003
def test_grid_follows_the_settings_bounding_box(monkeypatch):
    from aqdt.fusion.schemas import FusionSettings

    monkeypatch.delenv("AQDT_BBOX", raising=False)
    assert FusionSettings(resolution_m=500).bbox == DC_METRO
    monkeypatch.setenv("AQDT_BBOX", "-77.2,39.0,-76.9,38.8")
    settings = FusionSettings(resolution_m=500)
    assert settings.bbox == BoundingBox(nwlng=-77.2, nwlat=39.0, selng=-76.9, selat=38.8)
    small = grid_for(settings.bbox, 500)
    assert (
        small.width * small.height < grid_for(DC_METRO, 500).width * grid_for(DC_METRO, 500).height
    )
    assert grid_for(settings.bbox, 500) == small  # same settings, same grid
