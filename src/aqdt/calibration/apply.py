"""Applying fits: each sensor hour gets the most recent fit at or before it."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from aqdt.calibration.frames import CalibratedHourlyFrame
from aqdt.calibration.hourly import aggregate_hourly
from aqdt.calibration.products import CALIBRATED_HOURLY, FITS, SENSOR_HOURLY
from aqdt.calibration.schemas import CalibrationSettings, FitStatus, hour_bound
from aqdt.observation_store.archive import read_observations, read_partitioned, write_partitioned
from aqdt.observation_store.frames import empty_frame
from aqdt.observation_store.postgis import load_partitions
from aqdt.observation_store.products import render_partition_value
from aqdt.observation_store.schemas import Source

HOUR = timedelta(hours=1)
COLUMNS = list(CalibratedHourlyFrame.to_schema().columns)
CALIBRATED = (FitStatus.fitted.value, FitStatus.pooled.value)


# @spec CAL-APPLY-001, CAL-APPLY-002, CAL-APPLY-003, CAL-APPLY-004, CAL-APPLY-005
def apply_fits(hourly: pd.DataFrame, fits: pd.DataFrame) -> pd.DataFrame:
    """``CalibratedHourly`` rows for every ``SensorHourly`` row.

    The fit is the sensor's ``CalibrationFit`` with the greatest ``as_of <= hour`` (no age
    limit). ``pm25_calibrated = max(0, intercept + slope × pm25_corrected_mean)`` for a
    ``fitted`` or ``pooled`` fit; null, with ``fit_status = uncalibrated``, otherwise —
    ``fit_as_of`` is null only when no fit at or before the hour exists.
    """
    if len(hourly) == 0:
        return empty_frame(CalibratedHourlyFrame)
    left = hourly[["site_id", "hour", "pm25_corrected_mean", "n_snapshots"]].sort_values(
        ["hour", "site_id"], kind="stable"
    )
    right = fits[["site_id", "as_of", "status", "slope", "intercept"]].sort_values(
        ["as_of", "site_id"], kind="stable"
    )
    # the two frames may carry different string dtypes (built vs. read from Parquet)
    left = left.assign(site_id=left["site_id"].astype(object))
    right = right.assign(site_id=right["site_id"].astype(object))
    merged = pd.merge_asof(
        left, right, left_on="hour", right_on="as_of", by="site_id", direction="backward"
    )
    applicable = merged["status"].isin(CALIBRATED).fillna(False).to_numpy()
    value = merged["intercept"].to_numpy() + merged["slope"].to_numpy() * merged[
        "pm25_corrected_mean"
    ].to_numpy()
    out = pd.DataFrame(
        {
            "site_id": merged["site_id"].to_numpy(),
            "hour": merged["hour"].to_numpy(),
            "pm25_calibrated": np.where(applicable, np.maximum(value, 0.0), np.nan),
            "pm25_corrected_mean": merged["pm25_corrected_mean"].to_numpy(),
            "n_snapshots": merged["n_snapshots"].to_numpy(),
            "fit_as_of": merged["as_of"].to_numpy(),
            "fit_status": merged["status"].fillna(FitStatus.uncalibrated.value).to_numpy(),
        },
        columns=COLUMNS,
    )
    for name in ("hour", "fit_as_of"):
        out[name] = pd.to_datetime(out[name], utc=True).astype("datetime64[ns, UTC]")
    out = out.sort_values(["site_id", "hour"], kind="stable").reset_index(drop=True)
    return CalibratedHourlyFrame.validate(out)


# @spec CAL-RUN-002, CAL-RUN-003, CAL-RUN-004, CAL-STORE-005
def apply_calibrations(
    archive_uri: str,
    start: datetime,
    end: datetime,
    settings: CalibrationSettings,
    conn: Any = None,
) -> pd.DataFrame:
    """Aggregate ``[start, end)``, write its ``SensorHourly`` rows, apply the applicable fits,
    write the ``CalibratedHourly`` rows, and — given ``conn`` — load what was written."""
    start, end = hour_bound(start, "start"), hour_bound(end, "end")
    if end <= start:
        raise ValueError("end must be after start (half-open window)")
    sensors = read_observations(archive_uri, source=Source.purpleair, start=start, end=end)
    hourly = aggregate_hourly(sensors)
    partitions = write_partitioned(hourly, archive_uri, SENSOR_HOURLY)

    latest_hour = render_partition_value(end - HOUR)
    fits = read_partitioned(archive_uri, FITS, as_of=lambda rendered: rendered <= latest_hour)
    calibrated = apply_fits(hourly, fits)
    partitions += write_partitioned(calibrated, archive_uri, CALIBRATED_HOURLY)
    if conn is not None:
        load_partitions(conn, archive_uri, partitions)
    return calibrated
