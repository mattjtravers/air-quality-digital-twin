"""Sensor hourly aggregation — CAL-HOURLY, CAL-IN-001."""

from datetime import timedelta

import pandas as pd
import pytest

from aqdt.calibration.hourly import aggregate_hourly
from aqdt.observation_store.archive import read_observations
from aqdt.observation_store.frames import observations_to_frame
from aqdt.observation_store.schemas import QcFlag, Source

from .conftest import AS_OF, H, monitor_value, sensor_obs


@pytest.fixture
def hourly(archive_uri):
    observations = read_observations(archive_uri, source=Source.purpleair)
    return aggregate_hourly(observations).set_index(["site_id", "hour"])


# @spec CAL-IN-001
# @spec CAL-HOURLY-002
def test_flagged_and_uncorrected_snapshots_are_excluded_and_counted(hourly):
    hour = AS_OF - 3 * H
    row = hourly.loc[("purpleair:1", hour)]
    x = (monitor_value(hour) - 2) / 1.5
    assert row["pm25_corrected_mean"] == pytest.approx(x)  # the 999 and the None did not count
    assert row["humidity_mean"] == 50.0
    assert row["n_snapshots"] == 2
    assert row["n_flagged"] == 1


# @spec CAL-HOURLY-001
def test_snapshots_are_grouped_into_half_open_utc_hours():
    hour = AS_OF - 5 * H
    frame = observations_to_frame(
        [
            sensor_obs("purpleair:1", hour, 4.0, 0),  # exactly on the hour: inside
            sensor_obs("purpleair:1", hour, 6.0, 59),  # 59 minutes past: inside
            sensor_obs("purpleair:1", hour + H, 100.0, 0),  # next hour
        ]
    )
    result = aggregate_hourly(frame).set_index(["site_id", "hour"])
    assert result.loc[("purpleair:1", hour), "pm25_corrected_mean"] == 5.0
    assert result.loc[("purpleair:1", hour), "n_snapshots"] == 2
    assert result.loc[("purpleair:1", hour + H), "pm25_corrected_mean"] == 100.0
    assert result.index.get_level_values("hour").tz is not None


# @spec CAL-HOURLY-003
def test_hours_with_no_trusted_snapshot_have_no_row():
    hour = AS_OF - 5 * H
    frame = observations_to_frame(
        [
            sensor_obs("purpleair:1", hour, 999.0, 10, [QcFlag.out_of_range]),
            sensor_obs("purpleair:1", hour, None, 20, uncorrected=True),
            sensor_obs("purpleair:1", hour + H, 3.0, 10),
        ]
    )
    result = aggregate_hourly(frame)
    assert list(result["hour"]) == [hour + H]


# @spec CAL-HOURLY-004
def test_no_minimum_snapshot_count():
    hour = AS_OF - 5 * H
    frame = observations_to_frame([sensor_obs("purpleair:1", hour, 3.0, 10)])
    result = aggregate_hourly(frame)
    assert len(result) == 1
    assert result.iloc[0]["n_snapshots"] == 1


# @spec CAL-HOURLY-005
def test_sensor_hour_pairs_with_the_monitor_observation_at_the_same_hour(archive_uri, settings):
    from aqdt.calibration.fit import fit_calibrations

    fits = {f.site_id: f for f in fit_calibrations(archive_uri, AS_OF, settings)}
    # S1 was generated as y = 2 + 1.5 x against M1's value at the same hour; a one-hour
    # offset in pairing would destroy the exact fit.
    assert fits["purpleair:1"].slope == pytest.approx(1.5)
    assert fits["purpleair:1"].intercept == pytest.approx(2.0)
    assert fits["purpleair:1"].rmse == pytest.approx(0.0, abs=1e-9)


def test_output_columns_are_the_sensor_hourly_schema(hourly):
    assert set(hourly.reset_index().columns) == {
        "site_id",
        "hour",
        "pm25_corrected_mean",
        "humidity_mean",
        "n_snapshots",
        "n_flagged",
    }
    assert pd.api.types.is_integer_dtype(hourly["n_snapshots"])
    assert hourly.index.get_level_values("hour").min() == AS_OF - 30 * timedelta(days=1) - H
