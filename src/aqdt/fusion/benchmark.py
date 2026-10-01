"""Resolution benchmark: time and peak memory for one hourly surface at several grid resolutions.

    python -m aqdt.fusion.benchmark [--resolutions 500 200 100 50]
                                    [--hour 2026-09-20T15:00Z | --synthetic-points 100]
                                    [--archive-uri URI] [--json]

Each resolution runs in a fresh subprocess, so one resolution's peak memory cannot mask another's.
An archive hour is read but nothing is written: the raster is encoded in memory and discarded.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

DEFAULT_RESOLUTIONS = [500, 200, 100, 50]


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m aqdt.fusion.benchmark")
    parser.add_argument("--resolutions", type=int, nargs="+", default=DEFAULT_RESOLUTIONS)
    parser.add_argument("--hour", help="archive hour to krige (ISO 8601, UTC if naive)")
    parser.add_argument("--synthetic-points", type=int, default=100)
    parser.add_argument("--archive-uri", help="archive root (default: AQDT_ARCHIVE_URI)")
    parser.add_argument("--json", action="store_true", help="one JSON object per line")
    parser.add_argument("--worker", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.hour is not None:
        args.archive_uri = args.archive_uri or os.environ.get("AQDT_ARCHIVE_URI")
        if not args.archive_uri:
            parser.error("--hour needs --archive-uri or AQDT_ARCHIVE_URI")
    return args


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo") as handle:
            for line in handle:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


# @spec FUS-BENCH-003
def machine() -> dict[str, Any]:
    import pykrige
    import rasterio

    memory = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return {
        "cpu_model": _cpu_model(),
        "logical_cores": os.cpu_count(),
        "memory_gib": round(memory / 2**30, 1),
        "python": platform.python_version(),
        "pykrige": pykrige.__version__,
        "gdal": rasterio.__gdal_version__,
    }


def _hour(text: str) -> datetime:
    value = datetime.fromisoformat(text)
    value = value if value.tzinfo else value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _synthetic_points(n: int):
    """``n`` sensors scattered over DC_METRO sampling a smooth field, as kriging points."""
    import numpy as np
    from pyproj import Transformer

    from aqdt.fusion.points import HourPoints

    rng = np.random.default_rng(20261001)
    lons, lats = rng.uniform(-77.46, -76.74, n), rng.uniform(38.74, 39.06, n)
    z = 12 + 3 * np.sin(4 * (lons + 77.1)) + 2 * np.cos(5 * (lats - 38.9)) + rng.normal(0, 0.5, n)
    x, y = Transformer.from_crs(4326, 26918, always_xy=True).transform(lons, lats)
    return HourPoints(x=np.round(x), y=np.round(y), z=z, n_sensors=n, n_points=n)


# @spec FUS-BENCH-002, FUS-BENCH-004
def _worker(args: argparse.Namespace) -> dict[str, Any]:
    """One resolution, one hour: read, fit, predict, encode — timed; nothing written."""
    from aqdt.calibration.products import CALIBRATED_HOURLY
    from aqdt.fusion.grid import grid_for
    from aqdt.fusion.krige import predict_chunked, select_variogram
    from aqdt.fusion.points import hour_points
    from aqdt.fusion.raster import encode_surface
    from aqdt.fusion.schemas import FusionSettings, SurfaceFit, SurfaceStatus
    from aqdt.observation_store.archive import read_partitioned, read_sites

    settings = FusionSettings(resolution_m=args.worker)
    started = time.perf_counter()
    if args.hour is not None:
        hour = _hour(args.hour)
        calibrated = read_partitioned(args.archive_uri, CALIBRATED_HOURLY, date=hour.date())
        points = hour_points(calibrated, read_sites(args.archive_uri), hour)
    else:
        hour = datetime(2026, 1, 1, tzinfo=UTC)
        points = _synthetic_points(args.synthetic_points)
    grid = grid_for(settings.bbox, settings.resolution_m)
    variogram = select_variogram(points)
    if variogram is None:
        raise SystemExit(f"no eligible variogram for {points.n_points} points")
    predict_started = time.perf_counter()
    estimate, variance = predict_chunked(variogram, grid)
    predict_s = time.perf_counter() - predict_started
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
        surface_uri=None,
        surface_sha256=None,
    )
    raster = encode_surface(estimate, variance, grid, fit)
    total_s = time.perf_counter() - started
    peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss  # KiB on Linux
    return {
        "resolution_m": args.worker,
        "cells": grid.n_cells,
        "n_points": points.n_points,
        "variogram_model": variogram.model,
        "total_s": round(total_s, 3),
        "predict_s": round(predict_s, 3),
        "peak_rss_mib": round(peak_kib / 1024, 1),
        "raster_mib": round(len(raster) / 2**20, 2),
        "pid": os.getpid(),
    }


def _run_one(args: argparse.Namespace, resolution: int) -> dict[str, Any]:
    command = [sys.executable, "-m", "aqdt.fusion.benchmark", "--worker", str(resolution)]
    if args.hour is not None:
        command += ["--hour", args.hour, "--archive-uri", args.archive_uri]
    else:
        command += ["--synthetic-points", str(args.synthetic_points)]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    return json.loads(result.stdout.strip().splitlines()[-1])


def _table(info: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    lines = [
        f"{info['cpu_model']}, {info['logical_cores']} logical cores, {info['memory_gib']} GiB",
        f"Python {info['python']}, PyKrige {info['pykrige']}, GDAL {info['gdal']}",
        "",
        f"{'resolution (m)':>14} {'cells':>10} {'points':>6} {'total (s)':>10} "
        f"{'predict (s)':>11} {'peak RSS (MiB)':>14} {'raster (MiB)':>12}",
    ]
    for r in rows:
        lines.append(
            f"{r['resolution_m']:>14} {r['cells']:>10,} {r['n_points']:>6} {r['total_s']:>10.2f} "
            f"{r['predict_s']:>11.2f} {r['peak_rss_mib']:>14.1f} {r['raster_mib']:>12.2f}"
        )
    return "\n".join(lines)


# @spec FUS-BENCH-001
def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.worker is not None:
        print(json.dumps(_worker(args)))
        return 0
    info = machine()
    rows = [_run_one(args, resolution) for resolution in args.resolutions]
    if args.json:
        print(json.dumps(info))
        for row in rows:
            print(json.dumps(row))
    else:
        print(_table(info, rows))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
