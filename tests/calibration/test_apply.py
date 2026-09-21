"""Applying fits — CAL-APPLY, CAL-RUN-002."""

from datetime import datetime

import pandas as pd
import pytest

from aqdt.calibration import apply as apply_module
from aqdt.calibration.apply import apply_calibrations
from aqdt.calibration.fit import fit_calibrations
from aqdt.calibration.products import CALIBRATED_HOURLY, FITS, SENSOR_HOURLY
from aqdt.calibration.schemas import CalibrationSettings, FitStatus
from aqdt.observation_store.archive import read_partitioned

from .conftest import AS_OF, DAY, M1, RELATIONS, H, monitor_value

# A fit applies only to hours at or after its as_of, so the fixture fits the day before AS_OF
# (the daily refit that would have been current for the hours the tests apply to).
FIT_AS_OF = AS_OF - 24 * H


@pytest.fixture
def fitted(archive_uri, settings):
    fit_calibrations(archive_uri, FIT_AS_OF, settings)
    return archive_uri


def by_site_hour(frame):
    return frame.set_index(["site_id", "hour"]).sort_index()


# @spec CAL-APPLY-001
def test_one_calibrated_row_per_sensor_hour_in_the_window(fitted, settings):
    start, end = AS_OF - 10 * H, AS_OF - 4 * H
    result = apply_calibrations(fitted, start, end, settings)
    assert set(result.columns) == {
        "site_id",
        "hour",
        "pm25_calibrated",
        "pm25_corrected_mean",
        "n_snapshots",
        "fit_as_of",
        "fit_status",
    }
    assert result["hour"].min() == start
    assert result["hour"].max() == end - H
    hourly = read_partitioned(fitted, SENSOR_HOURLY, date=lambda d: d >= "2026-09-19")
    hourly = hourly[(hourly["hour"] >= start) & (hourly["hour"] < end)]
    assert len(result) == len(hourly)
    assert set(result["site_id"]) == set(RELATIONS)  # S5 is silent and has no rows
    merged = result.merge(hourly, on=["site_id", "hour"], suffixes=("", "_h"))
    assert (merged["pm25_corrected_mean"] == merged["pm25_corrected_mean_h"]).all()
    assert (merged["n_snapshots"] == merged["n_snapshots_h"]).all()


# @spec CAL-APPLY-003
def test_fitted_and_pooled_values_apply_their_own_fit(fitted, settings):
    result = by_site_hour(apply_calibrations(fitted, AS_OF - 10 * H, AS_OF - 4 * H, settings))
    fits = read_partitioned(fitted, FITS).set_index("site_id")
    assert fits.loc["purpleair:2", "status"] == "pooled"
    slope, intercept = fits.loc["purpleair:2", "slope"], fits.loc["purpleair:2", "intercept"]
    for hour in (AS_OF - 10 * H, AS_OF - 7 * H, AS_OF - 5 * H):
        y = monitor_value(hour)
        s1 = result.loc[("purpleair:1", hour)]
        assert s1["fit_status"] == "fitted"
        assert s1["fit_as_of"] == FIT_AS_OF
        assert s1["pm25_calibrated"] == pytest.approx(y)  # 2 + 1.5 x recovers the monitor value
        s2 = result.loc[("purpleair:2", hour)]
        assert s2["fit_status"] == "pooled"
        assert s2["pm25_calibrated"] == pytest.approx(intercept + slope * s2["pm25_corrected_mean"])


# @spec CAL-APPLY-003
def test_calibrated_values_are_clamped_at_zero(fitted, settings, monkeypatch):
    real = apply_module.read_partitioned

    def negative_intercepts(uri, product, **filters):
        frame = real(uri, product, **filters)
        if product.prefix.endswith("fits"):
            frame.loc[frame["status"] != "uncalibrated", "intercept"] = -1000.0
        return frame

    monkeypatch.setattr(apply_module, "read_partitioned", negative_intercepts)
    result = apply_calibrations(fitted, AS_OF - 3 * H, AS_OF - 1 * H, settings)
    assert (result["pm25_calibrated"].dropna() == 0.0).all()


# @spec CAL-APPLY-002
def test_the_latest_fit_at_or_before_the_hour_is_used(archive_uri, settings):
    earlier, later = AS_OF - 48 * H, AS_OF
    fit_calibrations(archive_uri, earlier, settings)
    fit_calibrations(archive_uri, later, settings)
    result = by_site_hour(apply_calibrations(archive_uri, earlier - 2 * H, later + 2 * H, settings))
    s1 = result.loc["purpleair:1"]
    # hours before the earlier fit: no fit applies
    assert s1.loc[earlier - 2 * H, "fit_status"] == "uncalibrated"
    assert pd.isna(s1.loc[earlier - 2 * H, "fit_as_of"])
    # the hour exactly at as_of uses that as_of (as_of <= hour)
    assert s1.loc[earlier, "fit_as_of"] == earlier
    assert s1.loc[later - H, "fit_as_of"] == earlier  # no cap on age
    assert s1.loc[later, "fit_as_of"] == later


# @spec CAL-APPLY-004
def test_uncalibrated_hours_have_null_values(archive_uri, settings):
    # every sensor uncalibrated
    fit_calibrations(archive_uri, FIT_AS_OF, CalibrationSettings(min_pairs=500))
    result = by_site_hour(apply_calibrations(archive_uri, AS_OF - 3 * H, AS_OF - 1 * H, settings))
    assert (result["fit_status"] == "uncalibrated").all()
    assert result["pm25_calibrated"].isna().all()
    # fitted and found uncalibrated: fit_as_of is set
    assert (result["fit_as_of"] == FIT_AS_OF).all()

    never = by_site_hour(
        apply_calibrations(archive_uri, AS_OF - 30 * DAY, AS_OF - 30 * DAY + H, settings)
    )
    assert (never["fit_status"] == "uncalibrated").all()
    assert never["fit_as_of"].isna().all()  # no fit at or before this hour exists


# @spec CAL-APPLY-005
def test_reference_monitors_are_not_calibrated(fitted, settings):
    result = apply_calibrations(fitted, AS_OF - 10 * H, AS_OF - 4 * H, settings)
    assert M1[0] not in set(result["site_id"])
    assert not result["site_id"].str.startswith("airnow:").any()


# @spec CAL-RUN-002
def test_apply_run_writes_sensor_hours_and_calibrated_rows(fitted, settings, monkeypatch):
    loaded = []
    monkeypatch.setattr(
        apply_module, "load_partitions", lambda c, u, uris: loaded.append(list(uris))
    )
    start, end = AS_OF - 30 * H, AS_OF - 20 * H
    result = apply_calibrations(fitted, start, end, settings, conn=object())
    stored = read_partitioned(fitted, CALIBRATED_HOURLY)
    assert len(stored) == len(result)
    assert stored["hour"].min() == start and stored["hour"].max() == end - H
    (uris,) = loaded
    assert any("/calibration/calibrated_hourly/date=2026-09-18/" in u for u in uris)
    assert any("/calibration/sensor_hourly/date=2026-09-18/" in u for u in uris)


# @spec CAL-RUN-002
def test_apply_run_without_conn_touches_the_archive_only(fitted, settings, monkeypatch):
    monkeypatch.setattr(
        apply_module, "load_partitions", lambda *a, **k: pytest.fail("must not load")
    )
    apply_calibrations(fitted, AS_OF - 3 * H, AS_OF - 1 * H, settings)


# @spec CAL-RUN-003
def test_apply_window_validation(fitted, settings):
    with pytest.raises(Exception):
        apply_calibrations(fitted, datetime(2026, 9, 19), AS_OF, settings)
    with pytest.raises(Exception):
        apply_calibrations(fitted, AS_OF, AS_OF - H, settings)
    with pytest.raises(Exception):
        apply_calibrations(fitted, AS_OF, AS_OF, settings)


# @spec CAL-RUN-004
def test_apply_run_is_deterministic(fitted, settings):
    start, end = AS_OF - 3 * H, AS_OF - 1 * H
    first = apply_calibrations(fitted, start, end, settings)
    path = f"{fitted}/calibration/calibrated_hourly/date=2026-09-19/calibrated_hourly.parquet"
    before = open(path, "rb").read()
    second = apply_calibrations(fitted, start, end, settings)
    pd.testing.assert_frame_equal(first, second)
    assert open(path, "rb").read() == before


def test_fit_status_values_are_the_enumeration(fitted, settings):
    result = apply_calibrations(fitted, AS_OF - 3 * H, AS_OF - 1 * H, settings)
    assert set(result["fit_status"]) <= {s.value for s in FitStatus}
