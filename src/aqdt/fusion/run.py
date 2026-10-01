"""``krige_surfaces``: an hourly kriged PM2.5 surface and its fit row for every hour in a window."""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from aqdt.calibration.products import CALIBRATED_HOURLY
from aqdt.calibration.schemas import hour_bound
from aqdt.fusion.frames import surface_fits_to_frame
from aqdt.fusion.grid import Grid, grid_for
from aqdt.fusion.krige import predict_chunked, select_variogram
from aqdt.fusion.points import HourPoints, hour_points
from aqdt.fusion.products import SURFACE_FITS
from aqdt.fusion.raster import encode_surface, surface_key
from aqdt.fusion.schemas import FusionSettings, SurfaceFit, SurfaceStatus
from aqdt.observation_store.archive import (
    read_partitioned,
    read_sites,
    replace_object,
    resolve_archive_uri,
    write_partitioned,
)
from aqdt.observation_store.postgis import load_partitions

log = logging.getLogger(__name__)

HOUR = timedelta(hours=1)
_NO_SURFACE = dict.fromkeys(
    (
        "variogram_model",
        "sill",
        "range_m",
        "nugget",
        "q1",
        "q2",
        "cr",
        "resolution_m",
        "width",
        "height",
        "surface_uri",
        "surface_sha256",
    )
)


def _not_kriged(hour: datetime, status: SurfaceStatus, points: HourPoints) -> SurfaceFit:
    return SurfaceFit(
        hour=hour,
        status=status,
        n_sensors=points.n_sensors,
        n_points=points.n_points,
        **_NO_SURFACE,
    )


# @spec FUS-STATUS-002, FUS-STATUS-003, FUS-STATUS-004
def _surface(
    hour: datetime, points: HourPoints, grid: Grid, settings: FusionSettings, uri: str
) -> tuple[SurfaceFit, bytes | None]:
    """Fit, predict, and encode one hour; the row, and the raster bytes when it was kriged."""
    if points.n_points < settings.min_points:
        return _not_kriged(hour, SurfaceStatus.insufficient_points, points), None
    variogram = select_variogram(points)
    if variogram is None:
        return _not_kriged(hour, SurfaceStatus.fit_failed, points), None
    estimate, variance = predict_chunked(variogram, grid)
    fit = SurfaceFit(
        hour=hour,
        status=SurfaceStatus.kriged,
        n_sensors=points.n_sensors,
        n_points=points.n_points,
        variogram_model=variogram.model,
        sill=variogram.sill,
        range_m=variogram.range_m,
        nugget=variogram.nugget,
        q1=variogram.q1,
        q2=variogram.q2,
        cr=variogram.cr,
        resolution_m=float(grid.resolution_m),
        width=grid.width,
        height=grid.height,
        surface_uri=uri,
        surface_sha256=None,
    )
    data = encode_surface(estimate, variance, grid, fit)
    return fit.model_copy(update={"surface_sha256": hashlib.sha256(data).hexdigest()}), data


# @spec FUS-IN-001, FUS-RASTER-006, FUS-STATUS-005
def _krige_hour(
    archive_uri: str, hour: datetime, grid: Grid, settings: FusionSettings
) -> SurfaceFit:
    """Write (or delete) the hour's raster through the store's guarded replacement, reading the
    hour's inputs inside ``produce`` so they are never older than the raster's token."""
    key = surface_key(hour)
    uri = f"{archive_uri.rstrip('/')}/{key}"
    outcome: dict[str, Any] = {}

    def produce() -> bytes | None:
        calibrated = read_partitioned(archive_uri, CALIBRATED_HOURLY, date=hour.date())
        points = hour_points(calibrated, read_sites(archive_uri), hour)
        outcome["fit"], data = _surface(hour, points, grid, settings, uri)
        return data

    replace_object(archive_uri, key, produce)
    return outcome["fit"]


# @spec FUS-RUN-001, FUS-RUN-002, FUS-RUN-003, FUS-RUN-004, FUS-RUN-005, FUS-STATUS-001
# @spec FUS-STORE-007
def krige_surfaces(
    archive_uri: str | None,
    start: datetime,
    end: datetime,
    settings: FusionSettings,
    conn: Any = None,
) -> pd.DataFrame:
    """Krige every hour in ``[start, end)``, in order: each hour's raster is written (or deleted)
    and then its ``SurfaceFit`` row, so a failure leaves every earlier hour complete. Given a
    PostGIS connection, the ``surface_fits`` partitions written are loaded at the end."""
    start, end = hour_bound(start, "start"), hour_bound(end, "end")
    if end <= start:
        raise ValueError(f"end ({end.isoformat()}) must be after start ({start.isoformat()})")
    archive_uri = resolve_archive_uri(archive_uri)
    grid = grid_for(settings.bbox, settings.resolution_m)
    fits: list[SurfaceFit] = []
    written: list[str] = []
    hour = start
    while hour < end:
        fit = _krige_hour(archive_uri, hour, grid, settings)
        for partition in write_partitioned(surface_fits_to_frame([fit]), archive_uri, SURFACE_FITS):
            if partition not in written:
                written.append(partition)
        log.info("surface %s: %s (%d points)", hour.isoformat(), fit.status.value, fit.n_points)
        fits.append(fit)
        hour += HOUR
    if conn is not None:
        load_partitions(conn, archive_uri, written)
    return surface_fits_to_frame(fits)
