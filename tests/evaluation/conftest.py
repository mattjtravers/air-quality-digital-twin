"""A synthetic archive for evaluation tests, built by running the real pipeline over synthetic
observations: calibration fit, calibration apply, and fusion write every product evaluation reads.

The truth is a known PM2.5 field ``truth(lon, lat, hour)``. Thirty PurpleAir sensors read it
through a per-sensor gain and offset plus noise, with channel A/B disagreement; five AirNow
monitors read it with small noise. Times (UTC):

- fit window: ``[AS_OF − 4 d, AS_OF)``, hourly sensor and monitor observations;
- ``AS_OF`` = 2026-09-14T00, one fit run (``window_days=4``, ``min_pairs=48``): sensors within
  10 km of a monitor are ``fitted``, the rest ``pooled``;
- 10:00, 11:00, 12:00 — every sensor reports; kriged;
- 13:00 — five sensors report; ``insufficient_points``;
- 14:00 — no ``surface_fits`` row (a gap in a window ending at 15:00).

Monitors: M1–M4 inside the grid; M5 north of the box (outside the grid). M2 at 11:00 carries a
``flatline`` flag; M3 at 12:00 has a null value (``missing_value``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from aqdt.observation_store.archive import write_observations, write_sites
from aqdt.observation_store.schemas import Observation, QcFlag, Site, SiteType, Source

H = timedelta(hours=1)
AS_OF = datetime(2026, 9, 14, tzinfo=UTC)
WINDOW_DAYS = 4
MIN_PAIRS = 48
H10, H11, H12, H13, H14 = (AS_OF + k * H for k in (10, 11, 12, 13, 14))
KRIGED = [H10, H11, H12]
EVAL_WINDOW = (H10, H14 + H)
RESOLUTION_M = 2000

MONITORS = {
    "airnow:840110010001": (38.90, -77.03),
    "airnow:840110010002": (38.85, -77.25),
    "airnow:840110010003": (39.00, -76.85),
    "airnow:840110010004": (38.80, -76.95),
    "airnow:840110010005": (39.30, -77.03),  # north of DC_METRO: outside the grid
}
INSIDE = list(MONITORS)[:4]
OUTSIDE_MONITOR = list(MONITORS)[4]
FLAGGED = (list(MONITORS)[1], H11)
NULL_VALUE = (list(MONITORS)[2], H12)
REPORTING_AT_13 = 5

CAL_ENV = {"AQDT_CAL_WINDOW_DAYS": str(WINDOW_DAYS), "AQDT_CAL_MIN_PAIRS": str(MIN_PAIRS)}


def truth(lon, lat, hour: datetime):
    """The known PM2.5 field, µg/m³ (roughly 3 to 14)."""
    h = (hour - AS_OF) / H
    base = 8.0 + 3.0 * np.sin(2 * np.pi * h / 24.0)
    return (
        base + 1.5 * np.sin(4.0 * (np.asarray(lon) + 77.1)) + np.cos(5.0 * (np.asarray(lat) - 38.9))
    )


def _sensors(n: int = 30, seed: int = 7) -> dict[str, tuple[float, float, float, float]]:
    """site_id -> (lat, lon, gain, offset); corrected value = (truth − offset) / gain + noise."""
    rng = np.random.default_rng(seed)
    lats = rng.uniform(38.74, 39.06, n)
    lons = rng.uniform(-77.46, -76.74, n)
    gains = rng.uniform(0.6, 0.9, n)
    offsets = rng.uniform(-1.0, 1.0, n)
    return {
        f"purpleair:{k + 1}": (float(a), float(o), float(g), float(b))
        for k, (a, o, g, b) in enumerate(zip(lats, lons, gains, offsets))
    }


SENSORS = _sensors()


def _sites() -> list[Site]:
    sites = [
        Site(
            site_id=site_id,
            source=Source.airnow,
            source_native_id=site_id.split(":")[1],
            site_type=SiteType.reference_monitor,
            name=f"monitor {k + 1}",
            latitude=lat,
            longitude=lon,
        )
        for k, (site_id, (lat, lon)) in enumerate(MONITORS.items())
    ]
    sites += [
        Site(
            site_id=site_id,
            source=Source.purpleair,
            source_native_id=site_id.split(":")[1],
            site_type=SiteType.low_cost_sensor,
            name=site_id,
            latitude=lat,
            longitude=lon,
        )
        for site_id, (lat, lon, _, _) in SENSORS.items()
    ]
    return sites


def _hours():
    hour = AS_OF - timedelta(days=WINDOW_DAYS)
    while hour < H14 + H:
        yield hour
        hour += H


def _observations() -> list[Observation]:
    rng = np.random.default_rng(11)
    out: list[Observation] = []
    for hour in _hours():
        for site_id, (lat, lon) in MONITORS.items():
            value: float | None = float(truth(lon, lat, hour) + rng.normal(0, 0.3))
            flags: list[QcFlag] = []
            if (site_id, hour) == FLAGGED:
                flags = [QcFlag.flatline]
            if (site_id, hour) == NULL_VALUE:
                value, flags = None, [QcFlag.missing_value]
            out.append(
                Observation(
                    site_id=site_id,
                    source=Source.airnow,
                    observed_at=hour,
                    latitude=lat,
                    longitude=lon,
                    pm25_raw=value,
                    pm25_channel_a=None,
                    pm25_channel_b=None,
                    humidity=None,
                    pm25_corrected=None,
                    qc_flags=flags,
                    raw={"Value": value},
                )
            )
        for k, (site_id, (lat, lon, gain, offset)) in enumerate(SENSORS.items()):
            if hour == H13 and k >= REPORTING_AT_13:
                continue
            corrected = (float(truth(lon, lat, hour)) - offset) / gain + rng.normal(0, 0.4)
            mean = max(corrected, 0.5) / 0.524
            diff = rng.normal(0, 0.6)
            out.append(
                Observation(
                    site_id=site_id,
                    source=Source.purpleair,
                    observed_at=hour + timedelta(minutes=30),
                    latitude=lat,
                    longitude=lon,
                    pm25_raw=mean,
                    pm25_channel_a=mean + diff / 2,
                    pm25_channel_b=mean - diff / 2,
                    humidity=50.0,
                    pm25_corrected=max(corrected, 0.0),
                    qc_flags=[],
                    raw={"sensor_index": int(site_id.split(":")[1])},
                )
            )
    return out


# @spec EVAL-CFG-002
def build_archive(uri: str) -> str:
    from aqdt.calibration.apply import apply_calibrations
    from aqdt.calibration.fit import fit_calibrations
    from aqdt.calibration.schemas import CalibrationSettings
    from aqdt.fusion.run import krige_surfaces
    from aqdt.fusion.schemas import FusionSettings

    write_sites(_sites(), uri)
    write_observations(_observations(), uri)
    cal = CalibrationSettings(window_days=WINDOW_DAYS, min_pairs=MIN_PAIRS)
    fit_calibrations(uri, AS_OF, cal)
    apply_calibrations(uri, H10, H14, cal)
    krige_surfaces(uri, H10, H14, FusionSettings(resolution_m=RESOLUTION_M))
    return uri


def eval_settings(**overrides):
    from aqdt.evaluation.schemas import EvaluationSettings

    values = {"mc_hours": 2, "mc_draws": 40, "seed": 0} | overrides
    return EvaluationSettings(**values)


def _cal_environment(mp: pytest.MonkeyPatch) -> None:
    for name, value in CAL_ENV.items():
        mp.setenv(name, value)
    for name in ("AQDT_BBOX", "AQDT_MAP_PM25_MAX", "AQDT_MAP_SD_MAX"):
        mp.delenv(name, raising=False)


@pytest.fixture
def cal_env(monkeypatch):
    """The calibration settings the archive's fits were made with, for one test."""
    _cal_environment(monkeypatch)


@pytest.fixture(scope="session")
def archive(tmp_path_factory) -> str:
    return build_archive(str(tmp_path_factory.mktemp("eval") / "archive"))


@pytest.fixture(scope="session")
def report(archive, tmp_path_factory):
    """One evaluation of the window; tests read its outputs."""
    from aqdt.evaluation.run import evaluate

    out = tmp_path_factory.mktemp("report")
    with pytest.MonkeyPatch.context() as mp:
        _cal_environment(mp)
        summary = evaluate(archive, *EVAL_WINDOW, out, eval_settings())
    return out, summary
