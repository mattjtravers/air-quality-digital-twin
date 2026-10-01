"""krige_surfaces end to end — FUS-IN-001, FUS-STATUS, FUS-RASTER-006, FUS-STORE-007, FUS-RUN."""

import hashlib
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest
import rasterio
from pydantic import ValidationError

from aqdt.calibration.frames import records_to_frame
from aqdt.calibration.products import CALIBRATED_HOURLY
from aqdt.calibration.schemas import CalibratedHourly
from aqdt.fusion import run as run_module
from aqdt.fusion.frames import SurfaceFitFrame
from aqdt.fusion.products import SURFACE_FITS
from aqdt.fusion.raster import surface_key
from aqdt.fusion.run import krige_surfaces
from aqdt.observation_store.archive import read_partitioned, write_partitioned

from .conftest import H10, H11, H12, H13, H14, POINTS_AT_10, USED_AT_10, H, hour10_rows

WINDOW = (H10, H14 + H)  # 10:00 .. 14:00 inclusive


def _row(frame, hour):
    (row,) = frame[frame["hour"] == hour].to_dict("records")
    return row


def _raster(archive_uri, hour) -> Path:
    return Path(archive_uri) / surface_key(hour)


@pytest.fixture
def result(archive_uri, settings):
    return krige_surfaces(archive_uri, *WINDOW, settings)


# --- Inputs -----------------------------------------------------------------------------------


# @spec FUS-IN-001
def test_inputs_come_from_the_archive_and_never_postgis(archive_uri, settings, monkeypatch):
    seen: list = []
    real = run_module.read_partitioned

    def spy(uri, product, **filters):
        seen.append(product)
        return real(uri, product, **filters)

    monkeypatch.setattr(run_module, "read_partitioned", spy)
    krige_surfaces(archive_uri, H10, H11, settings)
    assert CALIBRATED_HOURLY in seen
    package = Path(run_module.__file__).parent
    source = "\n".join(p.read_text() for p in package.glob("*.py")).lower()
    assert "psycopg" not in source and "cursor(" not in source


# --- Statuses -------------------------------------------------------------------------------


# @spec FUS-STATUS-001
def test_one_row_per_hour_with_a_known_status(result):
    assert list(result["hour"]) == [H10, H11, H12, H13, H14]
    assert list(result["status"]) == [
        "kriged",
        "insufficient_points",
        "fit_failed",
        "insufficient_points",
        "kriged",
    ]


# @spec FUS-STATUS-002
def test_too_few_points_is_insufficient_and_unfitted(archive_uri, settings, monkeypatch):
    from aqdt.fusion import krige

    calls: list = []
    real = krige.select_variogram
    monkeypatch.setattr(krige, "select_variogram", lambda p: calls.append(p) or real(p))
    monkeypatch.setattr(run_module, "select_variogram", krige.select_variogram, raising=False)
    frame = krige_surfaces(archive_uri, H11, H12, settings)
    row = _row(frame, H11)
    assert row["status"] == "insufficient_points" and row["n_points"] == 5
    assert calls == []
    assert _row(krige_surfaces(archive_uri, H13, H14, settings), H13)["n_sensors"] == 0


# @spec FUS-STATUS-003
def test_no_eligible_variogram_is_fit_failed(result):
    row = _row(result, H12)
    assert row["status"] == "fit_failed" and row["n_points"] == 12


# @spec FUS-STATUS-004
@pytest.mark.parametrize("hour", [H11, H12, H13])
def test_unkriged_hours_carry_counts_and_nulls(result, hour):
    row = _row(result, hour)
    assert row["n_sensors"] is not None and row["n_points"] is not None
    for name in (
        "variogram_model",
        "sill",
        "range_m",
        "nugget",
        "q1",
        "q2",
        "cr",
        "resolution_m",
        "width",
        "height",
        "surface_uri",
        "surface_sha256",
    ):
        value = row[name]
        assert value is None or (isinstance(value, float) and np.isnan(value)), (hour, name)


# @spec FUS-STATUS-004
# @spec FUS-VAR-004
def test_kriged_hours_carry_the_fit_and_counts(result, settings):
    row = _row(result, H10)
    assert (row["n_sensors"], row["n_points"]) == (USED_AT_10, POINTS_AT_10)
    assert row["variogram_model"] in ("spherical", "exponential")
    assert row["resolution_m"] == settings.resolution_m
    assert row["width"] > 0 and row["height"] > 0


# @spec FUS-STATUS-005
def test_a_raster_for_an_hour_that_stops_being_kriged_is_deleted(archive_uri, settings):
    krige_surfaces(archive_uri, H10, H11, settings)
    assert _raster(archive_uri, H10).exists()
    strict = settings.model_copy(update={"min_points": 500})
    row = _row(krige_surfaces(archive_uri, H10, H11, strict), H10)
    assert row["status"] == "insufficient_points"
    assert not _raster(archive_uri, H10).exists()
    stored = read_partitioned(archive_uri, SURFACE_FITS)
    assert _row(stored, H10)["status"] == "insufficient_points"


# @spec FUS-STATUS-005
def test_rasters_exist_exactly_for_kriged_hours(result, archive_uri):
    for hour, status in zip(result["hour"], result["status"]):
        assert _raster(archive_uri, hour).exists() == (status == "kriged"), hour


# --- Raster and row ---------------------------------------------------------------------------


# @spec FUS-STORE-007
def test_rows_point_at_their_rasters_by_uri_and_digest(result, archive_uri):
    for hour in (H10, H14):
        row = _row(result, hour)
        path = _raster(archive_uri, hour)
        assert row["surface_uri"] == f"{archive_uri}/{surface_key(hour)}"
        assert row["surface_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()


# @spec FUS-RASTER-003
def test_raster_tags_equal_the_row(result, archive_uri):
    row = _row(result, H10)
    with rasterio.open(_raster(archive_uri, H10)) as src:
        tags = src.tags()
        assert (src.width, src.height) == (row["width"], row["height"])
    assert tags["variogram_model"] == row["variogram_model"]
    assert float(tags["sill"]) == pytest.approx(row["sill"])
    assert float(tags["range_m"]) == pytest.approx(row["range_m"])
    assert float(tags["nugget"]) == pytest.approx(row["nugget"])
    assert int(tags["n_points"]) == row["n_points"]


# @spec FUS-RASTER-006
def test_hour_inputs_are_read_inside_produce_after_the_token(archive_uri, settings, monkeypatch):
    """Another writer changes the hour's calibrated values just before the guarded replacement
    starts; the surface written must come from the changed values, which holds only if the
    inputs are read inside ``produce`` (the store then orders ``produce`` after the token)."""
    real = run_module.replace_object

    def competing_then_real(uri, key, produce):
        if key == surface_key(H10):
            shifted = records_to_frame(hour10_rows(offset=100.0), CalibratedHourly)
            write_partitioned(shifted, uri, CALIBRATED_HOURLY)
        return real(uri, key, produce)

    monkeypatch.setattr(run_module, "replace_object", competing_then_real)
    krige_surfaces(archive_uri, H10, H11, settings)
    with rasterio.open(_raster(archive_uri, H10)) as src:
        estimate = src.read(1)
    assert float(np.nanmedian(estimate)) > 100.0


# --- Runs -----------------------------------------------------------------------------------


# @spec FUS-RUN-001
def test_run_returns_validated_rows_in_hour_order_and_writes_them(result, archive_uri):
    SurfaceFitFrame.validate(result)
    assert list(result["hour"]) == sorted(result["hour"])
    stored = read_partitioned(archive_uri, SURFACE_FITS)
    assert sorted(stored["hour"]) == list(result["hour"])
    assert (Path(archive_uri) / "fusion/surface_fits/date=2026-09-20/surface_fits.parquet").exists()


# @spec FUS-RUN-001
def test_rasters_and_rows_are_written_hour_by_hour_in_order(archive_uri, settings, monkeypatch):
    events: list[tuple[str, object]] = []
    real_replace, real_write = run_module.replace_object, run_module.write_partitioned

    def replace(uri, key, produce):
        events.append(("raster", key))
        return real_replace(uri, key, produce)

    def write(frame, uri, product):
        events.append(("row", tuple(frame["hour"])))
        return real_write(frame, uri, product)

    monkeypatch.setattr(run_module, "replace_object", replace)
    monkeypatch.setattr(run_module, "write_partitioned", write)
    krige_surfaces(archive_uri, H10, H12, settings)
    assert events == [
        ("raster", surface_key(H10)),
        ("row", (H10,)),
        ("raster", surface_key(H11)),
        ("row", (H11,)),
    ]


# @spec FUS-RUN-001
def test_given_a_connection_the_run_loads_the_partitions_it_wrote(
    archive_uri, settings, monkeypatch
):
    loaded: list = []
    monkeypatch.setattr(
        run_module, "load_partitions", lambda conn, uri, parts: loaded.append((conn, uri, parts))
    )
    conn = object()
    krige_surfaces(archive_uri, H10, H12, settings, conn=conn)
    ((got_conn, got_uri, parts),) = loaded
    assert got_conn is conn and got_uri == archive_uri
    assert set(parts) == {f"{archive_uri}/fusion/surface_fits/date=2026-09-20/surface_fits.parquet"}

    loaded.clear()
    krige_surfaces(archive_uri, H10, H12, settings)
    assert loaded == []


# @spec FUS-RUN-002
def test_bounds_must_be_aware_and_ordered_and_are_truncated(archive_uri, settings):
    with pytest.raises((ValueError, ValidationError)):
        krige_surfaces(archive_uri, datetime(2026, 9, 20, 10), H11, settings)
    with pytest.raises((ValueError, ValidationError)):
        krige_surfaces(archive_uri, H11, H10, settings)
    with pytest.raises((ValueError, ValidationError)):
        krige_surfaces(
            archive_uri, H10 + timedelta(minutes=20), H10 + timedelta(minutes=40), settings
        )
    eastern = datetime(2026, 9, 20, 6, 45, tzinfo=timezone(timedelta(hours=-4)))
    frame = krige_surfaces(archive_uri, eastern, H12, settings)
    assert list(frame["hour"]) == [H10, H11]


# @spec FUS-RUN-003
def test_a_failure_partway_leaves_finished_hours_complete(archive_uri, settings, monkeypatch):
    from aqdt.fusion import krige

    real = krige.predict_chunked
    calls: list = []

    def failing(variogram, grid, *args, **kwargs):
        calls.append(variogram)
        if len(calls) == 2:  # 10:00 is the first kriged hour, 14:00 the second
            raise RuntimeError("injected failure at 14:00")
        return real(variogram, grid, *args, **kwargs)

    monkeypatch.setattr(krige, "predict_chunked", failing)
    monkeypatch.setattr(run_module, "predict_chunked", failing, raising=False)
    with pytest.raises(RuntimeError, match="14:00"):
        krige_surfaces(archive_uri, *WINDOW, settings)
    stored = read_partitioned(archive_uri, SURFACE_FITS)
    assert sorted(stored["hour"]) == [H10, H11, H12, H13]
    assert _raster(archive_uri, H10).exists()
    assert not _raster(archive_uri, H14).exists()


# @spec FUS-RUN-004
def test_rerunning_against_an_unchanged_archive_is_byte_identical(archive_uri, settings):
    first = krige_surfaces(archive_uri, *WINDOW, settings)
    files = sorted(Path(archive_uri, "fusion").rglob("*.*"))
    before = {p: p.read_bytes() for p in files if not p.name.startswith(".")}
    second = krige_surfaces(archive_uri, *WINDOW, settings)
    after = {p: p.read_bytes() for p in files if not p.name.startswith(".")}
    assert before == after
    assert first.equals(second)


# @spec FUS-RUN-005
def test_a_window_with_no_calibrated_rows_succeeds_with_every_hour_insufficient(
    archive_uri, settings
):
    start = datetime(2026, 9, 25, 0, tzinfo=UTC)
    frame = krige_surfaces(archive_uri, start, start + 3 * H, settings)
    assert list(frame["status"]) == ["insufficient_points"] * 3
    assert list(frame["n_sensors"]) == [0, 0, 0]
    assert not list(Path(archive_uri, "fusion/surfaces").rglob("*2026-09-25T*.tif"))
