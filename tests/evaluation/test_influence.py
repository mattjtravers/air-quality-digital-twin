"""Sensor influence at a hotspot — EVAL-INFL. One hour of the synthetic archive, re-kriged without
each sensor near a centre, and the pure pieces checked against hand-computed values."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from PIL import Image
from pyproj import Transformer

from aqdt.calibration.products import CALIBRATED_HOURLY, FITS
from aqdt.fusion.grid import grid_for
from aqdt.fusion.krige import predict_chunked, select_variogram
from aqdt.fusion.points import hour_points, hour_sensors
from aqdt.fusion.products import SURFACE_FITS
from aqdt.fusion.schemas import FusionSettings
from aqdt.maps.surface import read_surface
from aqdt.observation_store.archive import read_observations, read_partitioned, read_sites
from aqdt.observation_store.schemas import Source

from .conftest import H11, H13, MONITORS, RESOLUTION_M, SENSORS

H = timedelta(hours=1)
TO_UTM = Transformer.from_crs(4326, 26918, always_xy=True)
TO_LONLAT = Transformer.from_crs(26918, 4326, always_xy=True)
#: a sensor near the middle of the box, so several neighbours fall within the radius
CENTRE_SENSOR = min(
    SENSORS, key=lambda s: math.hypot(SENSORS[s][0] - 38.9, SENSORS[s][1] - (-77.1))
)
CENTRE = SENSORS[CENTRE_SENSOR][:2]
RADIUS_KM = 15.0


@pytest.fixture(autouse=True)
def _fusion_environment(monkeypatch):
    for name in ("AQDT_BBOX", "AQDT_FUS_MIN_POINTS", "AQDT_FUS_RESOLUTION_M"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="module")
def influence(archive, tmp_path_factory):
    from aqdt.evaluation.influence import sensor_influence

    out = tmp_path_factory.mktemp("influence")
    with pytest.MonkeyPatch.context() as mp:
        for name in ("AQDT_BBOX", "AQDT_FUS_MIN_POINTS", "AQDT_FUS_RESOLUTION_M"):
            mp.delenv(name, raising=False)
        summary = sensor_influence(archive, H11, out, center=CENTRE, radius_km=RADIUS_KM)
    table = pd.read_csv(out / "influence.csv", dtype={"removed": str}, keep_default_na=False)
    return out, summary, table


def _num(value) -> float:
    return float("nan") if value in ("", None) else float(value)


def _krige(points):
    grid = grid_for(FusionSettings().bbox, RESOLUTION_M)
    estimate, _ = predict_chunked(select_variogram(points), grid)
    return grid, estimate


def _measures(grid, estimate, centre_xy, radius_m):
    xs, ys = grid.cell_centres()
    xs, ys = xs.reshape(estimate.shape), ys.reshape(estimate.shape)
    column = math.floor((centre_xy[0] - grid.x_min) / grid.resolution_m)
    row = math.floor((grid.y_max - centre_xy[1]) / grid.resolution_m)
    inside = np.hypot(xs - centre_xy[0], ys - centre_xy[1]) <= radius_m
    mean = float(np.nanmean(estimate))
    return {
        "centre_value": float(estimate[row, column]),
        "hour_mean": mean,
        "peak": float(estimate[inside].max()),
        "area_km2": float((inside & (estimate > mean + 2)).sum() * grid.resolution_m**2 / 1e6),
    }


def _in_radius(archive) -> list[str]:
    sensors = hour_sensors(read_partitioned(archive, CALIBRATED_HOURLY), read_sites(archive), H11)
    cx, cy = TO_UTM.transform(CENTRE[1], CENTRE[0])
    d = np.hypot(sensors["x"].astype(float) - cx, sensors["y"].astype(float) - cy)
    return list(sensors.loc[d <= RADIUS_KM * 1000, "site_id"])


# --- Inputs and centre ------------------------------------------------------------------------


# @spec EVAL-INFL-001
def test_the_hour_must_be_aware_kriged_and_the_radius_positive(archive, tmp_path):
    from aqdt.evaluation.influence import sensor_influence

    with pytest.raises(ValueError):
        sensor_influence(archive, H11.replace(tzinfo=None), tmp_path / "a")
    with pytest.raises(ValueError):
        sensor_influence(archive, H11, tmp_path / "b", radius_km=0)
    with pytest.raises(RuntimeError, match=H13.strftime("%Y-%m-%dT%H")):
        sensor_influence(archive, H13, tmp_path / "c")
    assert not any((tmp_path / name).exists() for name in "abc")


# @spec EVAL-INFL-001, EVAL-INFL-002
def test_default_centre_is_the_stored_peak_cell(archive, tmp_path):
    from aqdt.evaluation.influence import sensor_influence

    summary = sensor_influence(archive, H11 + timedelta(minutes=40), tmp_path, radius_km=1.0)
    assert summary.hour == H11
    fits = read_partitioned(archive, SURFACE_FITS)
    surface = read_surface(fits[fits["hour"] == H11].iloc[0].surface_uri)
    row, column = np.unravel_index(np.nanargmax(surface.pm25), surface.pm25.shape)
    x, y = surface.transform @ (column + 0.5, row + 0.5)
    lon, lat = TO_LONLAT.transform(x, y)
    assert summary.center == pytest.approx((lat, lon), abs=1e-5)


# --- Re-krige and measures --------------------------------------------------------------------


# @spec EVAL-INFL-003, EVAL-INFL-004, EVAL-INFL-008
def test_control_and_each_removal_are_re_kriged_and_measured(influence, archive):
    _, _, table = influence
    expected = _in_radius(archive)
    assert len(expected) >= 3, "the fixture puts several sensors within the radius"
    assert table.loc[0, "removed"] == ""
    assert sorted(table["removed"][1:]) == sorted(expected)
    distances = [_num(v) for v in table["distance_km"][1:]]
    assert distances == sorted(distances)

    calibrated = read_partitioned(archive, CALIBRATED_HOURLY)
    sites = read_sites(archive)
    cx, cy = TO_UTM.transform(CENTRE[1], CENTRE[0])
    grid, control = _krige(hour_points(calibrated, sites, H11))
    want = _measures(grid, control, (cx, cy), RADIUS_KM * 1000)
    for name, value in want.items():
        assert _num(table.loc[0, name]) == pytest.approx(value, abs=1e-4), name

    # the control reproduces the stored raster, since fusion is deterministic
    fits = read_partitioned(archive, SURFACE_FITS)
    stored = read_surface(fits[fits["hour"] == H11].iloc[0].surface_uri).pm25
    np.testing.assert_allclose(control, stored, atol=1e-4)

    removed = table.loc[1, "removed"]
    _, without = _krige(hour_points(calibrated[calibrated["site_id"] != removed], sites, H11))
    got = _measures(grid, without, (cx, cy), RADIUS_KM * 1000)
    row = table.loc[1]
    for name, value in got.items():
        assert _num(row[name]) == pytest.approx(value, abs=1e-4), name
        assert _num(row[f"delta_{name}"]) == pytest.approx(value - want[name], abs=1e-4), name
    assert _num(row["peak_above_mean"]) == pytest.approx(got["peak"] - got["hour_mean"], abs=1e-4)
    assert str(row["survives"]) == str(got["area_km2"] > 0)


# @spec EVAL-INFL-004
def test_monitor_value_is_the_nearest_monitor_inside_the_grid(influence, archive):
    _, summary, table = influence
    cx, cy = TO_UTM.transform(CENTRE[1], CENTRE[0])
    grid = grid_for(FusionSettings().bbox, RESOLUTION_M)

    def inside(lat, lon):
        x, y = TO_UTM.transform(lon, lat)
        return grid.x_min <= x < grid.x_min + grid.width * RESOLUTION_M and (
            grid.y_max - grid.height * RESOLUTION_M < y <= grid.y_max
        )

    def distance(site_id):
        lat, lon = MONITORS[site_id]
        x, y = TO_UTM.transform(lon, lat)
        return math.hypot(x - cx, y - cy)

    nearest = min((s for s, ll in MONITORS.items() if inside(*ll)), key=distance)
    assert summary.monitor["site_id"] == nearest
    observed = read_observations(archive, Source.airnow, H11, H11 + H)
    observed = observed[observed["site_id"] == nearest].iloc[0]
    assert summary.monitor["pm25_raw"] == pytest.approx(float(observed["pm25_raw"]))
    assert not math.isnan(_num(table.loc[0, "monitor_value"]))


# @spec EVAL-INFL-003
def test_a_removal_below_min_points_has_null_measures(archive, tmp_path, monkeypatch):
    from aqdt.evaluation.influence import sensor_influence

    n_points = hour_points(
        read_partitioned(archive, CALIBRATED_HOURLY), read_sites(archive), H11
    ).n_points
    monkeypatch.setenv("AQDT_FUS_MIN_POINTS", str(n_points))
    sensor_influence(archive, H11, tmp_path, center=CENTRE, radius_km=RADIUS_KM)
    table = pd.read_csv(tmp_path / "influence.csv", dtype={"removed": str}, keep_default_na=False)
    assert not math.isnan(_num(table.loc[0, "centre_value"]))
    for row in table[1:].itertuples():
        assert math.isnan(_num(row.centre_value)) and math.isnan(_num(row.area_km2))


# --- Evidence ---------------------------------------------------------------------------------


# @spec EVAL-INFL-005
def test_each_sensor_carries_its_values_and_its_fit(influence, archive):
    _, _, table = influence
    calibrated = read_partitioned(archive, CALIBRATED_HOURLY)
    calibrated = calibrated[calibrated["hour"] == H11].set_index("site_id")
    fits = read_partitioned(archive, FITS).set_index(["site_id", "as_of"])
    for row in table[1:].itertuples():
        mine = calibrated.loc[row.removed]
        assert _num(row.pm25_calibrated) == pytest.approx(mine["pm25_calibrated"])
        assert _num(row.pm25_corrected_mean) == pytest.approx(mine["pm25_corrected_mean"])
        assert row.fit_status == mine["fit_status"]
        fit = fits.loc[(row.removed, mine["fit_as_of"])]
        for name in ("slope", "intercept", "r2", "n_pairs"):
            value = fit[name]
            if pd.isna(value):
                assert math.isnan(_num(getattr(row, name))), name
            else:
                assert _num(getattr(row, name)) == pytest.approx(float(value)), name


# @spec EVAL-INFL-006
def test_channel_agreement_comes_from_the_hours_snapshots(influence, archive):
    _, _, table = influence
    snapshots = read_observations(archive, Source.purpleair, H11, H11 + H)
    for row in table[1:].itertuples():
        mine = snapshots[snapshots["site_id"] == row.removed]
        a = mine["pm25_channel_a"].to_numpy(float)
        b = mine["pm25_channel_b"].to_numpy(float)
        assert _num(row.ab_snapshots) == len(mine)
        assert _num(row.channel_a_mean) == pytest.approx(a.mean())
        assert _num(row.channel_b_mean) == pytest.approx(b.mean())
        assert _num(row.ab_abs_diff_mean) == pytest.approx(np.abs(a - b).mean())
        assert _num(row.ab_rel_diff_mean) == pytest.approx((np.abs(a - b) / ((a + b) / 2)).mean())
        assert _num(row.ab_disagreement) == 0


# @spec EVAL-INFL-006
def test_channel_agreement_skips_missing_channels_and_counts_flags():
    from aqdt.evaluation.influence import channel_agreement

    snapshots = pd.DataFrame(
        {
            "site_id": ["s", "s", "s", "t"],
            "pm25_channel_a": [10.0, 4.0, None, 0.0],
            "pm25_channel_b": [6.0, 4.0, 3.0, 0.0],
            "qc_flags": [["channel_disagreement"], [], ["channel_missing"], []],
        }
    )
    out = channel_agreement(snapshots, ["s", "t", "u"]).set_index("site_id")
    assert out.loc["s", "ab_snapshots"] == 3 and out.loc["s", "ab_disagreement"] == 1
    assert out.loc["s", "channel_a_mean"] == pytest.approx(7.0)
    assert out.loc["s", "ab_abs_diff_mean"] == pytest.approx(2.0)
    assert out.loc["s", "ab_rel_diff_mean"] == pytest.approx((4 / 8 + 0) / 2)
    assert math.isnan(out.loc["t", "ab_rel_diff_mean"]), "A + B = 0 has no relative difference"
    assert out.loc["u", "ab_snapshots"] == 0


# @spec EVAL-INFL-007
def test_persistence_counts_top_five_percent_hours_by_rush_and_other():
    from aqdt.evaluation.influence import persistence

    # Monday 2026-11-02, Eastern (EST, UTC−5): 13:00 UTC is 08:00 (rush), 15:00 UTC is 10:00
    rush, other = (datetime(2026, 11, 2, h, tzinfo=UTC) for h in (13, 15))
    weekend = datetime(2026, 11, 1, 13, tzinfo=UTC)  # Sunday 08:00: not rush
    rows = []
    for hour in (rush, other, weekend):
        values = {f"n{k}": float(k) for k in range(20)}
        values["hot"] = 30.0 if hour in (rush, weekend) else 1.0
        rows += [{"site_id": s, "hour": hour, "pm25_calibrated": v} for s, v in values.items()]
    rows.append({"site_id": "late", "hour": rush, "pm25_calibrated": 99.0})
    out = persistence(pd.DataFrame(rows), ["hot", "n19", "late", "absent"], other)
    out = out.set_index("site_id")
    assert out.loc["hot", "top5_hours_present"] == 3
    assert out.loc["hot", "top5_share"] == pytest.approx(2 / 3)
    assert out.loc["hot", "top5_share_rush"] == pytest.approx(1.0)
    assert out.loc["hot", "top5_share_other"] == pytest.approx(0.5)
    # 95th percentiles: 29.45 at rush (hot and late raise it), 18 at 10:00, 19 on Sunday
    assert out.loc["n19", "top5_share"] == pytest.approx(2 / 3)
    assert out.loc["absent", "top5_hours_present"] == 0
    assert math.isnan(out.loc["absent", "top5_share"])
    assert math.isnan(out.loc["late", "top5_share_other"]), "no non-rush hours"


# --- Outputs ----------------------------------------------------------------------------------


# @spec EVAL-INFL-009
def test_figure_is_written_at_300_dpi(influence):
    out, _, _ = influence
    image = Image.open(out / "influence.png")
    assert image.info["dpi"] == pytest.approx((300, 300), abs=0.5)
    assert 3.3 * 300 <= image.width <= 6.6 * 300


# @spec EVAL-INFL-008, EVAL-INFL-010
def test_summary_names_the_most_influential_sensor_and_survival(influence):
    out, summary, table = influence
    removals = table[1:]
    deltas = removals["delta_centre_value"].map(_num).abs()
    assert summary.most_influential["removed"] == removals.loc[deltas.idxmax(), "removed"]
    survives = [str(v) == "True" for v in removals["survives"]]
    assert summary.survives_every_removal == all(survives)
    assert summary.n_removed == len(removals)
    assert summary.radius_km == RADIUS_KM
    written = json.loads((out / "influence.json").read_text())
    assert written["hour"] == "2026-09-14T11:00:00Z"
    assert written["center"] == {"lat": pytest.approx(CENTRE[0]), "lon": pytest.approx(CENTRE[1])}
    assert written["survives_every_removal"] == summary.survives_every_removal
    assert written["most_influential"]["removed"] == summary.most_influential["removed"]
    assert set(written["control"]) >= {"centre_value", "monitor_value", "peak", "area_km2"}
    assert written["monitor"]["site_id"] == summary.monitor["site_id"]
