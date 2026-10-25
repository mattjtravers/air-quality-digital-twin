"""Per-sensor and pooled OLS fits against matched reference monitors, and the fit run."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from aqdt.calibration.frames import records_to_frame
from aqdt.calibration.hourly import aggregate_hourly
from aqdt.calibration.matching import match_references
from aqdt.calibration.products import FITS, SENSOR_HOURLY
from aqdt.calibration.schemas import CalibrationFit, CalibrationSettings, FitStatus, hour_bound
from aqdt.observation_store.archive import (
    read_observations,
    read_sites,
    write_partitioned,
)
from aqdt.observation_store.postgis import load_partitions
from aqdt.observation_store.schemas import Source


# @spec CAL-FIT-002, CAL-FIT-007
def ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float, float, float]:
    """Ordinary least squares ``y = intercept + slope × x``: ``(slope, intercept, r2, rmse)``,
    the diagnostics over the same pairs. ``x`` must not be constant."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    x_mean, y_mean = x.mean(), y.mean()
    sxx = float(((x - x_mean) ** 2).sum())
    if sxx == 0:
        raise ValueError("OLS is undefined on a constant input")
    slope = float(((x - x_mean) * (y - y_mean)).sum() / sxx)
    intercept = float(y_mean - slope * x_mean)
    residual = y - (intercept + slope * x)
    ss_res = float((residual**2).sum())
    ss_tot = float(((y - y_mean) ** 2).sum())
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else math.nan
    rmse = math.sqrt(ss_res / len(x))
    return slope, intercept, r2, rmse


_PAIR_COLUMNS = ["hour", "x", "y"]


def _pairs(
    hourly: pd.DataFrame, references: dict[str, pd.Series], site_id: str, ref_site_id: str | None
) -> pd.DataFrame:
    """Hours where the sensor's ``SensorHourly`` row and the monitor's trusted value both exist:
    ``hour``, ``x`` (the sensor's corrected mean), ``y`` (the reference value)."""
    if ref_site_id is None or ref_site_id not in references:
        return pd.DataFrame({c: pd.Series(dtype=float) for c in _PAIR_COLUMNS})
    sensor = hourly[hourly["site_id"] == site_id].set_index("hour")["pm25_corrected_mean"]
    joined = sensor.to_frame("x").join(references[ref_site_id].rename("y"), how="inner")
    joined.index.name = "hour"
    return joined.reset_index()[_PAIR_COLUMNS]


def _trusted_reference_values(monitors: pd.DataFrame) -> dict[str, pd.Series]:
    """Each reference monitor's trusted ``pm25_raw`` indexed by ``observed_at``."""
    trusted = monitors[monitors["qc_flags"].map(len).eq(0) & monitors["pm25_raw"].notna()]
    return {
        site_id: group.set_index("observed_at")["pm25_raw"]
        for site_id, group in trusted.groupby("site_id")
    }


@dataclass(frozen=True, eq=False)
class CalibrationPairs:
    """The matching and the pairs every fit of one window is computed from.

    ``matches`` has one row per ``low_cost_sensor`` site (``site_id``, ``ref_site_id``,
    ``distance_m``); ``pairs`` maps each of those sites to its pairs (``hour``, ``x``, ``y``);
    ``accepted`` holds the sites whose per-sensor fit passes its gates; ``pooled`` is the union of
    the accepted sites' pairs (``site_id``, ``hour``, ``x``, ``y``).
    """

    matches: pd.DataFrame
    pairs: dict[str, pd.DataFrame]
    accepted: set[str]
    pooled: pd.DataFrame


# @spec CAL-FIT-001, CAL-FIT-003, CAL-FIT-011, CAL-MATCH-002, CAL-HOURLY-005
def calibration_pairs(
    hourly: pd.DataFrame,
    monitors: pd.DataFrame,
    sites: pd.DataFrame,
    settings: CalibrationSettings,
) -> CalibrationPairs:
    """Match every sensor, build its pairs, and decide which per-sensor fits are accepted: at
    least ``min_pairs`` pairs, a non-constant input, and a positive slope."""
    references = _trusted_reference_values(monitors)
    matches = match_references(sites, set(references), settings.max_distance_m)
    pairs: dict[str, pd.DataFrame] = {}
    accepted: set[str] = set()
    pooled: list[pd.DataFrame] = []
    for row in matches.itertuples(index=False):
        ref = None if pd.isna(row.ref_site_id) else row.ref_site_id
        own = _pairs(hourly, references, row.site_id, ref)
        pairs[row.site_id] = own
        x = own["x"].to_numpy()
        if len(x) >= settings.min_pairs and np.ptp(x) > 0:
            if ols(x, own["y"].to_numpy())[0] > 0:
                accepted.add(row.site_id)
                pooled.append(own.assign(site_id=row.site_id))
    columns = ["site_id", *_PAIR_COLUMNS]
    pooled_frame = (
        pd.concat(pooled, ignore_index=True)[columns]
        if pooled
        else pd.DataFrame({c: pd.Series(dtype=float) for c in columns})
    )
    return CalibrationPairs(matches=matches, pairs=pairs, accepted=accepted, pooled=pooled_frame)


# @spec CAL-FIT-002, CAL-FIT-004, CAL-FIT-005, CAL-FIT-006, CAL-FIT-008
# @spec CAL-FIT-009, CAL-FIT-010
def fit_sensors(
    hourly: pd.DataFrame,
    monitors: pd.DataFrame,
    sites: pd.DataFrame,
    as_of: datetime,
    settings: CalibrationSettings,
) -> list[CalibrationFit]:
    """One ``CalibrationFit`` per ``low_cost_sensor`` site for ``as_of``, sorted by ``site_id``.

    Accepted sensors (``calibration_pairs``) get their own fit. Every other sensor receives the
    pooled fit — OLS over the union of accepted sensors' pairs, held to the same slope gate — or
    is ``uncalibrated`` when there is no acceptable pooled fit. Diagnostics are recorded, never
    gated on.
    """
    window_start = as_of - timedelta(days=settings.window_days)
    built = calibration_pairs(hourly, monitors, sites, settings)
    matches = built.matches
    own_pairs = {site_id: len(frame) for site_id, frame in built.pairs.items()}
    accepted: dict[str, tuple[float, float, float, float, int]] = {}
    for site_id in built.accepted:
        own = built.pairs[site_id]
        accepted[site_id] = (*ols(own["x"].to_numpy(), own["y"].to_numpy()), len(own))

    pooled: tuple[float, float, float, float, int] | None = None
    if accepted:
        x_all, y_all = built.pooled["x"].to_numpy(), built.pooled["y"].to_numpy()
        if np.ptp(x_all) > 0:
            slope, intercept, r2, rmse = ols(x_all, y_all)
            if slope > 0:
                pooled = (slope, intercept, r2, rmse, len(x_all))

    fits: list[CalibrationFit] = []
    for row in matches.itertuples(index=False):
        common: dict[str, Any] = {
            "site_id": row.site_id,
            "as_of": as_of,
            "window_start": window_start,
            "window_end": as_of,
        }
        if row.site_id in accepted:
            slope, intercept, r2, rmse, n = accepted[row.site_id]
            fits.append(
                CalibrationFit(
                    **common,
                    status=FitStatus.fitted,
                    ref_site_id=row.ref_site_id,
                    distance_m=float(row.distance_m),
                    n_pairs=n,
                    slope=slope,
                    intercept=intercept,
                    r2=r2,
                    rmse=rmse,
                )
            )
        elif pooled is not None:
            slope, intercept, r2, rmse, n = pooled
            fits.append(
                CalibrationFit(
                    **common,
                    status=FitStatus.pooled,
                    ref_site_id=None,
                    distance_m=None,
                    n_pairs=n,
                    slope=slope,
                    intercept=intercept,
                    r2=r2,
                    rmse=rmse,
                )
            )
        else:
            fits.append(
                CalibrationFit(
                    **common,
                    status=FitStatus.uncalibrated,
                    ref_site_id=None,
                    distance_m=None,
                    n_pairs=own_pairs[row.site_id],
                    slope=None,
                    intercept=None,
                    r2=None,
                    rmse=None,
                )
            )
    return fits


# @spec CAL-RUN-001, CAL-RUN-003, CAL-RUN-004, CAL-RUN-005, CAL-RUN-006, CAL-IN-002, CAL-IN-003
def fit_calibrations(
    archive_uri: str, as_of: datetime, settings: CalibrationSettings, conn: Any = None
) -> list[CalibrationFit]:
    """Aggregate the fitting window ``[as_of − window_days, as_of)``, write its ``SensorHourly``
    rows, match, fit, write the fits, and — given ``conn`` — load what was written."""
    as_of = hour_bound(as_of, "as_of")
    window_start = as_of - timedelta(days=settings.window_days)
    sensors = read_observations(archive_uri, source=Source.purpleair, start=window_start, end=as_of)
    monitors = read_observations(archive_uri, source=Source.airnow, start=window_start, end=as_of)
    sites = read_sites(archive_uri)

    hourly = aggregate_hourly(sensors)
    partitions = write_partitioned(hourly, archive_uri, SENSOR_HOURLY)
    fits = fit_sensors(hourly, monitors, sites, as_of, settings)
    partitions += write_partitioned(records_to_frame(fits, CalibrationFit), archive_uri, FITS)
    if conn is not None:
        load_partitions(conn, archive_uri, partitions)
    return fits
