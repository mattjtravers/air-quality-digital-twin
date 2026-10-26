"""Leave one monitor out: refit the calibration without each scored monitor in turn, re-krige the
hour as fusion would, and compare the error at that monitor with a control refit on every
monitor."""

from __future__ import annotations

import logging
from datetime import timedelta

import numpy as np
import pandas as pd

from aqdt.calibration.schemas import FitStatus
from aqdt.evaluation.montecarlo import select_mc_hours
from aqdt.fusion.krige import select_variogram
from aqdt.fusion.points import HourPoints, hour_points

log = logging.getLogger(__name__)

COLUMNS = [
    "hour",
    "site_id",
    "obs",
    "flagged",
    "stored_error",
    "control_error",
    "lomo_error",
    "n_fitted_to_x",
    "n_changed",
]
_CALIBRATED = (FitStatus.fitted.value, FitStatus.pooled.value)


def select_lomo_hours(hour_means: pd.Series, n: int, seed: int) -> list:
    """``n`` hours chosen as the Monte Carlo hours are (EVAL-MC-001); none when ``n`` is 0."""
    return select_mc_hours(hour_means, n, seed) if n > 0 else []


def recalibrate(calibrated: pd.DataFrame, fits: pd.DataFrame) -> pd.DataFrame:
    """``calibrated`` with ``pm25_calibrated`` and ``fit_status`` recomputed from the refit of each
    row's own ``(site_id, fit_as_of)``; a row whose refit is missing or ``uncalibrated`` becomes
    ``uncalibrated``."""
    refits = fits[["site_id", "as_of", "status", "slope", "intercept"]].rename(
        columns={"as_of": "fit_as_of", "status": "_status"}
    )
    left = calibrated.assign(
        site_id=calibrated["site_id"].astype(object),
        fit_as_of=pd.to_datetime(calibrated["fit_as_of"], utc=True),
    )
    refits = refits.assign(
        site_id=refits["site_id"].astype(object),
        fit_as_of=pd.to_datetime(refits["fit_as_of"], utc=True),
    )
    merged = left.merge(refits, on=["site_id", "fit_as_of"], how="left")
    applicable = merged["_status"].isin(_CALIBRATED).to_numpy()
    value = merged["intercept"].to_numpy(dtype=float) + merged["slope"].to_numpy(
        dtype=float
    ) * merged["pm25_corrected_mean"].to_numpy(dtype=float)
    out = calibrated.copy()
    out["pm25_calibrated"] = np.where(applicable, np.maximum(value, 0.0), np.nan)
    out["fit_status"] = np.where(
        applicable, merged["_status"].to_numpy(dtype=object), FitStatus.uncalibrated.value
    )
    return out


def predict_at(points: HourPoints, x: float, y: float, min_points: int) -> float | None:
    """The ordinary-kriging estimate at ``(x, y)`` with the variogram fusion would select, clamped
    at 0; ``None`` when fusion would not krige the hour."""
    if points.n_points < min_points:
        return None
    variogram = select_variogram(points)
    if variogram is None:
        return None
    z, _ = variogram.kriger.execute("points", np.array([x]), np.array([y]))
    return max(float(np.ma.getdata(z)[0]), 0.0)


def _fits_frame(fits) -> pd.DataFrame:
    from aqdt.calibration.frames import records_to_frame
    from aqdt.calibration.schemas import CalibrationFit

    return records_to_frame(fits, CalibrationFit)


class _Refits:
    """Control and without-monitor refits for each ``fit_as_of``, computed once and reused."""

    def __init__(self, archive_uri: str, sites: pd.DataFrame, settings) -> None:
        self.archive_uri, self.sites, self.settings = archive_uri, sites, settings
        self._inputs: dict[pd.Timestamp, tuple[pd.DataFrame, pd.DataFrame]] = {}
        self._fits: dict[tuple[pd.Timestamp, str | None], pd.DataFrame] = {}

    def _window(self, as_of: pd.Timestamp):
        if as_of not in self._inputs:
            from aqdt.calibration.hourly import aggregate_hourly
            from aqdt.observation_store.archive import read_observations
            from aqdt.observation_store.schemas import Source

            end = as_of.to_pydatetime()
            start = end - timedelta(days=self.settings.window_days)
            sensors = read_observations(self.archive_uri, Source.purpleair, start, end)
            monitors = read_observations(self.archive_uri, Source.airnow, start, end)
            self._inputs[as_of] = (aggregate_hourly(sensors), monitors)
        return self._inputs[as_of]

    def fits(self, as_of: pd.Timestamp, without: str | None) -> pd.DataFrame:
        key = (as_of, without)
        if key not in self._fits:
            from aqdt.calibration.fit import fit_sensors

            hourly, monitors = self._window(as_of)
            if without is not None:
                monitors = monitors[monitors["site_id"] != without]
            fits = fit_sensors(hourly, monitors, self.sites, as_of.to_pydatetime(), self.settings)
            self._fits[key] = _fits_frame(fits)
        return self._fits[key]


def _fit_rows(calibrated: pd.DataFrame, fits: pd.DataFrame) -> pd.DataFrame:
    """Each of the hour's sensor rows with its refit's ``status`` and ``ref_site_id``."""
    keys = calibrated[["site_id", "fit_as_of"]].assign(
        site_id=calibrated["site_id"].astype(object),
        fit_as_of=pd.to_datetime(calibrated["fit_as_of"], utc=True),
    )
    right = fits[["site_id", "as_of", "status", "ref_site_id"]].rename(
        columns={"as_of": "fit_as_of"}
    )
    right = right.assign(
        site_id=right["site_id"].astype(object),
        fit_as_of=pd.to_datetime(right["fit_as_of"], utc=True),
    )
    return keys.merge(right, on=["site_id", "fit_as_of"], how="left")


# @spec EVAL-LOMO-001, EVAL-LOMO-002, EVAL-LOMO-003, EVAL-LOMO-004
def leave_one_monitor_out(
    archive_uri: str,
    contexts,
    matchups: pd.DataFrame,
    calibrated: pd.DataFrame,
    sites: pd.DataFrame,
    calibration_settings,
    min_points: int,
    settings,
) -> pd.DataFrame:
    """One row per (LOMO hour, matchup monitor): the stored, control, and without-monitor errors
    at the monitor (``COLUMNS``)."""
    eligible = {c.hour: c for c in contexts if c.reproduced and len(c.monitors)}
    means = matchups[matchups["hour"].isin(eligible)].groupby("hour")["obs"].mean()
    hours = select_lomo_hours(means, settings.lomo_hours, settings.seed)
    refits = _Refits(archive_uri, sites, calibration_settings)
    rows: list[dict] = []
    for hour in hours:
        context = eligible[hour]
        at_hour = calibrated[calibrated["hour"] == hour]
        as_ofs = sorted(pd.to_datetime(at_hour["fit_as_of"].dropna(), utc=True).unique())
        as_ofs = [pd.Timestamp(a) for a in as_ofs]

        def refit(without, as_ofs=as_ofs):
            frames = [refits.fits(a, without) for a in as_ofs]
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
                columns=["site_id", "as_of", "status", "slope", "intercept", "ref_site_id"]
            )

        def predict(fits, x, y, at_hour=at_hour, hour=hour):
            points = hour_points(recalibrate(at_hour, fits), sites, hour.to_pydatetime())
            return predict_at(points, x, y, min_points)

        control = refit(None)
        control_rows = _fit_rows(at_hour, control)
        scored = matchups[matchups["hour"] == hour].set_index("site_id")
        for monitor in context.monitors.itertuples(index=False):
            m = scored.loc[monitor.site_id]
            without = refit(monitor.site_id)
            without_rows = _fit_rows(at_hour, without)
            control_value = predict(control, monitor.x, monitor.y)
            lomo_value = predict(without, monitor.x, monitor.y)
            changed = (control_rows["status"].fillna("") != without_rows["status"].fillna("")) | (
                control_rows["ref_site_id"].fillna("") != without_rows["ref_site_id"].fillna("")
            )
            rows.append(
                {
                    "hour": hour,
                    "site_id": monitor.site_id,
                    "obs": float(m["obs"]),
                    "flagged": bool(m["flagged"]),
                    "stored_error": float(m["error"]),
                    "control_error": np.nan if control_value is None else control_value - m["obs"],
                    "lomo_error": np.nan if lomo_value is None else lomo_value - m["obs"],
                    "n_fitted_to_x": int(
                        (
                            (control_rows["status"] == FitStatus.fitted.value)
                            & (control_rows["ref_site_id"] == monitor.site_id)
                        ).sum()
                    ),
                    "n_changed": int(changed.sum()),
                }
            )
        log.info("leave one monitor out %s: %d monitors", hour.isoformat(), len(context.monitors))
    return pd.DataFrame(rows, columns=COLUMNS)


def lomo_statistics(table: pd.DataFrame) -> dict[str, float]:
    """Over the unflagged rows: the RMSE of each error column, and the median of
    ``|lomo_error| − |control_error|`` with the number of rows it is taken over."""
    rows = table[~table["flagged"].astype(bool)] if len(table) else table
    out: dict[str, float] = {}
    for column in ("stored_error", "control_error", "lomo_error"):
        values = rows[column].dropna().to_numpy(dtype=float) if len(rows) else np.array([])
        out[column] = float(np.sqrt((values**2).mean())) if len(values) else np.nan
    both = rows.dropna(subset=["control_error", "lomo_error"]) if len(rows) else rows
    difference = (both["lomo_error"].abs() - both["control_error"].abs()) if len(both) else []
    out["median_difference"] = float(np.median(difference)) if len(both) else np.nan
    out["n"] = len(both)
    return out
