"""Fitting and the fit_calibrations run — CAL-FIT, CAL-IN-002/003, CAL-RUN-001/003/004/005/006."""

from datetime import datetime, timedelta

import numpy as np
import pytest

from aqdt.calibration import fit as fit_module
from aqdt.calibration.fit import fit_calibrations
from aqdt.calibration.products import FITS, SENSOR_HOURLY
from aqdt.calibration.schemas import CalibrationFit, CalibrationSettings, FitStatus
from aqdt.observation_store.archive import read_partitioned, write_observations

from .conftest import (
    AS_OF,
    DAY,
    FLAGGED_MONITOR_HOUR,
    M1,
    RELATIONS,
    SENSORS,
    H,
    fit_hours,
    monitor_obs,
    monitor_value,
)


def ols(x, y):
    slope, intercept = np.polyfit(x, y, 1)
    pred = intercept + slope * np.asarray(x)
    resid = np.asarray(y) - pred
    r2 = 1 - resid.var() / np.asarray(y).var()
    return slope, intercept, r2, float(np.sqrt((resid**2).mean()))


def expected_pooled():
    """OLS over the union of the accepted sensors' (x, y) pairs: S1, S4, S8."""
    xs, ys = [], []
    for site_id in ("purpleair:1", "purpleair:4", "purpleair:8"):
        for hour in fit_hours(100):
            if hour == FLAGGED_MONITOR_HOUR:
                continue
            y = monitor_value(hour)
            xs.append(RELATIONS[site_id](y))
            ys.append(y)
        if site_id == "purpleair:1":  # the extra in-window boundary hour
            y = monitor_value(AS_OF - 30 * DAY)
            xs.append(RELATIONS[site_id](y))
            ys.append(y)
    return ols(xs, ys), len(xs)


@pytest.fixture
def fits(archive_uri, settings) -> dict[str, CalibrationFit]:
    return {f.site_id: f for f in fit_calibrations(archive_uri, AS_OF, settings)}


# --- Pairs and acceptance -----------------------------------------------------


# @spec CAL-FIT-001
# @spec CAL-IN-002
def test_pairs_are_hours_with_both_sides_trusted_inside_the_half_open_window(fits):
    # 100 fit hours + the as_of-30d boundary hour, minus the flagged monitor hour
    assert fits["purpleair:1"].n_pairs == 100
    # the hour at as_of and the hour before as_of-30d were written but fall outside [start, end)
    assert fits["purpleair:8"].n_pairs == 99
    assert fits["purpleair:4"].n_pairs == 99


# @spec CAL-FIT-002
# @spec CAL-FIT-003
def test_accepted_fits_recover_the_generating_relation(fits):
    for site_id, (slope, intercept) in {
        "purpleair:1": (1.5, 2.0),
        "purpleair:8": (1.2, 1.0),
        "purpleair:4": (1.0, 0.0),
    }.items():
        fit = fits[site_id]
        assert fit.status == FitStatus.fitted, site_id
        assert fit.slope == pytest.approx(slope)
        assert fit.intercept == pytest.approx(intercept)
        assert fit.ref_site_id == M1[0]


# @spec CAL-FIT-003
def test_rejection_reasons_each_route_to_pooled(fits):
    assert fits["purpleair:2"].status == FitStatus.pooled  # 49 pairs < 72
    assert fits["purpleair:6"].status == FitStatus.pooled  # negative slope
    assert fits["purpleair:7"].status == FitStatus.pooled  # constant input
    assert fits["purpleair:3"].status == FitStatus.pooled  # no monitor in range
    assert fits["purpleair:5"].status == FitStatus.pooled  # no observations


# @spec CAL-FIT-003
def test_min_pairs_is_the_gate(archive_uri):
    fits = {
        f.site_id: f
        for f in fit_calibrations(archive_uri, AS_OF, CalibrationSettings(min_pairs=40))
    }
    assert fits["purpleair:2"].status == FitStatus.fitted
    assert fits["purpleair:2"].slope == pytest.approx(1.0)
    strict = {
        f.site_id: f
        for f in fit_calibrations(archive_uri, AS_OF, CalibrationSettings(min_pairs=101))
    }
    assert all(f.status != FitStatus.fitted for f in strict.values())


# @spec CAL-FIT-004
# @spec CAL-FIT-005
def test_pooled_fit_is_ols_over_the_union_of_accepted_pairs(fits):
    (slope, intercept, r2, rmse), n = expected_pooled()
    for site_id in ("purpleair:2", "purpleair:3", "purpleair:5", "purpleair:6", "purpleair:7"):
        fit = fits[site_id]
        assert fit.status == FitStatus.pooled
        assert fit.slope == pytest.approx(slope)
        assert fit.intercept == pytest.approx(intercept)
        assert fit.r2 == pytest.approx(r2)
        assert fit.rmse == pytest.approx(rmse)
        assert fit.n_pairs == n
        assert fit.ref_site_id is None and fit.distance_m is None


# @spec CAL-FIT-004
def test_pooled_fit_is_held_to_the_slope_gate(archive_uri, settings, monkeypatch):
    # Per-sensor fits see at most ~100 pairs; the pooled fit sees the union (~300). Invert only
    # the pooled slope so the per-sensor fits stay accepted while the pooled fit fails its gate.
    real = fit_module.ols

    def pooled_inverted(x, y):
        slope, intercept, r2, rmse = real(x, y)
        return (-slope if len(x) > 200 else slope), intercept, r2, rmse

    monkeypatch.setattr(fit_module, "ols", pooled_inverted)
    fits = {f.site_id: f for f in fit_calibrations(archive_uri, AS_OF, settings)}
    assert {s for s, f in fits.items() if f.status == FitStatus.fitted} == {
        "purpleair:1",
        "purpleair:4",
        "purpleair:8",
    }
    assert all(
        f.status == FitStatus.uncalibrated
        for s, f in fits.items()
        if s not in ("purpleair:1", "purpleair:4", "purpleair:8")
    )


# @spec CAL-FIT-006
def test_no_accepted_fit_leaves_every_sensor_uncalibrated(archive_uri):
    fits = {
        f.site_id: f
        for f in fit_calibrations(archive_uri, AS_OF, CalibrationSettings(min_pairs=500))
    }
    assert {f.status for f in fits.values()} == {FitStatus.uncalibrated}
    for fit in fits.values():
        assert fit.slope is None and fit.intercept is None
        assert fit.r2 is None and fit.rmse is None


# @spec CAL-FIT-007
def test_diagnostics_are_computed_over_the_fits_own_pairs(fits):
    fit = fits["purpleair:1"]
    assert fit.r2 == pytest.approx(1.0)
    assert fit.rmse == pytest.approx(0.0, abs=1e-9)
    (_, _, r2, rmse), _ = expected_pooled()
    assert fits["purpleair:2"].r2 == pytest.approx(r2)
    assert fits["purpleair:2"].rmse == pytest.approx(rmse)


# @spec CAL-FIT-008
def test_window_bounds_are_recorded(fits):
    for fit in fits.values():
        assert fit.window_start == AS_OF - 30 * DAY
        assert fit.window_end == AS_OF
        assert fit.as_of == AS_OF


# @spec CAL-FIT-009
def test_r2_is_not_a_gate(archive_uri, settings, tmp_path):
    # add noise to M1 so S1's fit has a poor r2; it must still be accepted (slope stays positive)
    noisy = []
    for k, hour in enumerate(fit_hours(100)):
        noisy.append(monitor_obs(M1, hour, monitor_value(hour) + (25.0 if k % 2 else -25.0)))
    write_observations(noisy, archive_uri)
    fits = {f.site_id: f for f in fit_calibrations(archive_uri, AS_OF, settings)}
    assert fits["purpleair:1"].r2 < 0.5
    assert fits["purpleair:1"].status == FitStatus.fitted


# @spec CAL-FIT-010
def test_exactly_one_fit_row_per_sensor(fits):
    assert set(fits) == set(SENSORS)
    assert all(f.status in set(FitStatus) for f in fits.values())
    assert all(f.as_of == AS_OF for f in fits.values())


# --- Run ----------------------------------------------------------------------


# @spec CAL-RUN-001
def test_fit_run_writes_sensor_hours_and_fits(archive_uri, settings):
    fit_calibrations(archive_uri, AS_OF, settings)
    fits = read_partitioned(archive_uri, FITS)
    assert len(fits) == len(SENSORS)
    hourly = read_partitioned(archive_uri, SENSOR_HOURLY)
    assert set(hourly["site_id"]) == set(RELATIONS)
    assert hourly["hour"].min() == AS_OF - 30 * DAY
    assert hourly["hour"].max() == AS_OF - H


# @spec CAL-RUN-001
def test_fit_run_loads_partitions_when_given_a_connection(archive_uri, settings, monkeypatch):
    loaded = []
    monkeypatch.setattr(fit_module, "load_partitions", lambda c, u, uris: loaded.append(list(uris)))
    conn = object()
    fit_calibrations(archive_uri, AS_OF, settings, conn=conn)
    (uris,) = loaded
    assert any("/calibration/fits/as_of=2026-09-20T00/fits.parquet" in u for u in uris)
    assert any("/calibration/sensor_hourly/date=" in u for u in uris)


# @spec CAL-IN-003
def test_fit_reads_the_archive_not_postgis(archive_uri, settings, monkeypatch):
    import aqdt.observation_store.postgis as postgis

    monkeypatch.setattr(
        postgis, "connect", lambda: pytest.fail("calibration must not open PostGIS")
    )
    fit_calibrations(archive_uri, AS_OF, settings)


# @spec CAL-RUN-003
def test_as_of_validation(archive_uri, settings):
    with pytest.raises(Exception):
        fit_calibrations(archive_uri, datetime(2026, 9, 20), settings)
    fits = fit_calibrations(archive_uri, AS_OF + timedelta(minutes=30), settings)
    assert all(f.as_of == AS_OF for f in fits)


# @spec CAL-RUN-004
def test_fit_run_is_deterministic(archive_uri, settings):
    first = fit_calibrations(archive_uri, AS_OF, settings)
    path = f"{archive_uri}/calibration/fits/as_of=2026-09-20T00/fits.parquet"
    before = open(path, "rb").read()
    second = fit_calibrations(archive_uri, AS_OF, settings)
    assert first == second
    assert open(path, "rb").read() == before


# @spec CAL-RUN-005
def test_refit_after_archive_change_replaces_the_fit(archive_uri, settings):
    fit_calibrations(archive_uri, AS_OF, settings)
    # M1 revises every hour upward by 10: S1's intercept moves from 2 to 12
    revised = [monitor_obs(M1, hour, monitor_value(hour) + 10) for hour in fit_hours(100)]
    write_observations(revised, archive_uri)
    fit_calibrations(archive_uri, AS_OF, settings)
    fits = read_partitioned(archive_uri, FITS)
    s1 = fits[fits["site_id"] == "purpleair:1"]
    assert len(s1) == 1
    assert s1.iloc[0]["intercept"] == pytest.approx(12.0)


# @spec CAL-RUN-006
def test_empty_window_writes_every_sensor_as_uncalibrated(archive_uri, settings):
    fits = fit_calibrations(archive_uri, datetime(2020, 1, 1, tzinfo=AS_OF.tzinfo), settings)
    assert {f.site_id for f in fits} == set(SENSORS)
    assert {f.status for f in fits} == {FitStatus.uncalibrated}
    stored = read_partitioned(archive_uri, FITS, as_of="2020-01-01T00")
    assert len(stored) == len(SENSORS)
