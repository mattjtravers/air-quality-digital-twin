"""The dashboard build over the synthetic archive — DASH-WIN, IN, GAP, FRAME, MAN, SITE."""

from __future__ import annotations

import json
import logging
import math
import re
import struct
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from pyproj import Transformer
from rasterio.transform import Affine, array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject

from aqdt.calibration.products import CALIBRATED_HOURLY
from aqdt.fusion.products import SURFACE_FITS
from aqdt.maps.render import colorize, colour_stops
from aqdt.maps.settings import PM25_MAX, SD_MAX
from aqdt.maps.surface import read_surface
from aqdt.observation_store.archive import read_partitioned, read_sites

from ..evaluation.conftest import FLAGGED, MONITORS, NULL_VALUE
from .conftest import GENERATED_AT, H10, H11, H12, H13, H14, H, build

PACKAGE = Path(__file__).resolve().parents[2] / "src" / "aqdt" / "dashboard"
ISO_UTC = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
HOUR_KEYS = {
    "hour",
    "status",
    "pm25_png",
    "sd_png",
    "bounds_latlon",
    "surface_min",
    "surface_mean",
    "surface_max",
    "sd_max",
    "n_sensors",
    "variogram_model",
    "monitors",
    "sensors",
}
SURFACE_FIELDS = (
    "pm25_png",
    "sd_png",
    "bounds_latlon",
    "surface_min",
    "surface_mean",
    "surface_max",
    "sd_max",
    "variogram_model",
)


def iso(hour) -> str:
    return hour.strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest_of(out: Path) -> dict:
    return json.loads((out / "data" / "manifest.json").read_text())


def hours_by_time(manifest: dict) -> dict[str, dict]:
    return {h["hour"]: h for h in manifest["hours"]}


def fits_by_hour(archive: str) -> dict[str, object]:
    fits = read_partitioned(archive, SURFACE_FITS)
    return {iso(row.hour.to_pydatetime()): row for row in fits.itertuples(index=False)}


def png_chunks(path: Path) -> list[bytes]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, offset = [], 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunks.append(data[offset + 4 : offset + 8])
        offset += 12 + length
    return chunks


def to_3857(array: np.ndarray, transform: Affine):
    """The reprojection DASH-FRAME-001 specifies, done independently of the build."""
    height, width = array.shape
    bounds = array_bounds(height, width, transform)
    dst_transform, dst_width, dst_height = calculate_default_transform(
        26918, 3857, width, height, *bounds
    )
    dst = np.full((dst_height, dst_width), np.nan, dtype=np.float32)
    reproject(
        array.astype(np.float32),
        dst,
        src_transform=transform,
        src_crs=26918,
        src_nodata=np.nan,
        dst_transform=dst_transform,
        dst_crs=3857,
        dst_nodata=np.nan,
        resampling=Resampling.nearest,
    )
    return dst, dst_transform


# --- Window -----------------------------------------------------------------------------------


# @spec DASH-WIN-001
def test_build_site_requires_an_aware_time_and_positive_hours(archive, tmp_path):
    with pytest.raises(ValueError):
        build(archive, tmp_path / "a", generated_at=GENERATED_AT.replace(tzinfo=None))
    with pytest.raises(ValueError):
        build(archive, tmp_path / "b", hours=0)
    source = "\n".join(p.read_text() for p in PACKAGE.glob("*.py"))
    assert ".now(" not in source and "utcnow" not in source and "time.time()" not in source


# @spec DASH-WIN-002
def test_default_window_ends_at_the_latest_kriged_hour(archive, tmp_path):
    summary = build(archive, tmp_path / "site", hours=5)
    manifest = manifest_of(tmp_path / "site")
    assert manifest["latest_hour"] == iso(H12)
    assert manifest["window_start"] == iso(H12 - 4 * H)
    assert manifest["window_end"] == iso(H12 + H)
    assert [h["hour"] for h in manifest["hours"]] == [iso(H12 - k * H) for k in range(4, -1, -1)]
    assert summary.latest_hour == H12


# @spec DASH-WIN-002
def test_hours_at_or_after_generated_at_are_ignored(archive, tmp_path):
    build(archive, tmp_path / "site", hours=2, generated_at=H11)
    assert manifest_of(tmp_path / "site")["latest_hour"] == iso(H10)


# @spec DASH-WIN-002, DASH-WIN-004
def test_search_is_bounded_to_seven_days(archive, tmp_path):
    build(archive, tmp_path / "ok", hours=1, generated_at=H12 + 7 * 24 * H - H)
    out = tmp_path / "late"
    with pytest.raises(RuntimeError, match="kriged"):
        build(archive, out, hours=1, generated_at=H12 + 8 * 24 * H)
    assert not out.exists()


# @spec DASH-WIN-003
def test_explicit_end_is_exclusive_and_truncated(site, archive, tmp_path):
    out, _ = site
    manifest = manifest_of(out)
    assert manifest["window_start"] == iso(H10)
    assert manifest["window_end"] == iso(H14 + H)
    assert manifest["latest_hour"] == iso(H12)
    build(archive, tmp_path / "t", hours=5, end=H14 + H + (H / 3))
    assert manifest_of(tmp_path / "t")["hours"] == manifest["hours"]


# @spec DASH-WIN-004
def test_a_window_without_a_kriged_hour_fails_before_writing(archive, tmp_path):
    out = tmp_path / "site"
    with pytest.raises(RuntimeError) as error:
        build(archive, out, hours=2, end=H14 + H)
    assert iso(H13) in str(error.value) or H13.isoformat() in str(error.value)
    assert not out.exists()


# --- Inputs -----------------------------------------------------------------------------------


# @spec DASH-IN-001
def test_the_build_reads_the_archive_only():
    source = "\n".join(p.read_text() for p in PACKAGE.rglob("*.py"))
    for forbidden in ("postgis", "psycopg", "httpx", "aqdt.purpleair", "aqdt.airnow", "connect("):
        assert forbidden not in source, forbidden


# --- Hour status ------------------------------------------------------------------------------


# @spec DASH-GAP-001, DASH-GAP-003
def test_every_hour_has_one_status_and_gaps_carry_no_surface(site):
    out, _ = site
    hours = hours_by_time(manifest_of(out))
    assert {k: h["status"] for k, h in hours.items()} == {
        iso(H10): "kriged",
        iso(H11): "kriged",
        iso(H12): "kriged",
        iso(H13): "insufficient_points",
        iso(H14): "missing",
    }
    for hour in (H13, H14):
        entry = hours[iso(hour)]
        for name in SURFACE_FIELDS:
            assert entry[name] is None, (hour, name)
        stamp = hour.strftime("%Y-%m-%dT%H")
        assert not (out / "data" / "frames" / f"pm25_{stamp}.png").exists()
        assert not (out / "data" / "frames" / f"sd_{stamp}.png").exists()


# @spec DASH-GAP-001, DASH-GAP-002
def test_an_unreadable_raster_costs_one_hour(archive, tmp_path, monkeypatch, caplog):
    from aqdt.dashboard import build as build_module

    broken = fits_by_hour(archive)[iso(H11)].surface_uri
    real = build_module.read_surface

    def read(uri):
        if uri == broken:
            raise FileNotFoundError(uri)
        return real(uri)

    monkeypatch.setattr(build_module, "read_surface", read)
    with caplog.at_level(logging.WARNING):
        summary = build(archive, tmp_path / "site", hours=5, end=H14 + H)
    hours = hours_by_time(manifest_of(tmp_path / "site"))
    assert hours[iso(H11)]["status"] == "surface_missing"
    assert hours[iso(H11)]["pm25_png"] is None
    assert hours[iso(H10)]["status"] == "kriged"
    assert summary.hours["surface_missing"] == 1
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert broken in warning and (iso(H11) in warning or H11.isoformat() in warning)


# --- Frames -----------------------------------------------------------------------------------


# @spec DASH-FRAME-001, DASH-FRAME-002
def test_frames_are_the_reprojected_surface_on_maps_scales(site, archive):
    out, _ = site
    hours = hours_by_time(manifest_of(out))
    surface = read_surface(fits_by_hour(archive)[iso(H11)].surface_uri)
    pm25, _ = to_3857(surface.pm25, surface.transform)
    sd, _ = to_3857(np.sqrt(np.clip(surface.variance, 0, None)), surface.transform)
    for kind, array in (("pm25", pm25), ("sd", sd)):
        image = np.asarray(Image.open(out / hours[iso(H11)][f"{kind}_png"]).convert("RGBA"))
        assert image.shape == (*array.shape, 4), kind
        np.testing.assert_array_equal(image, colorize(array, kind=kind), err_msg=kind)
        assert (image[..., 3] == 0).any(), f"{kind}: cells outside the grid are transparent"
        assert (image[..., 3] == 255).any(), kind


# @spec DASH-FRAME-003
def test_frames_are_bare_pngs_named_for_the_hour(site):
    out, summary = site
    frames = sorted((out / "data" / "frames").iterdir())
    expected = sorted(
        f"{kind}_{hour.strftime('%Y-%m-%dT%H')}.png"
        for kind in ("pm25", "sd")
        for hour in (H10, H11, H12)
    )
    assert [p.name for p in frames] == expected
    assert summary.frames == 6
    for path in frames:
        chunks = png_chunks(path)
        assert set(chunks) <= {b"IHDR", b"IDAT", b"IEND"}, (path.name, chunks)
        assert Image.open(path).mode == "RGBA"


# @spec DASH-FRAME-004
def test_bounds_are_the_web_mercator_extent_in_lat_lon(site, archive):
    out, _ = site
    entry = hours_by_time(manifest_of(out))[iso(H10)]
    surface = read_surface(fits_by_hour(archive)[iso(H10)].surface_uri)
    array, transform = to_3857(surface.pm25, surface.transform)
    west, south, east, north = array_bounds(*array.shape, transform)
    to_lonlat = Transformer.from_crs(3857, 4326, always_xy=True)
    (w, e), (s, n) = to_lonlat.transform([west, east], [south, north])
    (sw, ne) = entry["bounds_latlon"]
    assert sw == pytest.approx([s, w], abs=1e-5)
    assert ne == pytest.approx([n, e], abs=1e-5)
    # the grid's own corners all fall inside the overlay
    height, width = surface.pm25.shape
    gx0, gy0, gx1, gy1 = array_bounds(height, width, surface.transform)
    lon, lat = Transformer.from_crs(26918, 4326, always_xy=True).transform(
        [gx0, gx1, gx0, gx1], [gy0, gy0, gy1, gy1]
    )
    assert min(lat) >= s - 1e-5 and max(lat) <= n + 1e-5
    assert min(lon) >= w - 1e-5 and max(lon) <= e + 1e-5


# @spec DASH-FRAME-005
def test_frame_statistics_come_from_the_source_grid(site, archive):
    out, _ = site
    hours = hours_by_time(manifest_of(out))
    for hour in (H10, H11, H12):
        surface = read_surface(fits_by_hour(archive)[iso(hour)].surface_uri)
        pm25 = surface.pm25[np.isfinite(surface.pm25)]
        sd = np.sqrt(np.clip(surface.variance, 0, None))
        sd = sd[np.isfinite(sd)]
        entry = hours[iso(hour)]
        assert entry["surface_min"] == round(float(pm25.min()), 2)
        assert entry["surface_mean"] == round(float(pm25.mean()), 2)
        assert entry["surface_max"] == round(float(pm25.max()), 2)
        assert entry["sd_max"] == round(float(sd.max()), 2)


# --- Manifest ---------------------------------------------------------------------------------


# @spec DASH-MAN-001
def test_manifest_validates_and_carries_utc_timestamps(site):
    from aqdt.dashboard.manifest import Manifest

    out, _ = site
    text = (out / "data" / "manifest.json").read_text()
    Manifest.model_validate_json(text)
    manifest = json.loads(text)
    assert set(manifest) == {
        "generated_at",
        "latest_hour",
        "window_start",
        "window_end",
        "scales",
        "hours",
    }
    assert manifest["generated_at"] == iso(GENERATED_AT)
    for key in ("generated_at", "latest_hour", "window_start", "window_end"):
        assert ISO_UTC.fullmatch(manifest[key]), key
    for entry in manifest["hours"]:
        assert ISO_UTC.fullmatch(entry["hour"])
    schema = Manifest.model_json_schema()
    assert set(schema["required"]) >= set(manifest)


# @spec DASH-MAN-002
def test_hours_are_contiguous_and_carry_their_fit(site, archive):
    out, _ = site
    manifest = manifest_of(out)
    assert [h["hour"] for h in manifest["hours"]] == [iso(H10 + k * H) for k in range(5)]
    fits = fits_by_hour(archive)
    for entry in manifest["hours"]:
        assert set(entry) == HOUR_KEYS
        fit = fits.get(entry["hour"])
        assert entry["n_sensors"] == (None if fit is None else int(fit.n_sensors))
        if entry["status"] == "kriged":
            assert entry["variogram_model"] == fit.variogram_model
            for kind in ("pm25", "sd"):
                path = entry[f"{kind}_png"]
                assert not path.startswith("/") and (out / path).is_file()


# @spec DASH-MAN-003
def test_scales_come_from_maps(site):
    out, _ = site
    scales = manifest_of(out)["scales"]
    assert scales["pm25"] == {
        "max": PM25_MAX,
        "cmap": "viridis",
        "units": "µg/m³",
        "label": "PM2.5 (µg/m³)",
        "stops": colour_stops("pm25", 11),
    }
    assert scales["sd"] == {
        "max": SD_MAX,
        "cmap": "magma",
        "units": "µg/m³",
        "label": "Standard deviation (µg/m³)",
        "stops": colour_stops("sd", 11),
    }


# @spec DASH-MAN-004
def test_monitors_are_the_hours_airnow_observations(site, archive):
    out, _ = site
    hours = hours_by_time(manifest_of(out))
    sites = read_sites(archive).set_index("site_id")
    for hour in (H10, H11, H12, H13, H14):
        monitors = hours[iso(hour)]["monitors"]
        assert [m["site_id"] for m in monitors] == sorted(MONITORS), hour
        for m in monitors:
            assert set(m) == {"site_id", "name", "lat", "lon", "obs", "qc_flags"}
            assert m["name"] == sites.loc[m["site_id"], "name"]
            assert m["lat"] == round(MONITORS[m["site_id"]][0], 5)
    by_id = {m["site_id"]: m for m in hours[iso(FLAGGED[1])]["monitors"]}
    assert by_id[FLAGGED[0]]["qc_flags"] == ["flatline"]
    assert isinstance(by_id[FLAGGED[0]]["obs"], float)
    by_id = {m["site_id"]: m for m in hours[iso(NULL_VALUE[1])]["monitors"]}
    assert by_id[NULL_VALUE[0]]["obs"] is None
    assert by_id[NULL_VALUE[0]]["qc_flags"] == ["missing_value"]


# @spec DASH-MAN-005
def test_sensors_are_every_calibrated_row_of_the_hour(site, archive):
    out, _ = site
    hours = hours_by_time(manifest_of(out))
    calibrated = read_partitioned(archive, CALIBRATED_HOURLY)
    sited = set(read_sites(archive)["site_id"])
    for hour in (H10, H11, H12, H13, H14):
        rows = calibrated[calibrated["hour"] == hour]
        rows = rows[rows["site_id"].isin(sited)].sort_values("site_id")
        sensors = hours[iso(hour)]["sensors"]
        assert [s["sensor_id"] for s in sensors] == list(rows["site_id"]), hour
        for s, row in zip(sensors, rows.itertuples(index=False)):
            assert set(s) == {"sensor_id", "lat", "lon", "pm25_calibrated", "fit_status"}
            assert s["fit_status"] == row.fit_status
            if row.pm25_calibrated is None or not math.isfinite(row.pm25_calibrated):
                assert s["pm25_calibrated"] is None
            else:
                assert s["pm25_calibrated"] == round(float(row.pm25_calibrated), 2)
    assert hours[iso(H13)]["sensors"], "a non-kriged hour still lists its sensors"


def _decimals(value: float) -> int:
    text = repr(value)
    return len(text.split(".")[1]) if "." in text and "e" not in text else 0


# @spec DASH-MAN-006
def test_manifest_is_rounded_sorted_and_key_sorted(site):
    out, _ = site
    text = (out / "data" / "manifest.json").read_text()

    def check_order(pairs):
        keys = [k for k, _ in pairs]
        assert keys == sorted(keys), keys
        return dict(pairs)

    manifest = json.loads(text, object_pairs_hook=check_order)
    for entry in manifest["hours"]:
        for point in entry["monitors"] + entry["sensors"]:
            assert _decimals(point["lat"]) <= 5 and _decimals(point["lon"]) <= 5
        for value in [p.get("obs") for p in entry["monitors"]] + [
            p["pm25_calibrated"] for p in entry["sensors"]
        ]:
            assert value is None or _decimals(value) <= 2
        for name in ("surface_min", "surface_mean", "surface_max", "sd_max"):
            assert entry[name] is None or _decimals(entry[name]) <= 2
        if entry["bounds_latlon"]:
            for corner in entry["bounds_latlon"]:
                assert all(_decimals(v) <= 5 for v in corner)


# @spec DASH-MAN-006
def test_rebuild_is_stateless_and_deterministic(site, archive, tmp_path):
    out, _ = site
    again = tmp_path / "again"
    build(archive, again, hours=5, end=H14 + H, generated_at=GENERATED_AT + 3 * H)
    first, second = manifest_of(out), manifest_of(again)
    assert first.pop("generated_at") != second.pop("generated_at")
    assert first == second
    files = sorted(p.relative_to(out) for p in out.rglob("*") if p.is_file())
    assert files == sorted(p.relative_to(again) for p in again.rglob("*") if p.is_file())
    for path in files:
        if path.name != "manifest.json":
            assert (out / path).read_bytes() == (again / path).read_bytes(), path


# --- Site output ------------------------------------------------------------------------------


# @spec DASH-SITE-001
def test_a_failing_build_leaves_out_unchanged(archive, tmp_path, monkeypatch):
    from aqdt.dashboard import build as build_module

    out = tmp_path / "site"
    build(archive, out, hours=5, end=H14 + H)
    before = {p: p.read_bytes() for p in out.rglob("*") if p.is_file()}

    def explode(*args, **kwargs):
        raise RuntimeError("colour failure")

    monkeypatch.setattr(build_module, "colorize", explode)
    with pytest.raises(RuntimeError, match="colour failure"):
        build(archive, out, hours=5, end=H14 + H, generated_at=GENERATED_AT + H)
    assert {p: p.read_bytes() for p in out.rglob("*") if p.is_file()} == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["site"], "no temporary left behind"


# @spec DASH-SITE-002
def test_out_must_be_empty_or_a_previous_build(archive, tmp_path):
    out = tmp_path / "work"
    out.mkdir()
    (out / "notes.txt").write_text("keep me")
    with pytest.raises(RuntimeError, match=str(out)):
        build(archive, out, hours=5, end=H14 + H)
    assert (out / "notes.txt").read_text() == "keep me"
    assert sorted(p.name for p in out.iterdir()) == ["notes.txt"]

    empty = tmp_path / "empty"
    empty.mkdir()
    build(archive, empty, hours=5, end=H14 + H)
    stale = empty / "data" / "frames" / "pm25_2000-01-01T00.png"
    stale.write_bytes(b"old")
    build(archive, empty, hours=5, end=H14 + H)
    assert not stale.exists(), "a previous build is replaced whole"


# @spec DASH-SITE-003
def test_web_assets_are_copied_to_the_site_root(site):
    out, _ = site
    for name in ("index.html", "app.js", "style.css"):
        assert (out / name).read_bytes() == (PACKAGE / "web" / name).read_bytes(), name
    for path in (PACKAGE / "web").rglob("*"):
        if path.is_file():
            assert (out / path.relative_to(PACKAGE / "web")).is_file(), path


# @spec DASH-SITE-004
def test_build_summary(site):
    out, summary = site
    assert summary.window_start == H10 and summary.window_end == H14 + H
    assert summary.latest_hour == H12
    assert summary.hours == {"kriged": 3, "insufficient_points": 1, "missing": 1}
    assert summary.frames == 6
    assert summary.site_bytes == sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    assert summary.elapsed_s >= 0


# @spec DASH-SITE-005
def test_targets_are_five_minutes_and_fifty_megabytes(archive, tmp_path, monkeypatch, caplog):
    from aqdt.dashboard import build as build_module

    assert build_module.TARGET_SECONDS == 300
    assert build_module.TARGET_BYTES == 50_000_000
    with caplog.at_level(logging.WARNING):
        build(archive, tmp_path / "quiet", hours=5, end=H14 + H)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
    monkeypatch.setattr(build_module, "TARGET_SECONDS", 0)
    monkeypatch.setattr(build_module, "TARGET_BYTES", 1)
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        build(archive, tmp_path / "loud", hours=5, end=H14 + H)
    warnings = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "seconds" in warnings and "bytes" in warnings
