"""Per-hour variogram selection and chunked prediction — FUS-VAR, FUS-PRED."""

import math

import numpy as np
import pytest
from pyproj import Transformer

from aqdt.calibration.products import CALIBRATED_HOURLY
from aqdt.fusion import krige
from aqdt.fusion.grid import grid_for
from aqdt.fusion.krige import predict_chunked, select_variogram
from aqdt.fusion.points import HourPoints, hour_points
from aqdt.observation_store.archive import read_partitioned, read_sites
from aqdt.observation_store.schemas import DC_METRO

from .conftest import H10, H14, TEST_RESOLUTION_M, field

TO_WGS84 = Transformer.from_crs(26918, 4326, always_xy=True)


@pytest.fixture
def points(archive_uri) -> HourPoints:
    return hour_points(
        read_partitioned(archive_uri, CALIBRATED_HOURLY), read_sites(archive_uri), H10
    )


@pytest.fixture
def grid():
    return grid_for(DC_METRO, TEST_RESOLUTION_M)


class Recorder:
    """Wraps ``krige.OrdinaryKriging`` to record each construction's keyword arguments."""

    def __init__(self, real):
        self.real = real
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return self.real(*args, **kwargs)


class FakeKriging:
    """A stand-in whose cross-validation statistics are set per model."""

    cr_by_model: dict = {}

    def __init__(self, x, y, z, variogram_model, **kwargs):
        outcome = self.cr_by_model[variogram_model]
        if isinstance(outcome, Exception):
            raise outcome
        self.variogram_model = variogram_model
        self.variogram_model_parameters = [4.0, 20_000.0, 1.0]
        self.Q1, self.Q2, self.cR = 0.0, 1.0, outcome


def _fake(monkeypatch, cr_by_model):
    fake = type("Fake", (FakeKriging,), {"cr_by_model": cr_by_model})
    monkeypatch.setattr(krige, "OrdinaryKriging", fake)


# --- Variogram ------------------------------------------------------------------------------


# @spec FUS-VAR-001
def test_each_candidate_is_fitted_with_the_specified_options(points, monkeypatch):
    recorder = Recorder(krige.OrdinaryKriging)
    monkeypatch.setattr(krige, "OrdinaryKriging", recorder)
    select_variogram(points)
    models = [c["variogram_model"] for c in recorder.calls]
    assert sorted(models) == ["exponential", "spherical"]
    for call in recorder.calls:
        assert call.get("nlags", 6) == 6
        assert call.get("weight", False) is False
        assert call["enable_statistics"] is True
        assert call.get("coordinates_type", "euclidean") == "euclidean"


# @spec FUS-VAR-002
def test_smallest_cr_wins(points, monkeypatch):
    _fake(monkeypatch, {"spherical": 3.0, "exponential": 2.0})
    assert select_variogram(points).model == "exponential"
    _fake(monkeypatch, {"spherical": 1.0, "exponential": 2.0})
    assert select_variogram(points).model == "spherical"


# @spec FUS-VAR-002
def test_a_tie_goes_to_spherical(points, monkeypatch):
    _fake(monkeypatch, {"spherical": 2.0, "exponential": 2.0})
    assert select_variogram(points).model == "spherical"


# @spec FUS-VAR-003
@pytest.mark.parametrize(
    "outcomes, expected",
    [
        ({"spherical": ValueError("bounds"), "exponential": 5.0}, "exponential"),
        ({"spherical": float("nan"), "exponential": 5.0}, "exponential"),
        ({"spherical": 1.0, "exponential": float("inf")}, "spherical"),
        ({"spherical": ValueError("x"), "exponential": float("nan")}, None),
    ],
)
def test_failed_or_non_finite_candidates_are_not_eligible(points, monkeypatch, outcomes, expected):
    _fake(monkeypatch, outcomes)
    selected = select_variogram(points)
    assert (selected.model if selected else None) == expected


# @spec FUS-VAR-003
def test_identical_values_leave_no_eligible_candidate():
    rng = np.random.default_rng(1)
    flat = HourPoints(
        x=rng.uniform(300_000, 360_000, 15),
        y=rng.uniform(4_290_000, 4_330_000, 15),
        z=np.full(15, 7.0),
        n_sensors=15,
        n_points=15,
    )
    assert select_variogram(flat) is None


# @spec FUS-VAR-004
def test_selected_variogram_records_its_parameters_and_statistics(points):
    v = select_variogram(points)
    psill, range_m, nugget = v.kriger.variogram_model_parameters
    assert v.model in ("spherical", "exponential")
    assert v.sill == pytest.approx(psill + nugget)
    assert v.range_m == pytest.approx(range_m)
    assert v.nugget == pytest.approx(nugget)
    assert (v.q1, v.q2, v.cr) == (v.kriger.Q1, v.kriger.Q2, v.kriger.cR)
    assert all(math.isfinite(value) for value in (v.sill, v.range_m, v.nugget, v.q1, v.q2, v.cr))


# @spec FUS-VAR-005
def test_each_hour_is_fitted_from_its_own_points_alone(archive_uri, points):
    calibrated, sites = read_partitioned(archive_uri, CALIBRATED_HOURLY), read_sites(archive_uri)
    later = hour_points(calibrated, sites, H14)
    first = select_variogram(points)
    shifted = select_variogram(later)
    again = select_variogram(points)
    # a constant offset leaves the variogram unchanged: nothing leaks between hours
    assert (first.model, first.sill, first.range_m) == (again.model, again.sill, again.range_m)
    assert shifted.sill == pytest.approx(first.sill, rel=1e-6)

    rough = HourPoints(
        x=points.x,
        y=points.y,
        z=points.z * 3.0,
        n_sensors=points.n_sensors,
        n_points=points.n_points,
    )
    # a rougher hour gets its own, larger sill, and fitting the first hour afterwards is unchanged
    assert select_variogram(rough).sill > 4.0 * first.sill
    after = select_variogram(points)
    assert (after.model, after.sill, after.range_m) == (first.model, first.sill, first.range_m)


# --- Prediction -----------------------------------------------------------------------------


def _truth(grid):
    xs, ys = grid.cell_centres()
    lons, lats = TO_WGS84.transform(xs, ys)
    return (
        field(lons, lats).reshape(grid.height, grid.width),
        lons.reshape(grid.height, grid.width),
        lats.reshape(grid.height, grid.width),
    )


# @spec FUS-PRED-001
def test_kriged_surface_reproduces_the_known_field(points, grid):
    estimate, variance = predict_chunked(select_variogram(points), grid)
    assert estimate.shape == variance.shape == (grid.height, grid.width)
    truth, lons, lats = _truth(grid)
    interior = (lons > -77.40) & (lons < -76.80) & (lats > 38.78) & (lats < 39.02)
    rmse = float(np.sqrt(np.mean((estimate[interior] - truth[interior]) ** 2)))
    assert rmse < 1.0, f"interior RMSE {rmse:.3f} µg/m³ against a field spanning ~10 µg/m³"


# @spec FUS-PRED-001
def test_variance_grows_away_from_the_sensors(points, grid):
    _, variance = predict_chunked(select_variogram(points), grid)
    xs, ys = grid.cell_centres()
    nearest = np.min(np.hypot(xs[:, None] - points.x[None, :], ys[:, None] - points.y[None, :]), 1)
    nearest = nearest.reshape(grid.height, grid.width)
    near = variance[nearest < 1_000].mean()
    far = variance[nearest > 8_000].mean()
    assert near < far


# @spec FUS-PRED-002
@pytest.mark.parametrize("chunk_cells", [100, 257, 50_000])
def test_prediction_is_chunked_and_independent_of_chunk_size(points, grid, chunk_cells):
    variogram = select_variogram(points)
    sizes: list[int] = []
    real = variogram.kriger.execute

    def spy(style, xpoints, ypoints, *args, **kwargs):
        assert style == "points"
        sizes.append(len(xpoints))
        return real(style, xpoints, ypoints, *args, **kwargs)

    variogram.kriger.execute = spy
    estimate, variance = predict_chunked(variogram, grid, chunk_cells=chunk_cells)
    n_cells = grid.width * grid.height
    assert sum(sizes) == n_cells
    assert max(sizes) <= chunk_cells
    assert len(sizes) == math.ceil(n_cells / chunk_cells)

    variogram.kriger.execute = real
    whole_e, whole_v = predict_chunked(variogram, grid, chunk_cells=n_cells)
    np.testing.assert_allclose(estimate, whole_e, rtol=1e-6)
    np.testing.assert_allclose(variance, whole_v, rtol=1e-6, atol=1e-6)


# @spec FUS-PRED-002
def test_default_chunk_is_fifty_thousand_cells(points):
    variogram = select_variogram(points)
    sizes: list[int] = []
    real = variogram.kriger.execute

    def spy(style, xpoints, ypoints, *args, **kwargs):
        sizes.append(len(xpoints))
        return real(style, xpoints, ypoints, *args, **kwargs)

    variogram.kriger.execute = spy
    grid = grid_for(DC_METRO, 250)  # ~207 000 cells
    predict_chunked(variogram, grid)
    assert max(sizes) == 50_000 and len(sizes) == math.ceil(grid.width * grid.height / 50_000)


# @spec FUS-PRED-003
def test_estimate_and_variance_are_clamped_at_zero_and_float32(points, grid):
    variogram = select_variogram(points)

    def negative(style, xpoints, ypoints, *args, **kwargs):
        n = len(xpoints)
        values = np.where(np.arange(n) % 2 == 0, -3.0, 4.0)
        return np.ma.masked_array(values), np.ma.masked_array(values * 1e-9)

    variogram.kriger.execute = negative
    estimate, variance = predict_chunked(variogram, grid)
    assert estimate.dtype == np.float32 and variance.dtype == np.float32
    assert estimate.min() == 0.0 and estimate.max() == pytest.approx(4.0)
    assert variance.min() == 0.0
