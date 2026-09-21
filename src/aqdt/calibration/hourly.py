"""Sensor hourly aggregation: trusted, corrected snapshots → one row per sensor hour."""

from __future__ import annotations

import pandas as pd

from aqdt.calibration.frames import SensorHourlyFrame
from aqdt.observation_store.frames import empty_frame

COLUMNS = list(SensorHourlyFrame.to_schema().columns)


# @spec CAL-IN-001, CAL-HOURLY-001, CAL-HOURLY-002, CAL-HOURLY-003, CAL-HOURLY-004
def aggregate_hourly(observations: pd.DataFrame) -> pd.DataFrame:
    """One ``SensorHourly`` row per ``(site_id, H)`` with ``H <= observed_at < H + 1 h``.

    Only snapshots with no QC flag and a non-null ``pm25_corrected`` contribute to the means
    and to ``n_snapshots``; ``n_flagged`` counts the sensor's flagged snapshots in the hour (an
    unflagged snapshot with a null ``pm25_corrected`` is in neither). An hour with no trusted
    snapshot has no row.
    """
    if len(observations) == 0:
        return empty_frame(SensorHourlyFrame)
    frame = pd.DataFrame(
        {
            "site_id": observations["site_id"].to_numpy(),
            "hour": observations["observed_at"].dt.floor("h").to_numpy(),
            "pm25_corrected": observations["pm25_corrected"].to_numpy(),
            "humidity": observations["humidity"].to_numpy(),
            "flagged": observations["qc_flags"].map(len).gt(0).to_numpy(),
            "trusted": (
                observations["qc_flags"].map(len).eq(0) & observations["pm25_corrected"].notna()
            ).to_numpy(),
        }
    )
    keys = ["site_id", "hour"]
    trusted = (
        frame[frame["trusted"]]
        .groupby(keys, sort=True)
        .agg(
            pm25_corrected_mean=("pm25_corrected", "mean"),
            humidity_mean=("humidity", "mean"),
            n_snapshots=("pm25_corrected", "size"),
        )
    )
    if len(trusted) == 0:
        return empty_frame(SensorHourlyFrame)
    flagged = frame[frame["flagged"]].groupby(keys).size().rename("n_flagged")
    hourly = trusted.join(flagged, how="left").reset_index()
    hourly["n_flagged"] = hourly["n_flagged"].fillna(0).astype("int64")
    hourly["n_snapshots"] = hourly["n_snapshots"].astype("int64")
    hourly["hour"] = pd.to_datetime(hourly["hour"], utc=True).astype("datetime64[ns, UTC]")
    return SensorHourlyFrame.validate(hourly[COLUMNS])
