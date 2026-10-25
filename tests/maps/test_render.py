"""The shared surface renderer — MAP-RENDER, SCALE, CLIP, MARK, MULTI, READ, SAVE, CFG."""

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pytest  # noqa: E402
from PIL import Image  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from aqdt.fusion.grid import grid_for  # noqa: E402
from aqdt.maps import render  # noqa: E402
from aqdt.maps.settings import PM25_MAX, SD_MAX, MapSettings  # noqa: E402
from aqdt.observation_store.schemas import DC_METRO  # noqa: E402

GRID = grid_for(DC_METRO, 4000)


@pytest.fixture
def settings(monkeypatch):
    monkeypatch.delenv("AQDT_MAP_PM25_MAX", raising=False)
    monkeypatch.delenv("AQDT_MAP_SD_MAX", raising=False)
    return MapSettings()


def _array(top=10.0):
    return np.linspace(0, top, GRID.n_cells, dtype=np.float32).reshape(GRID.height, GRID.width)


@pytest.fixture
def ax():
    fig, ax = plt.subplots(figsize=(3.3, 3.0))
    yield ax
    plt.close(fig)


# --- Rendering --------------------------------------------------------------------------------


# @spec MAP-RENDER-001, MAP-RENDER-002
def test_render_places_the_grid_in_kilometres_with_labelled_axes(ax, settings):
    array = _array()
    array[0, 0] = np.nan
    result = render.render_surface(ax, array, GRID, kind="pm25", settings=settings)
    image = ax.get_images()[0]
    left, right, bottom, top = image.get_extent()
    assert left == pytest.approx(GRID.x_min / 1000)
    assert right == pytest.approx((GRID.x_min + GRID.width * GRID.resolution_m) / 1000)
    assert top == pytest.approx(GRID.y_max / 1000)
    assert bottom == pytest.approx((GRID.y_max - GRID.height * GRID.resolution_m) / 1000)
    assert image.origin == "upper" and image.get_interpolation() == "nearest"
    assert ax.get_xlabel() == "Easting (km, EPSG:26918)" and ax.get_ylabel() == "Northing (km)"
    assert ax.get_aspect() == 1.0
    assert result.mappable is image


# @spec MAP-RENDER-003, MAP-CLIP-002
def test_render_result_reports_clipping(ax, settings):
    clean = render.render_surface(ax, _array(10.0), GRID, kind="pm25", settings=settings)
    assert not clean.clipped and clean.clipped_fraction == 0 and clean.max == pytest.approx(10.0)
    hot = _array(24.0)
    result = render.render_surface(ax, hot, GRID, kind="pm25", settings=settings)
    assert result.clipped
    assert result.clipped_fraction == pytest.approx(float((hot > PM25_MAX).mean()))
    cmap, norm = result.mappable.get_cmap(), result.mappable.norm
    assert np.allclose(cmap(norm(30.0)), cmap(1.0))


# --- Scales -----------------------------------------------------------------------------------


# @spec MAP-SCALE-001, MAP-SCALE-002, MAP-SCALE-003
@pytest.mark.parametrize(
    ("kind", "cmap", "top"), [("pm25", "viridis", PM25_MAX), ("sd", "magma", SD_MAX)]
)
def test_each_kind_has_a_fixed_uniform_scale(ax, settings, kind, cmap, top):
    for array in (_array(1.0), _array(50.0)):
        result = render.render_surface(ax, array, GRID, kind=kind, settings=settings)
        assert result.mappable.get_cmap().name == cmap
        assert (result.mappable.norm.vmin, result.mappable.norm.vmax) == (0.0, top)
        assert type(result.mappable.norm).__name__ == "Normalize"


# --- Colorbars --------------------------------------------------------------------------------


# @spec MAP-CLIP-001
@pytest.mark.parametrize(
    ("kind", "label"), [("pm25", "PM2.5 (µg/m³)"), ("sd", "Standard deviation (µg/m³)")]
)
def test_colorbar_label_and_arrow_only_when_clipped(ax, settings, kind, label):
    result = render.render_surface(ax, _array(), GRID, kind=kind, settings=settings)
    flat = render.add_colorbar(ax.figure, result.mappable, ax, kind=kind, clipped=False)
    arrow = render.add_colorbar(ax.figure, result.mappable, ax, kind=kind, clipped=True)
    assert flat.ax.get_ylabel() == label
    assert flat.extend == "neither" and arrow.extend == "max"


# --- Markers ----------------------------------------------------------------------------------


# @spec MAP-MARK-001, MAP-MARK-002
def test_sensor_and_monitor_markers(ax, settings):
    sensors = (np.array([330_000.0, 340_000.0]), np.array([4_310_000.0, 4_320_000.0]))
    monitors = (np.array([335_000.0]), np.array([4_315_000.0]))
    render.render_surface(
        ax, _array(), GRID, kind="pm25", sensors=sensors, monitors=monitors, settings=settings
    )
    by_label = {c.get_label(): c for c in ax.collections}
    s, m = by_label["PurpleAir sensors"], by_label["AirNow monitors"]
    assert np.allclose(s.get_facecolor()[0][:3], (1, 1, 1))
    assert np.allclose(s.get_edgecolor()[0][:3], (0, 0, 0))
    assert np.allclose(m.get_edgecolor()[0][:3], (1, 1, 1))
    face = m.get_facecolor()[0]
    assert face[0] > 0.8 and face[2] < 0.3  # red-orange
    assert m.get_sizes()[0] > s.get_sizes()[0]
    assert m.get_zorder() > s.get_zorder()
    np.testing.assert_allclose(s.get_offsets()[0], (330.0, 4310.0))


# --- Small multiples --------------------------------------------------------------------------


# @spec MAP-MULTI-001, MAP-MULTI-002
def test_small_multiples_share_one_scale_and_one_colorbar(settings):
    panels = [
        render.Panel(array=_array(top), title=f"panel {k}") for k, top in enumerate((4, 8, 16, 6))
    ]
    fig, results = render.small_multiples(panels, GRID, kind="pm25", settings=settings)
    try:
        assert len(results) == 4
        assert {(r.mappable.norm.vmin, r.mappable.norm.vmax) for r in results} == {(0.0, PM25_MAX)}
        colorbars = [a for a in fig.axes if a.get_label() == "<colorbar>"]
        assert len(colorbars) == 1
        assert colorbars[0].get_ylabel() == "PM2.5 (µg/m³)"
        panel_axes = [a for a in fig.axes if a.get_label() != "<colorbar>"]
        assert {a.get_xlim() for a in panel_axes} == {panel_axes[0].get_xlim()}
        assert fig.get_size_inches()[0] == pytest.approx(6.5)
        inner = panel_axes[1]  # top-right: no y tick labels
        assert not any(t.get_visible() and t.get_text() for t in inner.get_yticklabels())
        assert any(r.clipped for r in results)
    finally:
        plt.close(fig)


# --- Reading and saving -----------------------------------------------------------------------


# @spec MAP-READ-001
def test_read_surface_returns_bands_transform_and_tags(tmp_path):
    from aqdt.fusion.raster import encode_surface
    from aqdt.fusion.schemas import SurfaceFit, SurfaceStatus
    from aqdt.maps.surface import read_surface

    est, var = _array(), _array(2.0)
    from datetime import UTC, datetime

    fit = SurfaceFit(
        hour=datetime(2026, 9, 14, 10, tzinfo=UTC),
        status=SurfaceStatus.kriged,
        n_sensors=30,
        n_points=30,
        variogram_model="spherical",
        sill=1.0,
        range_m=5000.0,
        nugget=0.1,
        q1=0.0,
        q2=1.0,
        cr=0.5,
        resolution_m=float(GRID.resolution_m),
        width=GRID.width,
        height=GRID.height,
        surface_uri="x",
        surface_sha256=None,
    )
    path = tmp_path / "s.tif"
    path.write_bytes(encode_surface(est, var, GRID, fit))
    surface = read_surface(str(path))
    np.testing.assert_allclose(surface.pm25, est)
    np.testing.assert_allclose(surface.variance, var)
    assert surface.transform.a == GRID.resolution_m and surface.transform.c == GRID.x_min
    assert surface.tags["variogram_model"] == "spherical"


# @spec MAP-SAVE-001
def test_save_figure_is_300_dpi_and_tight(tmp_path, settings):
    fig, ax = plt.subplots(figsize=(3.3, 3.0))
    render.render_surface(ax, _array(), GRID, kind="pm25", settings=settings)
    path = tmp_path / "f.png"
    render.save_figure(fig, path)
    with Image.open(path) as image:
        assert round(image.info["dpi"][0]) == 300
        assert image.size[0] / 300 < 3.3 + 0.2


# --- Settings ---------------------------------------------------------------------------------


# @spec MAP-CFG-001, MAP-CFG-002
def test_settings_defaults_environment_and_validation(monkeypatch, settings):
    assert (PM25_MAX, SD_MAX) == (12.0, 3.0)
    assert (settings.pm25_max, settings.sd_max) == (PM25_MAX, SD_MAX)
    monkeypatch.setenv("AQDT_MAP_PM25_MAX", "20")
    assert MapSettings().pm25_max == 20.0
    for field in ("pm25_max", "sd_max"):
        with pytest.raises(ValidationError, match=field):
            MapSettings(**{field: 0})


# @spec MAP-CFG-003
def test_rendering_needs_no_display():
    assert matplotlib.get_backend().lower() == "agg"
