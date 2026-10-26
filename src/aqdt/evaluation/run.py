"""``evaluate``: score every stored surface in a window against the held-out AirNow monitors,
add the calibration uncertainty to the kriging variance, and write the report."""

from __future__ import annotations

import hashlib
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from pyproj import Transformer

from aqdt.calibration.products import CALIBRATED_HOURLY
from aqdt.calibration.schemas import CalibrationSettings, hour_bound
from aqdt.evaluation import (
    anomaly,
    baselines,
    budget,
    diagnostics,
    figures,
    lomo,
    report,
    spikes,
)
from aqdt.evaluation.calse import POOLED, rebuild_fits
from aqdt.evaluation.decompose import REFERENCE_SD, detector_term, split_residual
from aqdt.evaluation.matchups import cell_index
from aqdt.evaluation.metrics import matchup_metrics, metric_table, pooled_bands, tertile_bands
from aqdt.evaluation.montecarlo import propagate, select_mc_hours
from aqdt.evaluation.schemas import EvaluationSettings, EvaluationSummary
from aqdt.evaluation.sensitivity import variogram_sensitivity
from aqdt.evaluation.totvar import (
    aggregation_matrix,
    cal_variance,
    ok_weights,
    pykrige_parameters,
    sensor_covariance,
)
from aqdt.fusion.grid import CRS, Grid
from aqdt.fusion.points import HourPoints, hour_points, hour_sensors
from aqdt.fusion.products import SURFACE_FITS
from aqdt.fusion.schemas import FusionSettings
from aqdt.maps.render import Panel, save_figure, small_multiples
from aqdt.maps.settings import MapSettings
from aqdt.maps.surface import SurfaceArrays, read_surface
from aqdt.observation_store.archive import (
    partition_objects,
    read_observations,
    read_partitioned,
    read_sites,
    resolve_archive_uri,
)
from aqdt.observation_store.products import OBSERVATIONS, render_partition_value
from aqdt.observation_store.schemas import SiteType, Source

log = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
_TO_UTM = Transformer.from_crs(4326, CRS, always_xy=True)
_READ_BATCH = 16
MATCHUP_COLUMNS = [
    "hour",
    "site_id",
    "name",
    "obs",
    "qc_flags",
    "flagged",
    "surface",
    "variance",
    "sd",
    "error",
    "z",
    "nearest_sensor_km",
    "n_sensors",
    "pooled_share",
    "fitted_share",
    "pooled_share_10km",
    "points_mean",
    "cal_variance",
    "total_variance",
    "total_sd",
    "z_total",
    "cal_variance_raw",
    "z_total_raw",
    "inputs_reproduced",
    "hour_mean",
    "hour_median",
    "idw",
    "rf",
    "hour_mean_points_band",
    "hour_mean_obs_band",
    "spike_hour",
    "spike_day",
    "variogram",
]
POOLED_RADIUS_M = 10_000


@dataclass
class _Hour:
    """What one kriged hour contributes beyond its matchups."""

    hour: pd.Timestamp
    fit: Any
    sensors: pd.DataFrame
    points: HourPoints
    monitors: pd.DataFrame  # the hour's matchup monitors: site_id, x, y
    reproduced: bool
    sha_matches: bool
    max_pm25: float
    max_sd: float
    finite_cells: int
    cells_above_pm25: int
    cells_above_sd: int


def _date_filter(start: datetime, end: datetime):
    first = render_partition_value(start.date())
    last = render_partition_value((end - timedelta(microseconds=1)).date())
    return lambda d: first <= d <= last


def _monitor_sites(sites: pd.DataFrame) -> pd.DataFrame:
    monitors = sites[sites["site_type"] == SiteType.reference_monitor.value]
    x, y = _TO_UTM.transform(monitors["longitude"].to_numpy(), monitors["latitude"].to_numpy())
    return pd.DataFrame(
        {
            "site_id": monitors["site_id"].to_numpy(),
            "name": monitors["name"].to_numpy(),
            "x": x,
            "y": y,
        }
    ).sort_values("site_id", ignore_index=True)


def _read_surfaces(uris: list[str]):
    """Rasters in order, read a batch at a time so S3 latency overlaps but memory stays bounded."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        for k in range(0, len(uris), _READ_BATCH):
            yield from pool.map(read_surface, uris[k : k + _READ_BATCH])


def _nearest_km(points: HourPoints, x: float, y: float) -> float:
    if points.n_points == 0:
        return math.nan
    return float(np.min(np.hypot(points.x - x, points.y - y)) / 1000)


# @spec EVAL-MATCH-007
def _pooled_near(sensors: pd.DataFrame, x: float, y: float) -> float:
    """The pooled fraction of the kriging-input sensors within 10 km of ``(x, y)``."""
    if not len(sensors):
        return math.nan
    near = np.hypot(
        sensors["x"].to_numpy(dtype=float) - x, sensors["y"].to_numpy(dtype=float) - y
    ) <= POOLED_RADIUS_M
    if not near.any():
        return math.nan
    return float((sensors.loc[near, "fit_status"] == "pooled").mean())


def _score_hour(
    fit, surface: SurfaceArrays, calibrated, sites, monitor_sites, observations, map_settings
) -> tuple[list[dict], _Hour, list[dict]]:
    """The hour's matchups, its context, and per-monitor accounting rows."""
    hour = pd.Timestamp(fit.hour)
    sensors = hour_sensors(calibrated, sites, hour.to_pydatetime())
    points = hour_points(calibrated, sites, hour.to_pydatetime())
    reproduced = points.n_sensors == int(fit.n_sensors) and points.n_points == int(fit.n_points)
    sha_matches = hashlib.sha256(surface.data).hexdigest() == fit.surface_sha256
    pooled_share = float((sensors["fit_status"] == "pooled").mean()) if len(sensors) else math.nan
    fitted_share = float((sensors["fit_status"] == "fitted").mean()) if len(sensors) else math.nan
    height, width = surface.pm25.shape
    obs = observations[observations["observed_at"] == hour].merge(
        monitor_sites, on="site_id", how="inner"
    )
    rows, monitors, accounting = [], [], []
    for o in obs.sort_values("site_id").itertuples(index=False):
        cell = cell_index(surface.transform, width, height, o.x, o.y)
        flags = list(o.qc_flags)
        status = "matchup"
        if cell is None:
            status = "outside"
        elif o.pm25_raw is None or not np.isfinite(o.pm25_raw):
            status = "null_value"
        elif not np.isfinite(surface.pm25[cell]) or not np.isfinite(surface.variance[cell]):
            status = "nan_cell"
        accounting.append({"site_id": o.site_id, "status": status, "flagged": bool(flags)})
        if status != "matchup":
            continue
        value, variance = float(surface.pm25[cell]), float(surface.variance[cell])
        sd = math.sqrt(variance)
        error = value - float(o.pm25_raw)
        rows.append(
            {
                "hour": hour,
                "site_id": o.site_id,
                "name": o.name,
                "obs": float(o.pm25_raw),
                "qc_flags": ",".join(str(f) for f in flags),
                "flagged": bool(flags),
                "surface": value,
                "variance": variance,
                "sd": sd,
                "error": error,
                "z": error / sd if sd > 0 else math.nan,
                "nearest_sensor_km": _nearest_km(points, o.x, o.y),
                "n_sensors": len(sensors),
                "pooled_share": pooled_share,
                "fitted_share": fitted_share,
                "pooled_share_10km": _pooled_near(sensors, o.x, o.y),
                "points_mean": float(points.z.mean()) if points.n_points else math.nan,
                "inputs_reproduced": reproduced,
            }
        )
        monitors.append({"site_id": o.site_id, "x": o.x, "y": o.y})
    sd_grid = np.sqrt(surface.variance)
    finite = np.isfinite(surface.pm25)
    context = _Hour(
        hour=hour,
        fit=fit,
        sensors=sensors,
        points=points,
        monitors=pd.DataFrame(monitors, columns=["site_id", "x", "y"]),
        reproduced=reproduced,
        sha_matches=sha_matches,
        max_pm25=float(np.nanmax(surface.pm25)),
        max_sd=float(np.nanmax(sd_grid)),
        finite_cells=int(finite.sum()),
        cells_above_pm25=int((surface.pm25[finite] > map_settings.pm25_max).sum()),
        cells_above_sd=int((sd_grid[finite] > map_settings.sd_max).sum()),
    )
    return rows, context, accounting


def _fit_key(row) -> tuple[str, pd.Timestamp]:
    as_of = pd.Timestamp(row.fit_as_of)
    return (row.site_id, as_of) if row.fit_status == "fitted" else (POOLED, as_of)


def _raw_matrix(cov) -> np.ndarray:
    return np.array([[cov.var_a_raw, cov.cov_ab_raw], [cov.cov_ab_raw, cov.var_b_raw]])


def _calibration_inputs(sensors: pd.DataFrame, rebuilt, stored_by_key, raw: bool = False):
    """Per-sensor arrays for the calibration covariance, or ``None`` if any fit has none;
    ``raw`` takes each fit's uncorrected covariance."""
    C, sigma2, keys, theta = [], [], [], []
    for row in sensors.itertuples(index=False):
        key = _fit_key(row)
        cov = rebuilt.covariances.get(key)
        if cov is None:
            return None
        stored = stored_by_key.get((row.site_id, pd.Timestamp(row.fit_as_of)))
        C.append(_raw_matrix(cov) if raw else cov.matrix())
        sigma2.append(cov.sigma2)
        keys.append(key[1] if key[0] == POOLED else None)
        theta.append(
            (stored.intercept, stored.slope) if stored is not None else (cov.intercept, cov.slope)
        )
    return np.array(C), np.array(sigma2), keys, np.array(theta)


# @spec EVAL-TOTVAR-001, EVAL-TOTVAR-002, EVAL-TOTVAR-003, EVAL-UNC-001
def _cal_variance(context: _Hour, rebuilt, stored_by_key, raw: bool = False) -> np.ndarray:
    n = len(context.monitors)
    inputs = _calibration_inputs(context.sensors, rebuilt, stored_by_key, raw)
    if n == 0 or inputs is None:
        return np.full(n, np.nan)
    C, sigma2, keys, _ = inputs
    fit = context.fit
    params = pykrige_parameters(fit.sill, fit.range_m, fit.nugget)
    lam = ok_weights(
        context.points.x,
        context.points.y,
        fit.variogram_model,
        params,
        context.monitors["x"].to_numpy(),
        context.monitors["y"].to_numpy(),
    )
    M = aggregation_matrix(
        context.sensors["x"], context.sensors["y"], context.points.x, context.points.y
    )
    sigma = sensor_covariance(context.sensors["pm25_corrected_mean"], C, sigma2, keys)
    return cal_variance(lam, M, sigma)


# @spec EVAL-MC-001, EVAL-MC-002, EVAL-MC-003
def _monte_carlo(contexts, matchups, rebuilt, stored_by_key, settings) -> pd.DataFrame:
    columns = ["hour", "site_id", "analytic", "monte_carlo", "monte_carlo_independent"]
    eligible = [
        c
        for c in contexts
        if c.reproduced
        and len(c.monitors)
        and matchups.loc[matchups["hour"] == c.hour, "cal_variance"].notna().all()
    ]
    if not eligible:
        return pd.DataFrame(columns=columns)
    by_hour = {c.hour: c for c in eligible}
    means = matchups[matchups["hour"].isin(by_hour)].groupby("hour")["obs"].mean()
    rng = np.random.default_rng(settings.seed)
    rows = []
    for hour in select_mc_hours(means, settings.mc_hours, settings.seed):
        c = by_hour[hour]
        C, sigma2, keys, theta = _calibration_inputs(c.sensors, rebuilt, stored_by_key)
        params = pykrige_parameters(c.fit.sill, c.fit.range_m, c.fit.nugget)
        common = (
            c.sensors["x"].to_numpy(),
            c.sensors["y"].to_numpy(),
            c.sensors["pm25_corrected_mean"].to_numpy(),
            theta[:, 0],
            theta[:, 1],
            C,
            sigma2,
            keys,
            c.fit.variogram_model,
            params,
            c.monitors["x"].to_numpy(),
            c.monitors["y"].to_numpy(),
        )
        shared = propagate(*common, draws=settings.mc_draws, rng=rng)
        independent = propagate(*common, draws=settings.mc_draws, rng=rng, shared_pooled=False)
        analytic = matchups[matchups["hour"] == hour].set_index("site_id")["cal_variance"]
        for k, site_id in enumerate(c.monitors["site_id"]):
            rows.append(
                {
                    "hour": hour,
                    "site_id": site_id,
                    "analytic": float(analytic[site_id]),
                    "monte_carlo": float(shared[k]),
                    "monte_carlo_independent": float(independent[k]),
                }
            )
    return pd.DataFrame(rows, columns=columns)


# @spec EVAL-DECOMP-001, EVAL-DECOMP-002, EVAL-DECOMP-003
# @spec EVAL-BASE-001, EVAL-BASE-002, EVAL-BASE-004
def _baselines(contexts, matchups, monitor_sites, settings) -> None:
    """Fill the matchups' ``hour_mean``, ``hour_median``, ``idw`` and ``rf`` columns from the
    hours' rebuilt kriging points."""
    if not len(matchups):
        return
    points = pd.concat(
        [
            pd.DataFrame({"hour": c.hour, "x": c.points.x, "y": c.points.y, "z": c.points.z})
            for c in contexts
            if c.points.n_points
        ],
        ignore_index=True,
    )
    by_hour = {c.hour: c.points for c in contexts}
    located = matchups[["hour", "site_id"]].merge(
        monitor_sites[["site_id", "x", "y"]], on="site_id", how="left"
    )
    levels = {h: baselines.hour_level(p.z) for h, p in by_hour.items() if p.n_points}
    matchups["hour_mean"] = [levels.get(h, (math.nan, math.nan))[0] for h in matchups["hour"]]
    matchups["hour_median"] = [levels.get(h, (math.nan, math.nan))[1] for h in matchups["hour"]]
    matchups["idw"] = [
        baselines.idw(by_hour[h].x, by_hour[h].y, by_hour[h].z, x, y)
        for h, x, y in zip(located["hour"], located["x"], located["y"])
    ]
    model = baselines.fit_forest(points, settings.seed)
    matchups["rf"] = baselines.predict_forest(model, points, located)


def _decomposition(rebuilt, stored_by_key) -> pd.DataFrame:
    fits = []
    for (site_id, as_of), cov in rebuilt.covariances.items():
        if cov is None:
            continue
        if site_id == POOLED:
            stored = next(
                (r for (s, a), r in stored_by_key.items() if a == as_of and r.status == "pooled"),
                None,
            )
        else:
            stored = stored_by_key.get((site_id, as_of))
        slope = stored.slope if stored is not None else cov.slope
        detector = detector_term(slope, rebuilt.snapshots[(site_id, as_of)])
        fits.append(
            {
                "status": "pooled" if site_id == POOLED else "fitted",
                "n": cov.n,
                "sigma2": cov.sigma2,
                "detector": 0.0 if math.isnan(detector) else detector,
            }
        )
    columns = [
        "group",
        "reference",
        "fits",
        "a",
        "b",
        "c",
        "sigma2",
        "share_a",
        "share_b",
        "share_c",
        "clamped",
    ]
    rows = []
    frame = pd.DataFrame(fits, columns=["status", "n", "sigma2", "detector"])
    for group in ("overall", "fitted", "pooled"):
        subset = frame if group == "overall" else frame[frame["status"] == group]
        for reference, ref_sd in REFERENCE_SD.items():
            parts = [split_residual(r.sigma2, r.detector, ref_sd) for r in subset.itertuples()]
            weights = subset["n"].to_numpy(dtype=float)
            if len(parts) == 0 or weights.sum() == 0:
                rows.append({"group": group, "reference": reference, "fits": 0})
                continue
            avg = {k: float(np.average([p[k] for p in parts], weights=weights)) for k in "abc"}
            sigma2 = float(np.average(subset["sigma2"], weights=weights))
            rows.append(
                {
                    "group": group,
                    "reference": reference,
                    "fits": len(parts),
                    **avg,
                    "sigma2": sigma2,
                    **_shares(avg["a"], avg["b"], sigma2),
                    "clamped": int(sum(p["clamped"] for p in parts)),
                }
            )
    return pd.DataFrame(rows, columns=columns)


def _shares(a: float, b: float, sigma2: float) -> dict[str, float]:
    """Shares of the pair-weighted residual variance: detector, then reference, the remainder
    last and never below zero."""
    share_a = min(a / sigma2, 1.0) if sigma2 > 0 else math.nan
    share_b = min(b / sigma2, 1.0 - share_a) if sigma2 > 0 else math.nan
    return {"share_a": share_a, "share_b": share_b, "share_c": max(0.0, 1.0 - share_a - share_b)}


def _variogram_class(context) -> str:
    fit = context.fit
    return diagnostics.variogram_class(fit.variogram_model, fit.sill, fit.nugget, fit.range_m)


# @spec EVAL-FIG-004
def _select_map_hours(contexts, matchups) -> list[_Hour]:
    """Four hours with a regular variogram (any kriged hour when none is), one per quartile of
    the hour's mean monitor value, on distinct days when possible; every candidate hour when
    there are four or fewer."""
    regular = [c for c in contexts if _variogram_class(c) == "regular"]
    contexts = regular or contexts
    with_means = [(c, matchups.loc[matchups["hour"] == c.hour, "obs"].mean()) for c in contexts]
    with_means = [(c, m) for c, m in with_means if np.isfinite(m)] or [(c, 0.0) for c in contexts]
    if len(with_means) <= 4:
        return [c for c, _ in with_means]
    means = pd.Series([m for _, m in with_means])
    quartile = pd.qcut(means.rank(method="first"), 4, labels=False)
    chosen, days = [], set()
    for q in range(4):
        members = [with_means[i] for i in np.flatnonzero(quartile.to_numpy() == q)]
        target = float(np.median([m for _, m in members]))
        ordered = sorted(members, key=lambda cm: (abs(cm[1] - target), cm[0].hour))
        pick = next((cm for cm in ordered if cm[0].hour.date() not in days), ordered[0])
        chosen.append(pick[0])
        days.add(pick[0].hour.date())
    return sorted(chosen, key=lambda c: c.hour)


# @spec EVAL-FIG-004
def _maps(chosen: list[_Hour], matchups, map_settings, out: Path) -> dict[str, list]:
    arrays = {c.hour: read_surface(c.fit.surface_uri) for c in chosen}
    first = arrays[chosen[0].hour]
    height, width = first.pm25.shape
    grid = Grid(
        x_min=first.transform.c,
        y_max=first.transform.f,
        width=width,
        height=height,
        resolution_m=first.transform.a,
    )
    clipping = {}
    scale_10 = MapSettings(pm25_max=10.0, sd_max=map_settings.sd_max)
    for kind, name, scale in (
        ("pm25", "maps_pm25", map_settings),
        ("sd", "maps_sd", map_settings),
        ("pm25", "maps_pm25_10", scale_10),
    ):
        panels = []
        for c in chosen:
            surface = arrays[c.hour]
            mean_obs = matchups.loc[matchups["hour"] == c.hour, "obs"].mean()
            title = f"{c.hour:%Y-%m-%d %H:00} UTC · monitors {mean_obs:.1f} µg/m³"
            panels.append(
                Panel(
                    array=surface.pm25 if kind == "pm25" else np.sqrt(surface.variance),
                    title=title,
                    sensors=(c.sensors["x"].to_numpy(float), c.sensors["y"].to_numpy(float)),
                    monitors=(c.monitors["x"].to_numpy(float), c.monitors["y"].to_numpy(float)),
                )
            )
        fig, results = small_multiples(panels, grid, kind=kind, settings=scale)
        handles, labels = fig.axes[0].get_legend_handles_labels()
        fig.legend(handles, labels, loc="outside lower center", ncols=2, frameon=False)
        data = pd.DataFrame(
            {
                "hour": [c.hour for c in chosen],
                "mean_monitor_obs": [
                    matchups.loc[matchups["hour"] == c.hour, "obs"].mean() for c in chosen
                ],
                "max": [r.max for r in results],
                "clipped_fraction": [r.clipped_fraction for r in results],
            }
        )
        data.to_csv(out / f"{name}.csv", index=False)
        save_figure(fig, out / f"{name}.png")
        import matplotlib.pyplot as plt

        plt.close(fig)
        clipping.setdefault(kind, results)
    return clipping


def _availability(
    monitor_sites, accounting, contexts, rebuilt, stored_by_key, inside_lookup
) -> pd.DataFrame:
    acc = pd.DataFrame(accounting, columns=["site_id", "status", "flagged"])
    targets: dict[str, list[float]] = {}
    seen: set = set()
    for c in contexts:
        for row in c.sensors.itertuples(index=False):
            if row.fit_status != "fitted":
                continue
            key = (row.site_id, pd.Timestamp(row.fit_as_of))
            stored = stored_by_key.get(key)
            if stored is None or row.site_id in seen:
                continue
            seen.add(row.site_id)
            targets.setdefault(stored.ref_site_id, []).append(stored.distance_m)
    rows = []
    for m in monitor_sites.itertuples(index=False):
        mine = acc[acc["site_id"] == m.site_id]
        distances = targets.get(m.site_id, [])
        rows.append(
            {
                "site_id": m.site_id,
                "name": m.name,
                "inside_grid": inside_lookup(m.x, m.y),
                "observations": len(mine),
                "matchups": int((mine["status"] == "matchup").sum()),
                "null_value": int((mine["status"] == "null_value").sum()),
                "nan_cell": int((mine["status"] == "nan_cell").sum()),
                "flagged": int(mine["flagged"].sum()),
                "calibration_targets": len(distances),
                "median_target_distance_m": float(np.median(distances)) if distances else math.nan,
            }
        )
    return pd.DataFrame(rows)


def _magnitudes(
    matchups,
    median_corrected,
    rebuilt,
    decomposition,
    sensitivity,
    stored,
    observations,
    availability,
    lomo_stats,
) -> dict:
    out: dict[str, str] = {}
    table = rebuilt.table
    fitted = table[table["status"] == "fitted"]
    pooled = table[table["status"] == "pooled"]

    def at_median(rows) -> float:
        if rows.empty:
            return math.nan
        x = median_corrected
        values = rows["var_a"] + x**2 * rows["var_b"] + 2 * x * rows["cov_ab"]
        return float(np.sqrt(np.median(values)))

    def ug(value: float, text: str) -> str | None:
        return None if not np.isfinite(value) else text.format(value)

    overall = decomposition[(decomposition["group"] == "overall")]
    if len(overall) and np.isfinite(overall["a"].iloc[0]):
        out[budget.DETECTOR] = (
            f"√(a) = {math.sqrt(overall['a'].iloc[0]):.2f} µg/m³ (pair-weighted, calibrated units)"
        )
    for source, rows in ((budget.SENSOR_FIT, fitted), (budget.POOLED_FIT, pooled)):
        text = ug(at_median(rows), "median parameter SD at the median sensor value: {:.2f} µg/m³")
        if text:
            out[source] = text
    if len(table):
        out[budget.RESIDUAL] = (
            f"median σ̂ {np.sqrt(fitted['sigma2'].median()):.2f} µg/m³ (fitted), "
            f"{np.sqrt(pooled['sigma2'].median()):.2f} µg/m³ (pooled)"
        )
    if len(matchups):
        out[budget.KRIGING] = f"median kriging SD at monitors {matchups['sd'].median():.2f} µg/m³"
    rms = sensitivity["rms_difference"].dropna()
    if len(rms):
        out[budget.VARIOGRAM] = (
            f"median RMS spherical vs exponential at monitors {rms.median():.2f} µg/m³"
        )
    if len(observations):
        rate = observations["qc_flags"].map(len).gt(0).mean()
        out[budget.AIRNOW_DATA] = f"{100 * rate:.1f}% of monitor-hours in the window flagged"
    if lomo_stats["n"]:
        targets = availability["calibration_targets"].sum() if len(availability) else 0
        out[budget.DEPENDENCE] = (
            f"median |error without the monitor| − |control error| "
            f"{lomo_stats['median_difference']:+.2f} µg/m³ over {lomo_stats['n']} unflagged "
            f"monitor-hours (lomo.csv); {int(targets)} kriging-input sensors calibrated against "
            f"a scored monitor, median distance "
            f"{availability['median_target_distance_m'].median() / 1000:.1f} km "
            "(availability.csv)"
        )
    slopes = (
        stored.loc[stored["status"] == "fitted", "slope"] if len(stored) else pd.Series(dtype=float)
    )
    if len(slopes):
        out[budget.DILUTION] = (
            f"fitted slopes median {slopes.median():.2f} (5–95%: "
            f"{slopes.quantile(0.05):.2f}–{slopes.quantile(0.95):.2f})"
        )
    return out


# @spec EVAL-SPLIT-003, EVAL-SPIKE-002
def _label_hours(matchups: pd.DataFrame, contexts, affected: set) -> None:
    """The matchups' hour-level split labels: tertile bands of the hour's point mean and of its
    mean monitor obs, the spike hour and day, and the variogram class."""
    if not len(matchups):
        for column in ("hour_mean_points_band", "hour_mean_obs_band", "spike_hour", "spike_day",
                       "variogram"):
            matchups[column] = pd.Series(dtype=object)
        return
    point_means = pd.Series(
        {c.hour: float(c.points.z.mean()) for c in contexts if c.points.n_points}
    )
    obs_means = matchups.groupby("hour")["obs"].mean()
    point_bands = tertile_bands(point_means)
    obs_bands = tertile_bands(obs_means)
    matchups["hour_mean_points_band"] = matchups["hour"].map(point_bands).fillna("none")
    matchups["hour_mean_obs_band"] = matchups["hour"].map(obs_bands).fillna("none")
    affected_days = {pd.Timestamp(h).date() for h in affected}
    matchups["spike_hour"] = np.where(matchups["hour"].isin(affected), "affected", "other")
    matchups["spike_day"] = np.where(
        pd.to_datetime(matchups["hour"], utc=True).dt.date.isin(affected_days), "affected", "other"
    )
    classes = {c.hour: _variogram_class(c) for c in contexts}
    matchups["variogram"] = matchups["hour"].map(classes)


# @spec EVAL-UNC-003
def _shared_residuals(contexts, stored_by_key, observations: pd.DataFrame) -> pd.DataFrame:
    """Per kriged hour, ``pm25_calibrated − obs`` for the hour's fitted kriging-input sensors
    whose reference monitor has an unflagged reading at the hour."""
    trusted = observations[
        observations["qc_flags"].map(len).eq(0) & observations["pm25_raw"].notna()
    ]
    lookup = {
        (s, pd.Timestamp(t)): float(v)
        for s, t, v in zip(trusted["site_id"], trusted["observed_at"], trusted["pm25_raw"])
    }
    rows = []
    for c in contexts:
        fitted = c.sensors[c.sensors["fit_status"] == "fitted"]
        for row in fitted.itertuples(index=False):
            stored = stored_by_key.get((row.site_id, pd.Timestamp(row.fit_as_of)))
            ref = getattr(stored, "ref_site_id", None) if stored is not None else None
            obs = lookup.get((ref, c.hour)) if ref else None
            if obs is not None:
                rows.append({"hour": c.hour, "r": float(row.pm25_calibrated) - obs})
    return pd.DataFrame(rows, columns=["hour", "r"])


# @spec EVAL-IN-001, EVAL-IN-002, EVAL-IN-003, EVAL-MATCH-001, EVAL-MATCH-003, EVAL-MATCH-004
# @spec EVAL-MATCH-005, EVAL-MATCH-006, EVAL-AVAIL-001, EVAL-AVAIL-002, EVAL-AVAIL-003
# @spec EVAL-AVAIL-004, EVAL-FIG-001, EVAL-FIG-005, EVAL-RUN-001, EVAL-RUN-004, EVAL-BASE-003
# @spec EVAL-IN-004, EVAL-SPIKE-003, EVAL-UNC-004, EVAL-UNC-005, EVAL-SPIKE-005, EVAL-ANOM-003
def evaluate(
    archive_uri: str | None,
    start: datetime,
    end: datetime,
    out_dir: str | Path,
    settings: EvaluationSettings,
) -> EvaluationSummary:
    """Evaluate every kriged hour in ``[start, end)`` and write the report into ``out_dir``. Reads
    the archive only; writes nothing to it."""
    start, end = hour_bound(start, "start"), hour_bound(end, "end")
    if end <= start:
        raise ValueError(f"end ({end.isoformat()}) must be after start ({start.isoformat()})")
    archive_uri = resolve_archive_uri(archive_uri)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    map_settings = MapSettings()
    calibration_settings = CalibrationSettings()

    fits = read_partitioned(archive_uri, SURFACE_FITS, date=_date_filter(start, end))
    fits = fits[(fits["hour"] >= start) & (fits["hour"] < end)].sort_values("hour")
    kriged = fits[fits["status"] == "kriged"]
    sites = read_sites(archive_uri)
    monitor_sites = _monitor_sites(sites)
    # one hour each side for the timing lags; matchups use only the window's hours
    observations = read_observations(archive_uri, Source.airnow, start - HOUR, end + HOUR)
    in_window = observations[
        (observations["observed_at"] >= start) & (observations["observed_at"] < end)
    ]
    calibrated = read_partitioned(archive_uri, CALIBRATED_HOURLY, date=_date_filter(start, end))

    rows: list[dict] = []
    accounting: list[dict] = []
    contexts: list[_Hour] = []
    grid_shape = None
    fit_rows = list(kriged.itertuples(index=False))
    for fit, surface in zip(fit_rows, _read_surfaces([f.surface_uri for f in fit_rows])):
        hour_rows, context, hour_accounting = _score_hour(
            fit, surface, calibrated, sites, monitor_sites, observations, map_settings
        )
        rows += hour_rows
        accounting += hour_accounting
        contexts.append(context)
        grid_shape = grid_shape or (surface.transform, surface.pm25.shape)
        log.info("evaluated %s: %d matchups", context.hour.isoformat(), len(hour_rows))
    matchups = pd.DataFrame(rows, columns=MATCHUP_COLUMNS)

    fitted_keys, pooled_keys = set(), set()
    for c in contexts:
        for row in c.sensors.itertuples(index=False):
            key = _fit_key(row)
            (pooled_keys.add(key[1]) if key[0] == POOLED else fitted_keys.add(key))
    rebuilt = rebuild_fits(archive_uri, fitted_keys, pooled_keys, calibration_settings)
    stored_by_key = {
        (r.site_id, pd.Timestamp(r.as_of)): r for r in rebuilt.stored.itertuples(index=False)
    }

    cal, cal_raw = {}, {}
    for c in contexts:
        corrected = _cal_variance(c, rebuilt, stored_by_key)
        uncorrected = _cal_variance(c, rebuilt, stored_by_key, raw=True)
        for site_id, value, raw in zip(c.monitors["site_id"], corrected, uncorrected):
            cal[(c.hour, site_id)] = value
            cal_raw[(c.hour, site_id)] = raw
    if len(matchups):
        keys = list(zip(matchups["hour"], matchups["site_id"]))
        matchups["cal_variance"] = [cal[k] for k in keys]
        matchups["total_variance"] = matchups["variance"] + matchups["cal_variance"]
        matchups["total_sd"] = np.sqrt(matchups["total_variance"])
        matchups["z_total"] = np.where(
            matchups["total_sd"] > 0, matchups["error"] / matchups["total_sd"], np.nan
        )
        matchups["cal_variance_raw"] = [cal_raw[k] for k in keys]
        raw_sd = np.sqrt(matchups["variance"] + matchups["cal_variance_raw"])
        matchups["z_total_raw"] = np.where(raw_sd > 0, matchups["error"] / raw_sd, np.nan)
    matchups["flagged"] = matchups["flagged"].astype(bool)
    matchups["inputs_reproduced"] = matchups["inputs_reproduced"].astype(bool)

    min_points = FusionSettings().min_points
    spike_tables, spike_rows, tier_rows, affected = [], [], [], set()
    for c in contexts:
        mask = spikes.spike_mask(c.points.z)
        if not mask.any():
            continue
        affected.add(c.hour)
        spike_tables.append(spikes.spike_points(c.hour, c.sensors, c.points))
        scored = matchups[matchups["hour"] == c.hour]
        spike_rows.append(spikes.rekrige(c.hour, c.points, mask, c.monitors, scored, min_points))
        for tier, tier_mask in spikes.tier_masks(c.points.z).items():
            rows_t = spikes.rekrige(c.hour, c.points, tier_mask, c.monitors, scored, min_points)
            tier_rows.append(rows_t.assign(tier=tier))
    spike_points = (
        pd.concat(spike_tables, ignore_index=True)
        if spike_tables
        else pd.DataFrame(columns=spikes.POINT_COLUMNS)
    )
    spike_table = (
        pd.concat(spike_rows, ignore_index=True)
        if spike_rows
        else pd.DataFrame(columns=spikes.COLUMNS)
    )
    tier_table = (
        pd.concat(tier_rows, ignore_index=True)
        if tier_rows
        else pd.DataFrame(columns=["tier", *spikes.COLUMNS])
    )
    _label_hours(matchups, contexts, affected)

    shares = pd.Series(
        {
            c.hour: matchups.loc[matchups["hour"] == c.hour, "pooled_share"].iloc[0]
            for c in contexts
            if (matchups["hour"] == c.hour).any()
        }
    )
    bands = pooled_bands(shares)
    metrics = metric_table(matchups, bands, settings.seed)
    unflagged = matchups[~matchups["flagged"]]
    headline = matchup_metrics(unflagged)
    all_metrics = matchup_metrics(matchups)

    sensitivity = pd.DataFrame(
        [
            {
                "hour": c.hour,
                "selected": c.fit.variogram_model,
                **dict(
                    zip(
                        ("alternative", "rms_difference"),
                        variogram_sensitivity(
                            c.points,
                            c.fit.variogram_model,
                            pykrige_parameters(c.fit.sill, c.fit.range_m, c.fit.nugget),
                            c.monitors["x"].to_numpy(),
                            c.monitors["y"].to_numpy(),
                        ),
                    )
                ),
            }
            for c in contexts
        ],
        columns=["hour", "selected", "alternative", "rms_difference"],
    )
    mc = _monte_carlo(contexts, matchups, rebuilt, stored_by_key, settings)
    _baselines(contexts, matchups, monitor_sites, settings)
    baseline_table = (
        baselines.baseline_table(matchups, settings.seed)
        if len(matchups)
        else pd.DataFrame(columns=baselines.COLUMNS)
    )
    paired = (
        baselines.paired_table(matchups, settings.seed)
        if len(matchups)
        else pd.DataFrame(columns=baselines.PAIRED_COLUMNS)
    )
    anomaly_table = (
        anomaly.anomaly_table(matchups, settings.seed)
        if len(matchups)
        else pd.DataFrame(columns=anomaly.COLUMNS)
    )
    lomo_table = (
        lomo.leave_one_monitor_out(
            archive_uri,
            contexts,
            matchups,
            calibrated,
            sites,
            calibration_settings,
            min_points,
            settings,
        )
        if len(matchups)
        else pd.DataFrame(columns=lomo.COLUMNS)
    )
    lomo_stats = lomo.lomo_statistics(lomo_table, settings.seed)
    lomo_monitors = lomo.lomo_by_monitor(lomo_table)
    decomposition = _decomposition(rebuilt, stored_by_key)

    def inside(x: float, y: float):
        if grid_shape is None:
            return None
        transform, (height, width) = grid_shape
        return cell_index(transform, width, height, x, y) is not None

    availability = _availability(
        monitor_sites, accounting, contexts, rebuilt, stored_by_key, inside
    )
    corrected = (
        pd.concat([c.sensors["pm25_corrected_mean"] for c in contexts])
        if contexts
        else pd.Series(dtype=float)
    )
    magnitudes = _magnitudes(
        matchups,
        float(corrected.median()) if len(corrected) else np.nan,
        rebuilt,
        decomposition,
        sensitivity,
        rebuilt.stored,
        observations[observations["observed_at"].isin(kriged["hour"])]
        if len(kriged)
        else in_window,
        availability,
        lomo_stats,
    )
    error_budget = budget.error_budget(magnitudes)

    unflagged_rows = matchups[~matchups["flagged"]]
    scored = len(unflagged_rows) > 0
    residuals = _shared_residuals(contexts, stored_by_key, observations)
    shared = diagnostics.shared_offset(residuals)
    coverage_diag = (
        diagnostics.coverage_diagnostics(unflagged_rows, settings.seed, shared[0], residuals)
        if scored
        else pd.DataFrame(columns=diagnostics.COVERAGE_COLUMNS)
    )
    reliability = (
        diagnostics.reliability(unflagged_rows, settings.seed)
        if scored
        else pd.DataFrame(columns=diagnostics.RELIABILITY_COLUMNS)
    )
    conditional = (
        diagnostics.slopes(unflagged_rows, settings.seed)
        if scored
        else pd.DataFrame(columns=diagnostics.SLOPE_COLUMNS)
    )
    pooling = (
        diagnostics.pooling_regression(matchups, settings.seed)
        if scored
        else pd.DataFrame(columns=diagnostics.POOLING_COLUMNS)
    )
    per_monitor = diagnostics.per_monitor(matchups, availability)
    variogram = diagnostics.variogram_table(kriged)
    lag = diagnostics.lag_table(matchups, observations, settings.seed)
    flag_rates = diagnostics.flag_rates(
        matchups,
        observations[observations["observed_at"].isin(kriged["hour"])]
        if len(kriged)
        else in_window,
    )
    spike_summary = spikes.spike_summary(matchups, spike_table, spike_points, affected)
    spike_tiers = spikes.tier_summary(matchups, tier_table, spike_points, settings.seed)

    matchups.to_csv(out / "matchups.csv", index=False)
    metrics.to_csv(out / "metrics.csv", index=False)
    availability.to_csv(out / "availability.csv", index=False)
    rebuilt.table.to_csv(out / "fit_uncertainty.csv", index=False)
    mc.to_csv(out / "montecarlo.csv", index=False)
    lomo_table.to_csv(out / "lomo.csv", index=False)
    lomo_monitors.to_csv(out / "lomo_by_monitor.csv", index=False)
    baseline_table.to_csv(out / "baselines.csv", index=False)
    paired.to_csv(out / "baseline_paired.csv", index=False)
    decomposition.to_csv(out / "decomposition.csv", index=False)
    sensitivity.to_csv(out / "variogram_sensitivity.csv", index=False)
    variogram.to_csv(out / "variogram.csv", index=False)
    coverage_diag.to_csv(out / "coverage_diagnostics.csv", index=False)
    anomaly_table.to_csv(out / "anomaly.csv", index=False)
    reliability.to_csv(out / "reliability.csv", index=False)
    conditional.to_csv(out / "conditional_bias.csv", index=False)
    spike_points.to_csv(out / "spike_points.csv", index=False)
    spike_table.to_csv(out / "spikes.csv", index=False)
    spike_tiers.to_csv(out / "spike_tiers.csv", index=False)
    pooling.to_csv(out / "pooling.csv", index=False)
    per_monitor.to_csv(out / "per_monitor.csv", index=False)
    lag.to_csv(out / "lag.csv", index=False)
    error_budget.to_csv(out / "error_budget.csv", index=False)

    figures.error_budget(error_budget, out)
    written = ["error_budget"]
    clipping = {}
    if len(matchups):
        figures.timeseries(matchups, out)
        figures.scatter(matchups, headline["r2"], out)
        figures.coverage(headline, out)
        figures.bias_vs_pooled(matchups, out)
        figures.decomposition(decomposition, out)
        written += ["timeseries", "scatter", "coverage", "bias_vs_pooled", "decomposition"]
        if len(mc):
            figures.mc_vs_analytic(mc, out)
            written.append("mc_vs_analytic")
        if len(lomo_table):
            figures.lomo_rmse(lomo_table, out)
            written.append("lomo_rmse")
        figures.baselines_rmse(baseline_table, out)
        written.append("baselines_rmse")
        if len(reliability):
            figures.reliability(reliability, out)
            written.append("reliability")
        figures.qq_ztotal(unflagged_rows["z_total"].to_numpy(dtype=float), out)
        written.append("qq_ztotal")
        clipping = _maps(_select_map_hours(contexts, matchups), matchups, map_settings, out)
        written += ["maps_pm25", "maps_sd", "maps_pm25_10"]
    figures.write_captions(out, written)

    expected = pd.date_range(start, end, freq="h", inclusive="left")
    present = set(fits["hour"])
    gaps = [h for h in expected if h not in present]
    finite = sum(c.finite_cells for c in contexts)
    context = {
        "start": start,
        "end": end,
        "kriged_hours": len(contexts),
        "days": len({c.hour.date() for c in contexts}),
        "gaps": gaps,
        "not_kriged": fits[fits["status"] != "kriged"][["hour", "status"]],
        "not_reproduced": [c.hour for c in contexts if not c.reproduced],
        "sha_mismatch": [c.hour for c in contexts if not c.sha_matches],
        "headline": headline,
        "all": all_metrics,
        "metrics": metrics,
        "matchups": matchups,
        "availability": availability,
        "montecarlo": mc,
        "lomo": lomo_stats,
        "lomo_hours": lomo_table["hour"].nunique() if len(lomo_table) else 0,
        "baselines": baseline_table,
        "paired": paired,
        "lomo_by_monitor": lomo_monitors,
        "coverage_diagnostics": coverage_diag,
        "shared": shared,
        "conditional": conditional,
        "reliability": reliability,
        "spike_points": spike_points,
        "spike_summary": spike_summary,
        "spike_tiers": spike_tiers,
        "anomaly": anomaly_table,
        "pooling": pooling,
        "variogram": variogram,
        "lag": lag,
        "flag_rates": flag_rates,
        "per_monitor": per_monitor,
        "matchups_sha256": report.file_sha256(out / "matchups.csv"),
        "airnow_objects": partition_objects(
            archive_uri,
            OBSERVATIONS,
            source=Source.airnow.value,
            date=_date_filter(start - HOUR, end + HOUR),
        ),
        "decomposition": decomposition,
        "sensitivity": sensitivity,
        "fit_table": rebuilt.table,
        "map_settings": map_settings,
        "max_pm25": max((c.max_pm25 for c in contexts), default=math.nan),
        "max_sd": max((c.max_sd for c in contexts), default=math.nan),
        "frac_above_pm25": sum(c.cells_above_pm25 for c in contexts) / finite
        if finite
        else math.nan,
        "frac_above_sd": sum(c.cells_above_sd for c in contexts) / finite if finite else math.nan,
        "map_hours": [r for r in clipping.get("pm25", [])],
        "settings": settings,
        "archive_uri": archive_uri,
    }
    report.write_summary(out / "summary.md", context)
    report.write_run_json(out / "run.json", context)
    return EvaluationSummary(
        window_start=start,
        window_end=end,
        out_dir=out,
        kriged_hours=len(contexts),
        days=context["days"],
        headline={
            k: (None if isinstance(v, float) and math.isnan(v) else v) for k, v in headline.items()
        },
    )
