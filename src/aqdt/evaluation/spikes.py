"""Spike points: kriging points far above the hour's other points by a robust rule, and what
removing them changes at the monitors (a diagnostic; the stored surfaces are untouched)."""

from __future__ import annotations

import numpy as np
import pandas as pd

from aqdt.evaluation.bootstrap import days, mean_interval
from aqdt.evaluation.lomo import predict_at
from aqdt.fusion.points import HourPoints

MAD_SCALE = 1.4826
MAD_MULTIPLE = 5.0
FLOOR = 20.0  # µg/m³
TIERS = ("20–50", "50–100", ">100")
TIER_COLUMNS = ["tier", "points", "hours", "n", "bias", "bias_without", "change", "change_ci_low",
                "change_ci_high"]
POINT_COLUMNS = ["hour", "site_ids", "z", "median", "mad"]
COLUMNS = [
    "hour",
    "site_id",
    "obs",
    "flagged",
    "stored_error",
    "control_error",
    "without_error",
    "delta",
]


# @spec EVAL-SPIKE-001
def spike_mask(z) -> np.ndarray:
    """Points more than 5 robust SDs (1.4826 × MAD) above the hour's median and above 20 µg/m³."""
    z = np.asarray(z, dtype=float)
    if not len(z):
        return np.zeros(0, dtype=bool)
    median = np.median(z)
    mad = np.median(np.abs(z - median))
    return (z - median > MAD_MULTIPLE * MAD_SCALE * mad) & (z > FLOOR)


# @spec EVAL-SPIKE-001
def spike_points(hour, sensors: pd.DataFrame, points: HourPoints) -> pd.DataFrame:
    """The hour's spike points with the sensors merged into each (``hour_sensors`` rows)."""
    mask = spike_mask(points.z)
    if not mask.any():
        return pd.DataFrame(columns=POINT_COLUMNS)
    median = float(np.median(points.z))
    mad = float(np.median(np.abs(points.z - median)))
    sx = np.round(sensors["x"].to_numpy(dtype=float))
    sy = np.round(sensors["y"].to_numpy(dtype=float))
    rows = []
    for i in np.flatnonzero(mask):
        at = (sx == np.round(points.x[i])) & (sy == np.round(points.y[i]))
        rows.append(
            {
                "hour": hour,
                "site_ids": ";".join(sorted(sensors.loc[at, "site_id"].astype(str))),
                "z": float(points.z[i]),
                "median": median,
                "mad": mad,
            }
        )
    return pd.DataFrame(rows, columns=POINT_COLUMNS)


def without(points: HourPoints, mask: np.ndarray) -> HourPoints:
    """``points`` with the masked points removed."""
    keep = ~np.asarray(mask, dtype=bool)
    return HourPoints(
        x=points.x[keep],
        y=points.y[keep],
        z=points.z[keep],
        n_sensors=points.n_sensors - int((~keep).sum()),
        n_points=int(keep.sum()),
    )


# @spec EVAL-SPIKE-003
def rekrige(
    hour, points: HourPoints, mask: np.ndarray, monitors: pd.DataFrame, scored: pd.DataFrame,
    min_points: int,
) -> pd.DataFrame:
    """Per matchup monitor of the hour: the error of a control re-krige on all points and of one
    without the spike points, and their difference."""
    reduced = without(points, mask)
    by_site = scored.set_index("site_id")
    rows = []
    for monitor in monitors.itertuples(index=False):
        if monitor.site_id not in by_site.index:
            continue
        m = by_site.loc[monitor.site_id]
        control = predict_at(points, monitor.x, monitor.y, min_points)
        removed = predict_at(reduced, monitor.x, monitor.y, min_points)
        control_error = np.nan if control is None else control - float(m["obs"])
        without_error = np.nan if removed is None else removed - float(m["obs"])
        rows.append(
            {
                "hour": hour,
                "site_id": monitor.site_id,
                "obs": float(m["obs"]),
                "flagged": bool(m["flagged"]),
                "stored_error": float(m["error"]),
                "control_error": control_error,
                "without_error": without_error,
                "delta": without_error - control_error,
            }
        )
    return pd.DataFrame(rows, columns=COLUMNS)


def _rmse(values) -> float:
    values = np.asarray(values, dtype=float)
    return float(np.sqrt(np.mean(values**2))) if len(values) else np.nan


# @spec EVAL-SPIKE-004
def spike_summary(
    matchups: pd.DataFrame, rows: pd.DataFrame, points: pd.DataFrame, affected: set
) -> dict:
    """Over unflagged matchups: each affected hour's bias and RMSE with the stored errors and
    with stored + Δ; each affected day's RMSE both ways; the headline over all hours and over
    the hours not affected."""
    from aqdt.evaluation.metrics import matchup_metrics

    unflagged = matchups[~matchups["flagged"].astype(bool)] if len(matchups) else matchups
    delta = {(r.hour, r.site_id): r.delta for r in rows.itertuples(index=False)}
    if len(unflagged):
        adjusted = unflagged["error"].to_numpy(dtype=float) + np.array(
            [
                0.0 if not np.isfinite(delta.get((h, s), np.nan)) else delta[(h, s)]
                for h, s in zip(unflagged["hour"], unflagged["site_id"])
            ]
        )
        frame = unflagged.assign(error_without=adjusted)
    else:
        frame = unflagged.assign(error_without=pd.Series(dtype=float))
    hours = []
    for hour in sorted(affected):
        part = frame[frame["hour"] == hour]
        hours.append(
            {
                "hour": hour,
                "n": len(part),
                "bias": float(part["error"].mean()) if len(part) else np.nan,
                "rmse": _rmse(part["error"]),
                "bias_without": float(part["error_without"].mean()) if len(part) else np.nan,
                "rmse_without": _rmse(part["error_without"]),
            }
        )
    day_labels = pd.to_datetime(frame["hour"], utc=True).dt.strftime("%Y-%m-%d")
    affected_days = sorted({pd.Timestamp(h).strftime("%Y-%m-%d") for h in affected})
    days = [
        {
            "day": day,
            "rmse": _rmse(frame.loc[day_labels == day, "error"]),
            "rmse_without": _rmse(frame.loc[day_labels == day, "error_without"]),
        }
        for day in affected_days
    ]
    unaffected = unflagged[~unflagged["hour"].isin(affected)] if len(unflagged) else unflagged
    return {
        "points": len(points),
        "hours": len(affected),
        "hour_table": pd.DataFrame(
            hours, columns=["hour", "n", "bias", "rmse", "bias_without", "rmse_without"]
        ),
        "days": pd.DataFrame(days, columns=["day", "rmse", "rmse_without"]),
        "headline_all": matchup_metrics(unflagged),
        "headline_unaffected": matchup_metrics(unaffected),
    }


# @spec EVAL-SPIKE-005
def tier(z: float) -> str:
    """A spike point's value tier: ``20–50`` up to 50, ``50–100`` up to 100, ``>100`` above."""
    if z <= 50:
        return TIERS[0]
    if z <= 100:
        return TIERS[1]
    return TIERS[2]


# @spec EVAL-SPIKE-005
def tier_masks(z) -> dict[str, np.ndarray]:
    """For each tier present among the hour's spike points, the mask of that tier's points."""
    z = np.asarray(z, dtype=float)
    mask = spike_mask(z)
    labels = np.array([tier(v) if m else "" for v, m in zip(z, mask)])
    return {t: labels == t for t in TIERS if (labels == t).any()}


# @spec EVAL-SPIKE-005
def tier_summary(
    matchups: pd.DataFrame, rows: pd.DataFrame, points: pd.DataFrame, seed: int
) -> pd.DataFrame:
    """Per tier, over the unflagged matchups of the hours holding a point of the tier: the mean
    bias with the stored errors and with ``stored + delta_tier``, and the change with its
    day-bootstrap interval. ``rows`` carries ``tier``, ``hour``, ``site_id``, and ``delta``."""
    unflagged = matchups[~matchups["flagged"].astype(bool)] if len(matchups) else matchups
    point_tiers = points["z"].map(tier) if len(points) else pd.Series(dtype=str)
    out = []
    for name in TIERS:
        in_tier = points[point_tiers == name] if len(points) else points
        hours = set(in_tier["hour"]) if len(in_tier) else set()
        part = unflagged[unflagged["hour"].isin(hours)] if len(unflagged) else unflagged
        tier_rows = rows[rows["tier"] == name] if len(rows) else rows
        delta = {(r.hour, r.site_id): r.delta for r in tier_rows.itertuples(index=False)}
        change = np.array(
            [
                0.0 if not np.isfinite(delta.get((h, s), np.nan)) else delta[(h, s)]
                for h, s in zip(part["hour"], part["site_id"])
            ],
            dtype=float,
        )
        error = part["error"].to_numpy(dtype=float)
        lo, hi = (
            mean_interval(days(part["hour"]), change, seed) if len(part) else (np.nan, np.nan)
        )
        out.append(
            {
                "tier": name,
                "points": len(in_tier),
                "hours": len(hours),
                "n": len(part),
                "bias": float(error.mean()) if len(part) else np.nan,
                "bias_without": float((error + change).mean()) if len(part) else np.nan,
                "change": float(change.mean()) if len(part) else np.nan,
                "change_ci_low": lo,
                "change_ci_high": hi,
            }
        )
    return pd.DataFrame(out, columns=TIER_COLUMNS)
