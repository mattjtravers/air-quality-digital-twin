"""Sensor influence at a hotspot: one hour re-kriged without each sensor near a centre, beside the
evidence for judging each of those sensors (evaluation LLD § Sensor influence at a hotspot)."""

from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from pyproj import Transformer
from rasterio.transform import Affine

from aqdt.calibration.products import CALIBRATED_HOURLY, FITS
from aqdt.calibration.schemas import hour_bound
from aqdt.evaluation.matchups import cell_index
from aqdt.evaluation.schemas import InfluenceSummary
from aqdt.fusion.grid import CRS, Grid, grid_for
from aqdt.fusion.krige import predict_chunked, select_variogram
from aqdt.fusion.points import HourPoints, hour_points, hour_sensors
from aqdt.fusion.products import SURFACE_FITS
from aqdt.fusion.schemas import FusionSettings, SurfaceStatus
from aqdt.maps.render import Panel, save_figure, small_multiples
from aqdt.maps.surface import read_surface
from aqdt.observation_store.archive import (
    read_observations,
    read_partitioned,
    read_sites,
    resolve_archive_uri,
)
from aqdt.observation_store.products import render_partition_value
from aqdt.observation_store.schemas import QcFlag, SiteType, Source

log = logging.getLogger(__name__)

H = timedelta(hours=1)
#: A hotspot cell is this far above its hour's mean, µg/m³.
HOTSPOT_ABOVE_MEAN = 2.0
#: Hours of top-5% persistence, ending with the hour.
PERSISTENCE_HOURS = 168
TOP_PERCENTILE = 95
#: Weekday rush hours, Eastern, by the hour's start.
RUSH_HOURS = frozenset({7, 8, 9, 16, 17, 18})
EASTERN = "America/New_York"
MEASURES = ("centre_value", "monitor_value", "hour_mean", "peak", "peak_above_mean", "area_km2")
EVIDENCE = (
    "pm25_calibrated",
    "pm25_corrected_mean",
    "fit_status",
    "slope",
    "intercept",
    "r2",
    "n_pairs",
    "ab_snapshots",
    "channel_a_mean",
    "channel_b_mean",
    "ab_abs_diff_mean",
    "ab_rel_diff_mean",
    "ab_disagreement",
    "top5_hours_present",
    "top5_share",
    "top5_share_rush",
    "top5_share_other",
)
COLUMNS = (
    ["removed", "distance_km", *MEASURES]
    + [f"delta_{m}" for m in MEASURES]
    + ["survives", *EVIDENCE]
)
_TO_UTM = Transformer.from_crs(4326, CRS, always_xy=True)
_TO_LONLAT = Transformer.from_crs(CRS, 4326, always_xy=True)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _krige(points: HourPoints, grid: Grid, min_points: int) -> np.ndarray | None:
    """The hour's estimate on ``grid`` as fusion would krige these points, or None when they are
    too few or no variogram is eligible."""
    if points.n_points < min_points:
        return None
    variogram = select_variogram(points)
    if variogram is None:
        return None
    estimate, _ = predict_chunked(variogram, grid)
    return estimate


# @spec EVAL-INFL-004
def measure(
    estimate: np.ndarray | None,
    grid: Grid,
    centre_cell: tuple[int, int] | None,
    monitor_cell: tuple[int, int] | None,
    in_radius: np.ndarray,
) -> dict[str, float]:
    """A surface's values at the centre and the monitor, its mean, and its hotspot."""
    if estimate is None:
        return dict.fromkeys(MEASURES, math.nan)
    mean = float(np.nanmean(estimate))
    disc = estimate[in_radius]
    peak = float(np.nanmax(disc)) if disc.size else math.nan
    hot = int((disc > mean + HOTSPOT_ABOVE_MEAN).sum())
    return {
        "centre_value": float(estimate[centre_cell]) if centre_cell else math.nan,
        "monitor_value": float(estimate[monitor_cell]) if monitor_cell else math.nan,
        "hour_mean": mean,
        "peak": peak,
        "peak_above_mean": peak - mean,
        "area_km2": hot * grid.resolution_m**2 / 1e6,
    }


# @spec EVAL-INFL-006
def channel_agreement(snapshots: pd.DataFrame, site_ids: list[str]) -> pd.DataFrame:
    """Per sensor, its snapshots' channel A and B agreement over the hour."""
    rows = []
    for site_id in site_ids:
        mine = snapshots[snapshots["site_id"] == site_id]
        a = pd.to_numeric(mine["pm25_channel_a"], errors="coerce").to_numpy(float)
        b = pd.to_numeric(mine["pm25_channel_b"], errors="coerce").to_numpy(float)
        both = np.isfinite(a) & np.isfinite(b)
        diff = np.abs(a - b)[both]
        total = (a + b)[both]
        relative = diff[total > 0] / (total[total > 0] / 2)
        flags = [list(f) for f in mine["qc_flags"]]
        rows.append(
            {
                "site_id": site_id,
                "ab_snapshots": len(mine),
                "channel_a_mean": _mean(a[np.isfinite(a)]),
                "channel_b_mean": _mean(b[np.isfinite(b)]),
                "ab_abs_diff_mean": _mean(diff),
                "ab_rel_diff_mean": _mean(relative),
                "ab_disagreement": sum(QcFlag.channel_disagreement.value in f for f in flags),
            }
        )
    return pd.DataFrame(rows)


def _mean(values: np.ndarray) -> float:
    return float(values.mean()) if values.size else math.nan


def _rush(hour: pd.Timestamp) -> bool:
    local = hour.tz_convert(EASTERN)
    return local.weekday() < 5 and local.hour in RUSH_HOURS


# @spec EVAL-INFL-007
def persistence(rows: pd.DataFrame, site_ids: list[str], hour: datetime) -> pd.DataFrame:
    """Per sensor, how often it sat in the top 5% of the kriging sensors' values over the 168
    hours ending with ``hour``, overall, at weekday rush hours, and at every other hour.

    ``rows`` are kriging-sensor rows (``site_id``, ``hour``, ``pm25_calibrated``)."""
    end = pd.Timestamp(hour)
    start = end - (PERSISTENCE_HOURS - 1) * H
    window = rows[(rows["hour"] >= start) & (rows["hour"] <= end)]
    tops: dict[str, list[tuple[bool, bool]]] = {s: [] for s in site_ids}
    for when, group in window.groupby("hour", sort=True):
        values = group["pm25_calibrated"].to_numpy(float)
        cut = np.percentile(values, TOP_PERCENTILE)
        rush = _rush(pd.Timestamp(when))
        for site_id, value in zip(group["site_id"], values):
            if site_id in tops:
                tops[site_id].append((rush, bool(value >= cut)))

    def share(hours: list[bool]) -> float:
        return sum(hours) / len(hours) if hours else math.nan

    return pd.DataFrame(
        [
            {
                "site_id": site_id,
                "top5_hours_present": len(seen),
                "top5_share": share([top for _, top in seen]),
                "top5_share_rush": share([top for rush, top in seen if rush]),
                "top5_share_other": share([top for rush, top in seen if not rush]),
            }
            for site_id, seen in tops.items()
        ]
    )


def _fits(archive_uri: str, sensors: pd.DataFrame) -> pd.DataFrame:
    as_ofs = pd.to_datetime(sensors["fit_as_of"], utc=True).dropna().unique()
    rendered = {render_partition_value(pd.Timestamp(a).to_pydatetime()) for a in as_ofs}
    if not rendered:
        return pd.DataFrame(columns=["site_id", "as_of", "slope", "intercept", "r2", "n_pairs"])
    return read_partitioned(archive_uri, FITS, as_of=lambda v: v in rendered)


def _fit_evidence(sensor, fits: pd.DataFrame) -> dict:
    match = fits[
        (fits["site_id"] == sensor.site_id)
        & (pd.to_datetime(fits["as_of"], utc=True) == pd.Timestamp(sensor.fit_as_of))
    ]
    if match.empty:
        return dict.fromkeys(("slope", "intercept", "r2", "n_pairs"))
    fit = match.iloc[0]
    return {name: fit[name] for name in ("slope", "intercept", "r2", "n_pairs")}


def _kriging_rows(calibrated: pd.DataFrame, sites: pd.DataFrame, hour: datetime) -> pd.DataFrame:
    hours = sorted(pd.to_datetime(calibrated["hour"], utc=True).unique())
    frames = []
    for when in hours:
        if hour - (PERSISTENCE_HOURS - 1) * H <= when.to_pydatetime() <= hour:
            rows = hour_sensors(calibrated, sites, when.to_pydatetime())
            frames.append(rows[["site_id", "pm25_calibrated"]].assign(hour=when))
    if not frames:
        return pd.DataFrame(columns=["site_id", "hour", "pm25_calibrated"])
    return pd.concat(frames, ignore_index=True)


def _nearest_monitor(
    sites: pd.DataFrame, grid: Grid, transform: Affine, centre_xy: tuple[float, float]
) -> tuple[pd.Series | None, tuple[int, int] | None]:
    monitors = sites[sites["site_type"] == SiteType.reference_monitor.value]
    best, best_cell, best_d = None, None, math.inf
    for monitor in monitors.itertuples(index=False):
        x, y = _TO_UTM.transform(float(monitor.longitude), float(monitor.latitude))
        cell = cell_index(transform, grid.width, grid.height, x, y)
        d = math.hypot(x - centre_xy[0], y - centre_xy[1])
        if cell is not None and d < best_d:
            best, best_cell, best_d = monitor, cell, d
    return best, best_cell


def _crop(grid: Grid, centre_xy: tuple[float, float], half_m: float) -> tuple[slice, slice, Grid]:
    res = grid.resolution_m
    c0 = max(0, math.floor((centre_xy[0] - half_m - grid.x_min) / res))
    c1 = min(grid.width, math.ceil((centre_xy[0] + half_m - grid.x_min) / res))
    r0 = max(0, math.floor((grid.y_max - (centre_xy[1] + half_m)) / res))
    r1 = min(grid.height, math.ceil((grid.y_max - (centre_xy[1] - half_m)) / res))
    sub = Grid(
        x_min=grid.x_min + c0 * res,
        y_max=grid.y_max - r0 * res,
        width=c1 - c0,
        height=r1 - r0,
        resolution_m=res,
    )
    return slice(r0, r1), slice(c0, c1), sub


# @spec EVAL-INFL-009
def _figure(
    path: Path,
    grid: Grid,
    centre_xy: tuple[float, float],
    radius_m: float,
    panels: list[tuple[str, np.ndarray, pd.DataFrame]],
    monitors_xy: tuple[np.ndarray, np.ndarray],
) -> None:
    rows, cols, sub = _crop(grid, centre_xy, 2 * radius_m)
    drawn = []
    for title, estimate, sensors in panels:
        array = estimate[rows, cols] - float(np.nanmean(estimate))
        drawn.append(
            Panel(
                array=array,
                title=title,
                sensors=(sensors["x"].to_numpy(float), sensors["y"].to_numpy(float)),
                monitors=monitors_xy,
            )
        )
    fig, _ = small_multiples(drawn, sub, kind="diff", ncols=2)
    for ax in fig.axes[: len(drawn)]:
        ax.add_patch(
            plt.Circle(
                (centre_xy[0] / 1000, centre_xy[1] / 1000),
                radius_m / 1000,
                fill=False,
                linestyle="--",
                linewidth=0.7,
                edgecolor="black",
            )
        )
        ax.plot(centre_xy[0] / 1000, centre_xy[1] / 1000, marker="+", color="black", markersize=6)
    save_figure(fig, path)
    plt.close(fig)


def _clean(value):
    if isinstance(value, (float, np.floating)):
        return None if not math.isfinite(float(value)) else round(float(value), 4)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


# @spec EVAL-INFL-001, EVAL-INFL-002, EVAL-INFL-003, EVAL-INFL-005, EVAL-INFL-008
# @spec EVAL-INFL-010
def sensor_influence(
    archive_uri: str | None,
    hour: datetime,
    out_dir: str | Path,
    *,
    center: tuple[float, float] | None = None,
    radius_km: float = 5.0,
) -> InfluenceSummary:
    """Re-krige ``hour`` without each kriging sensor within ``radius_km`` of ``center`` and report
    what each removal does to the hotspot there, with the evidence on each sensor."""
    hour = hour_bound(hour, "hour")
    if not radius_km > 0:
        raise ValueError(f"radius_km must be positive, got {radius_km}")
    archive_uri = resolve_archive_uri(archive_uri)
    fits = read_partitioned(archive_uri, SURFACE_FITS, date=render_partition_value(hour.date()))
    fit = fits[fits["hour"] == hour] if not fits.empty else fits
    if fit.empty or fit.iloc[0]["status"] != SurfaceStatus.kriged.value:
        status = "no surface_fits row" if fit.empty else f"status {fit.iloc[0]['status']}"
        raise RuntimeError(f"hour {_iso(hour)} is not kriged ({status}); nothing to re-krige")
    fit = fit.iloc[0]
    stored = read_surface(fit["surface_uri"])
    settings = FusionSettings()
    grid = grid_for(settings.bbox, float(fit["resolution_m"]))
    transform = Affine.from_gdal(*grid.transform)

    if center is None:
        row, column = np.unravel_index(np.nanargmax(stored.pm25), stored.pm25.shape)
        x, y = stored.transform @ (column + 0.5, row + 0.5)
        lon, lat = _TO_LONLAT.transform(x, y)
        center = (float(lat), float(lon))
    centre_xy = _TO_UTM.transform(center[1], center[0])
    radius_m = radius_km * 1000

    first = (hour - (PERSISTENCE_HOURS - 1) * H).date()
    days = (hour.date() - first).days + 1
    dates = {render_partition_value(first + timedelta(days=k)) for k in range(days)}
    calibrated = read_partitioned(archive_uri, CALIBRATED_HOURLY, date=lambda d: d in dates)
    sites = read_sites(archive_uri)
    sensors = hour_sensors(calibrated, sites, hour)
    distance = np.hypot(
        sensors["x"].to_numpy(float) - centre_xy[0], sensors["y"].to_numpy(float) - centre_xy[1]
    )
    sensors = sensors.assign(distance_km=distance / 1000)
    near = sensors[distance <= radius_m].sort_values(["distance_km", "site_id"], kind="stable")

    xs, ys = grid.cell_centres()
    in_radius = (
        np.hypot(xs - centre_xy[0], ys - centre_xy[1]).reshape(grid.height, grid.width) <= radius_m
    )
    centre_cell = cell_index(transform, grid.width, grid.height, *centre_xy)
    monitor, monitor_cell = _nearest_monitor(sites, grid, transform, centre_xy)
    observed = read_observations(archive_uri, Source.airnow, hour, hour + H)
    monitor_obs = None
    if monitor is not None:
        mine = observed[observed["site_id"] == monitor.site_id]
        if not mine.empty and pd.notna(mine.iloc[0]["pm25_raw"]):
            monitor_obs = float(mine.iloc[0]["pm25_raw"])

    log.info(
        "influence %s: centre %.5f, %.5f; %d sensors within %.1f km",
        _iso(hour),
        center[0],
        center[1],
        len(near),
        radius_km,
    )
    control_surface = _krige(hour_points(calibrated, sites, hour), grid, settings.min_points)
    control = measure(control_surface, grid, centre_cell, monitor_cell, in_radius)
    rows: list[dict] = [{"removed": "", "distance_km": math.nan, **control}]
    surfaces: dict[str, np.ndarray] = {}
    for sensor in near.itertuples(index=False):
        reduced = calibrated[calibrated["site_id"] != sensor.site_id]
        surface = _krige(hour_points(reduced, sites, hour), grid, settings.min_points)
        measured = measure(surface, grid, centre_cell, monitor_cell, in_radius)
        if surface is not None:
            surfaces[sensor.site_id] = surface
        log.info(
            "  without %s: centre %.2f (Δ %+.2f), area %.2f km²",
            sensor.site_id,
            measured["centre_value"],
            measured["centre_value"] - control["centre_value"],
            measured["area_km2"],
        )
        rows.append(
            {
                "removed": sensor.site_id,
                "distance_km": float(sensor.distance_km),
                **measured,
                **{f"delta_{m}": measured[m] - control[m] for m in MEASURES},
                "survives": (
                    bool(measured["area_km2"] > 0)
                    if math.isfinite(measured["area_km2"])
                    else None
                ),
            }
        )

    site_ids = list(near["site_id"])
    if site_ids:
        fits_used = _fits(archive_uri, near)
        snapshots = read_observations(archive_uri, Source.purpleair, hour, hour + H)
        agreement = channel_agreement(snapshots, site_ids).set_index("site_id")
        persisted = persistence(_kriging_rows(calibrated, sites, hour), site_ids, hour)
        persisted = persisted.set_index("site_id")
        calibrated_now = calibrated[calibrated["hour"] == hour].set_index("site_id")
        for row, sensor in zip(rows[1:], near.itertuples(index=False)):
            mine = calibrated_now.loc[sensor.site_id]
            row |= {
                "pm25_calibrated": float(mine["pm25_calibrated"]),
                "pm25_corrected_mean": float(mine["pm25_corrected_mean"]),
                "fit_status": str(mine["fit_status"]),
                **_fit_evidence(sensor, fits_used),
                **agreement.loc[sensor.site_id].to_dict(),
                **persisted.loc[sensor.site_id].to_dict(),
            }

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    table = pd.DataFrame(rows).reindex(columns=COLUMNS)
    table.to_csv(out_dir / "influence.csv", index=False)

    removals = [r for r in rows[1:] if math.isfinite(r["delta_centre_value"])]
    most = max(removals, key=lambda r: abs(r["delta_centre_value"]), default=None)
    most_influential = None
    if most is not None:
        most_influential = {
            "removed": most["removed"],
            "distance_km": _clean(most["distance_km"]),
            **{f"delta_{m}": _clean(most[f"delta_{m}"]) for m in MEASURES},
            "survives": most["survives"],
        }

    monitors = sites[sites["site_type"] == SiteType.reference_monitor.value]
    monitors_xy = _TO_UTM.transform(
        monitors["longitude"].to_numpy(float), monitors["latitude"].to_numpy(float)
    )
    panels = []
    if control_surface is not None:
        panels.append((f"All {len(sensors)} kriging sensors", control_surface, sensors))
    if most is not None:
        title = f"Without {most['removed']} (Δ centre {most['delta_centre_value']:+.1f} µg/m³)"
        others = sensors[sensors["site_id"] != most["removed"]]
        panels.append((title, surfaces[most["removed"]], others))
    if panels:
        _figure(
            out_dir / "influence.png",
            grid,
            centre_xy,
            radius_m,
            panels,
            (np.asarray(monitors_xy[0]), np.asarray(monitors_xy[1])),
        )

    survives = [r["survives"] for r in removals]
    summary = InfluenceSummary(
        hour=hour,
        center=(float(center[0]), float(center[1])),
        radius_km=float(radius_km),
        out_dir=out_dir,
        monitor={
            "site_id": None if monitor is None else monitor.site_id,
            "name": None if monitor is None else monitor.name,
            "pm25_raw": monitor_obs,
        },
        control={m: _clean(control[m]) for m in MEASURES},
        most_influential=most_influential,
        n_removed=len(near),
        survives_every_removal=all(survives),
    )
    (out_dir / "influence.json").write_text(
        json.dumps(
            {
                "hour": _iso(hour),
                "center": {"lat": summary.center[0], "lon": summary.center[1]},
                "radius_km": summary.radius_km,
                "monitor": summary.monitor,
                "control": summary.control,
                "most_influential": summary.most_influential,
                "n_removed": summary.n_removed,
                "survives_every_removal": summary.survives_every_removal,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    return summary
