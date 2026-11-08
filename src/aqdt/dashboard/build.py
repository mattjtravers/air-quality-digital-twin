"""``build_site``: the whole dashboard, rebuilt from the archive — frames, manifest, front end."""

from __future__ import annotations

import json
import logging
import math
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
from scipy.spatial import cKDTree

from aqdt.calibration.products import CALIBRATED_HOURLY
from aqdt.dashboard.frames import bounds_latlon, encode_png, to_web_mercator
from aqdt.dashboard.manifest import Hour, HourStatus, Manifest, Monitor, Scale, Scales, Sensor
from aqdt.evaluation.matchups import cell_index
from aqdt.fusion.grid import CRS
from aqdt.fusion.points import HourPoints, hour_points
from aqdt.fusion.products import SURFACE_FITS
from aqdt.fusion.schemas import SurfaceStatus
from aqdt.maps.render import colorize, colour_stops, scale
from aqdt.maps.settings import MapSettings
from aqdt.maps.surface import SurfaceArrays, read_surface
from aqdt.observation_store.archive import read_observations, read_partitioned, read_sites
from aqdt.observation_store.products import render_partition_value
from aqdt.observation_store.schemas import SiteType, Source

log = logging.getLogger(__name__)

H = timedelta(hours=1)
#: How far back the default window looks for a kriged hour (dashboard LLD § Window).
SEARCH_DAYS = 7
#: Build targets: crossing either is logged as a warning (DASH-SITE-005).
TARGET_SECONDS = 300
TARGET_BYTES = 50_000_000
WEB = Path(__file__).parent / "web"
FRAMES = "data/frames"
MANIFEST = "data/manifest.json"
UNITS = "µg/m³"
N_STOPS = 11
#: Overlay alpha (dashboard LLD § Support): a known cell, a cell no better than the hour's mean,
#: and a cell beyond the variogram range (at least MIN_RANGE_M) from every sensor.
ALPHA_MAX = 0.85
ALPHA_AT_SILL = 0.60
ALPHA_MIN = 0.40
MIN_RANGE_M = 5000.0
_READ_WORKERS = 8
_TO_UTM = Transformer.from_crs(4326, CRS, always_xy=True)


@dataclass(frozen=True)
class BuildSummary:
    window_start: datetime
    window_end: datetime  # exclusive
    latest_hour: datetime
    hours: dict[str, int]  # hour status -> count
    frames: int
    site_bytes: int
    elapsed_s: float


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hour_floor(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _date_filter(start: datetime, end: datetime):
    first = render_partition_value(start.date())
    last = render_partition_value((end - timedelta(microseconds=1)).date())
    return lambda d: first <= d <= last


def _round(value, places: int = 2) -> float | None:
    if value is None or pd.isna(value):
        return None
    value = float(value)
    return round(value, places) if math.isfinite(value) else None


def _read_fits(archive_uri: str, start: datetime, end: datetime) -> pd.DataFrame:
    fits = read_partitioned(archive_uri, SURFACE_FITS, date=_date_filter(start, end))
    if fits.empty:
        return fits
    return fits[(fits["hour"] >= start) & (fits["hour"] < end)]


# @spec DASH-WIN-002, DASH-WIN-003, DASH-WIN-004
def _resolve_window(
    archive_uri: str, hours: int, end: datetime | None, generated_at: datetime
) -> tuple[datetime, datetime, datetime]:
    """``(window_start, window_end, latest_hour)``; raises when the span holds no kriged hour."""
    if end is None:
        span = (generated_at - timedelta(days=SEARCH_DAYS), generated_at)
    else:
        window_end = _hour_floor(end)
        span = (window_end - hours * H, window_end)
    fits = _read_fits(archive_uri, *span)
    kriged = fits[fits["status"] == SurfaceStatus.kriged.value] if not fits.empty else fits
    if kriged.empty:
        raise RuntimeError(
            f"no kriged hour in surface_fits for [{_iso(span[0])}, {_iso(span[1])}); "
            "nothing to publish"
        )
    latest = pd.Timestamp(kriged["hour"].max()).to_pydatetime().astimezone(UTC)
    if end is None:
        window_end = latest + H
    return window_end - hours * H, window_end, latest


def _check_out(out: Path) -> None:
    if out.exists() and any(out.iterdir()) and not (out / MANIFEST).is_file():
        raise RuntimeError(
            f"refusing to replace {out}: it is not empty and holds no {MANIFEST} from a previous "
            "dashboard build"
        )


def _try_read(uri: str) -> SurfaceArrays | Exception:
    try:
        return read_surface(uri)
    except Exception as error:  # one unreadable raster costs one hour, not the build
        return error


def _surfaces(uris: list[str]) -> Iterator[SurfaceArrays | Exception]:
    with ThreadPoolExecutor(max_workers=_READ_WORKERS) as pool:
        yield from pool.map(_try_read, uris)


def _scales(settings: MapSettings) -> Scales:
    def one(kind: str) -> Scale:
        cmap, bottom, top, label = scale(kind, settings)
        return Scale(
            min=bottom,
            max=top,
            cmap=cmap,
            units=UNITS,
            label=label,
            stops=colour_stops(kind, N_STOPS, settings=settings),
        )

    return Scales(pm25=one("pm25"), diff=one("diff"), sd=one("sd"))


@dataclass(frozen=True)
class _MonitorHour:
    """One monitor's observation for an hour, before the hour's surface is sampled at it."""

    site_id: str
    name: str | None
    lat: float
    lon: float
    x: float  # EPSG:26918
    y: float
    obs: float | None  # unrounded
    qc_flags: list[str]


def _monitors(
    observations: pd.DataFrame, sites: pd.DataFrame
) -> dict[datetime, list[_MonitorHour]]:
    monitor_sites = sites[sites["site_type"] == SiteType.reference_monitor.value]
    joined = pd.DataFrame(observations).merge(
        monitor_sites[["site_id", "name", "latitude", "longitude"]],
        on="site_id",
        how="inner",
        suffixes=("_obs", ""),
    )
    out: dict[datetime, list[_MonitorHour]] = {}
    for row in joined.sort_values("site_id", kind="stable").itertuples(index=False):
        hour = pd.Timestamp(row.observed_at).to_pydatetime().astimezone(UTC)
        name = None if row.name is None or pd.isna(row.name) else str(row.name)
        x, y = _TO_UTM.transform(float(row.longitude), float(row.latitude))
        obs = None if row.pm25_raw is None or pd.isna(row.pm25_raw) else float(row.pm25_raw)
        out.setdefault(hour, []).append(
            _MonitorHour(
                site_id=row.site_id,
                name=name,
                lat=float(row.latitude),
                lon=float(row.longitude),
                x=float(x),
                y=float(y),
                obs=obs,
                qc_flags=[str(flag) for flag in row.qc_flags],
            )
        )
    return out


# @spec DASH-MAN-007
def monitor_check(
    monitors: list[_MonitorHour], surface: SurfaceArrays | None
) -> tuple[list[Monitor], dict]:
    """The hour's monitors with the surface's value at each one's cell and map − observed, and
    the hour's count and mean of those differences. Without a surface, no value is sampled."""
    out: list[Monitor] = []
    differences: list[float] = []
    for m in monitors:
        map_value = difference = None
        if surface is not None:
            height, width = surface.pm25.shape
            cell = cell_index(surface.transform, width, height, m.x, m.y)
            if cell is not None and np.isfinite(surface.pm25[cell]):
                map_value = float(surface.pm25[cell])
        if map_value is not None and m.obs is not None and not m.qc_flags:
            difference = map_value - m.obs
            differences.append(difference)
        out.append(
            Monitor(
                site_id=m.site_id,
                name=m.name,
                lat=round(m.lat, 5),
                lon=round(m.lon, 5),
                obs=_round(m.obs),
                qc_flags=m.qc_flags,
                map_value=_round(map_value),
                difference=_round(difference),
            )
        )
    fields = {
        "monitor_n": len(differences),
        "monitor_mean_difference": _round(np.mean(differences)) if differences else None,
    }
    return out, fields


def _sensors(calibrated: pd.DataFrame, sites: pd.DataFrame) -> dict[datetime, list[Sensor]]:
    if calibrated.empty:
        return {}
    joined = calibrated[["site_id", "hour", "pm25_calibrated", "fit_status"]].merge(
        sites[["site_id", "latitude", "longitude"]], on="site_id", how="inner"
    )
    out: dict[datetime, list[Sensor]] = {}
    for row in joined.sort_values("site_id", kind="stable").itertuples(index=False):
        hour = pd.Timestamp(row.hour).to_pydatetime().astimezone(UTC)
        out.setdefault(hour, []).append(
            Sensor(
                sensor_id=row.site_id,
                lat=round(float(row.latitude), 5),
                lon=round(float(row.longitude), 5),
                pm25_calibrated=_round(row.pm25_calibrated),
                fit_status=str(row.fit_status),
            )
        )
    return out


def _finite(array: np.ndarray) -> np.ndarray:
    return array[np.isfinite(array)]


# @spec DASH-FRAME-006
def nugget_share(nugget, sill) -> float | None:
    """The share of the hour's variance its variogram puts in the nugget: noise at a point rather
    than spatial structure. None when the sill is 0 or either value is missing."""
    if nugget is None or sill is None or pd.isna(nugget) or pd.isna(sill) or float(sill) == 0:
        return None
    return round(float(nugget) / float(sill), 2)


# @spec DASH-FRAME-007
def support_alpha(
    sd: np.ndarray, nearest_m: np.ndarray | None, range_m: float, sill: float | None
) -> np.ndarray:
    """Each cell's overlay alpha: from 0.85 at a standard deviation of 0 down to 0.60 at the
    hour's ``√sill`` and above, and 0.40 wherever the nearest kriging point (``nearest_m``,
    metres; ``None`` when the points are unknown) is farther than the variogram's range or
    5 km, whichever is longer."""
    sd = np.asarray(sd, dtype=float)
    if sill is None or not np.isfinite(sill) or sill <= 0:
        alpha = np.full(sd.shape, ALPHA_MAX)
    else:
        alpha = ALPHA_MAX - (ALPHA_MAX - ALPHA_AT_SILL) * np.minimum(sd / math.sqrt(sill), 1.0)
    if nearest_m is not None:
        alpha = np.where(np.asarray(nearest_m) > max(range_m, MIN_RANGE_M), ALPHA_MIN, alpha)
    return alpha


def _nearest_point_m(surface: SurfaceArrays, points: HourPoints) -> np.ndarray | None:
    """Distance from each cell centre to the nearest kriging point, metres."""
    if points.n_points == 0:
        return None
    height, width = surface.pm25.shape
    t = surface.transform
    xs = t.c + (np.arange(width) + 0.5) * t.a
    ys = t.f + (np.arange(height) + 0.5) * t.e
    gx, gy = np.meshgrid(xs, ys)
    distance, _ = cKDTree(np.column_stack([points.x, points.y])).query(
        np.column_stack([gx.ravel(), gy.ravel()])
    )
    return distance.reshape(height, width)


# @spec DASH-FRAME-001, DASH-FRAME-002, DASH-FRAME-005, DASH-FRAME-007
def _render_hour(
    site: Path,
    hour: datetime,
    surface: SurfaceArrays,
    settings: MapSettings,
    points: HourPoints,
    range_m: float,
    sill: float | None,
) -> dict:
    """Write the hour's three frames, faded where the estimate is least supported; return its
    surface fields for the manifest."""
    stamp = hour.strftime("%Y-%m-%dT%H")
    pm25 = _finite(surface.pm25)
    mean = pm25.mean() if pm25.size else np.nan
    sd = np.sqrt(np.clip(surface.variance, 0, None))
    alpha, _ = to_web_mercator(
        support_alpha(sd, _nearest_point_m(surface, points), range_m, sill), surface.transform
    )
    fields: dict = {}
    for kind, array in (("pm25", surface.pm25), ("diff", surface.pm25 - mean), ("sd", sd)):
        mercator, transform = to_web_mercator(array, surface.transform)
        rgba = colorize(mercator, kind=kind, settings=settings)
        rgba[..., 3] = np.where(np.isfinite(mercator), np.round(255 * alpha), 0).astype(np.uint8)
        path = f"{FRAMES}/{kind}_{stamp}.png"
        (site / path).write_bytes(encode_png(rgba))
        fields[f"{kind}_png"] = path
        fields["bounds_latlon"] = bounds_latlon(mercator.shape, transform)
    sds = _finite(sd)
    fields |= {
        "surface_min": _round(pm25.min()) if pm25.size else None,
        "surface_mean": _round(mean) if pm25.size else None,
        "surface_max": _round(pm25.max()) if pm25.size else None,
        "sd_max": _round(sds.max()) if sds.size else None,
    }
    return fields


# @spec DASH-FRAME-006, DASH-FRAME-008
def _variogram_fields(fit) -> dict:
    return {
        "variogram_model": fit.variogram_model,
        "range_km": _round(fit.range_m / 1000),
        "nugget": _round(fit.nugget, 4),
        "sill": _round(fit.sill, 4),
        "nugget_share": nugget_share(fit.nugget, fit.sill),
    }


_NO_VARIOGRAM = dict.fromkeys(("variogram_model", "range_km", "nugget", "sill", "nugget_share"))
_NO_SURFACE = {
    "pm25_png": None,
    "diff_png": None,
    "sd_png": None,
    "bounds_latlon": None,
    "surface_min": None,
    "surface_mean": None,
    "surface_max": None,
    "sd_max": None,
}


_NO_POINTS = HourPoints(x=np.empty(0), y=np.empty(0), z=np.empty(0), n_sensors=0, n_points=0)


# @spec DASH-WIN-001, DASH-IN-001, DASH-GAP-001, DASH-GAP-002, DASH-GAP-003, DASH-MAN-002
# @spec DASH-MAN-003, DASH-MAN-004, DASH-MAN-005, DASH-MAN-006, DASH-SITE-001, DASH-SITE-002
# @spec DASH-SITE-003, DASH-SITE-004, DASH-SITE-005
def build_site(
    archive_uri: str,
    out: str | Path,
    *,
    hours: int = 48,
    end: datetime | None = None,
    generated_at: datetime,
    map_settings: MapSettings | None = None,
) -> BuildSummary:
    """Rebuild the whole site into ``out`` from the archive: one frame pair per kriged hour of
    the window, the manifest, and the front end. ``out`` is replaced only once every file is
    written, and only if it is empty or a previous build."""
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    if end is not None and (end.tzinfo is None or end.utcoffset() is None):
        raise ValueError("end must be timezone-aware")
    if hours < 1:
        raise ValueError(f"hours must be positive, got {hours}")
    started = time.perf_counter()
    settings = map_settings or MapSettings()
    generated_at = generated_at.astimezone(UTC)
    out = Path(out)
    _check_out(out)

    window_start, window_end, latest = _resolve_window(archive_uri, hours, end, generated_at)
    log.info(
        "dashboard window [%s, %s), latest kriged hour %s",
        _iso(window_start),
        _iso(window_end),
        _iso(latest),
    )
    dates = _date_filter(window_start, window_end)
    fits = _read_fits(archive_uri, window_start, window_end)
    fit_rows = {
        pd.Timestamp(row.hour).to_pydatetime().astimezone(UTC): row
        for row in fits.itertuples(index=False)
    }
    sites = read_sites(archive_uri)
    monitors = _monitors(
        read_observations(archive_uri, Source.airnow, window_start, window_end), sites
    )
    calibrated = read_partitioned(archive_uri, CALIBRATED_HOURLY, date=dates)
    sensors = _sensors(calibrated, sites)
    sampled: dict[datetime, SurfaceArrays] = {}

    window = [window_start + k * H for k in range(hours)]
    kriged = [h for h in window if h in fit_rows and fit_rows[h].status == "kriged"]

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{out.name}-build-", dir=out.parent))
    try:
        (staging / FRAMES).mkdir(parents=True)
        surface_fields: dict[datetime, dict] = {}
        statuses: dict[datetime, HourStatus] = {}
        uris = [fit_rows[h].surface_uri for h in kriged]
        for hour, uri, surface in zip(kriged, uris, _surfaces(uris)):
            if isinstance(surface, Exception):
                log.warning(
                    "surface for %s could not be read from %s (%s); the hour is shown as "
                    "surface_missing",
                    _iso(hour),
                    uri,
                    surface,
                )
                statuses[hour] = HourStatus.surface_missing
                continue
            points = hour_points(calibrated, sites, hour) if not calibrated.empty else _NO_POINTS
            fit = fit_rows[hour]
            surface_fields[hour] = _render_hour(
                staging, hour, surface, settings, points, float(fit.range_m), fit.sill
            )
            sampled[hour] = surface
            statuses[hour] = HourStatus.kriged

        entries = []
        for hour in window:
            fit = fit_rows.get(hour)
            status = statuses.get(hour) or (
                HourStatus(fit.status) if fit is not None else HourStatus.missing
            )
            kriged_hour = status == HourStatus.kriged
            hour_monitors, check = monitor_check(monitors.get(hour, []), sampled.get(hour))
            entries.append(
                Hour(
                    hour=_iso(hour),
                    status=status,
                    **surface_fields.get(hour, _NO_SURFACE),
                    **(_variogram_fields(fit) if kriged_hour else _NO_VARIOGRAM),
                    **check,
                    n_sensors=None if fit is None else int(fit.n_sensors),
                    monitors=hour_monitors,
                    sensors=sensors.get(hour, []),
                )
            )
        manifest = Manifest(
            generated_at=_iso(generated_at),
            latest_hour=_iso(latest),
            window_start=_iso(window_start),
            window_end=_iso(window_end),
            scales=_scales(settings),
            hours=entries,
        )
        (staging / MANIFEST).write_text(
            json.dumps(manifest.model_dump(mode="json"), sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        for path in sorted(WEB.rglob("*")):
            if path.is_file():
                target = staging / path.relative_to(WEB)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        _replace(out, staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    site_bytes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    elapsed = time.perf_counter() - started
    if elapsed > TARGET_SECONDS:
        log.warning(
            "dashboard build took %.1f seconds, over the %d-second target", elapsed, TARGET_SECONDS
        )
    if site_bytes > TARGET_BYTES:
        log.warning("dashboard site is %d bytes, over the %d-byte target", site_bytes, TARGET_BYTES)
    counts = Counter(entry.status.value for entry in entries)
    return BuildSummary(
        window_start=window_start,
        window_end=window_end,
        latest_hour=latest,
        hours=dict(counts),
        frames=3 * len(surface_fields),
        site_bytes=site_bytes,
        elapsed_s=elapsed,
    )


def _replace(out: Path, staging: Path) -> None:
    """Move the finished site into place, removing the previous build only after."""
    if not out.exists():
        staging.rename(out)
        return
    previous = Path(tempfile.mkdtemp(prefix=f".{out.name}-old-", dir=out.parent))
    previous.rmdir()
    out.rename(previous)
    staging.rename(out)
    shutil.rmtree(previous, ignore_errors=True)
