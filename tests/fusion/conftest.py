"""A synthetic archive for fusion tests, built from a known PM2.5 field.

Calibrated sensor values are exact samples of ``field(lon, lat)``, a smooth surface over the
D.C. metro, so the kriged surface has a known answer. Hours (UTC, 2026-09-20):

- 10:00 — 60 scattered sensors, 2 co-located, 1 outside the box; plus 3 uncalibrated rows and
  1 row whose site has no site record. Expected: kriged.
- 11:00 — 5 sensors. Expected: insufficient_points.
- 12:00 — 12 sensors, every value 7.0. Expected: fit_failed.
- 13:00 — no rows. Expected: insufficient_points.
- 14:00 — the 10:00 sensors again, values + 2. Expected: kriged.

A reference monitor sits in the middle of the box with an AirNow reading of 500 µg/m³ at 10:00;
it must never become a kriging point.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from aqdt.calibration.frames import records_to_frame
from aqdt.calibration.products import CALIBRATED_HOURLY
from aqdt.calibration.schemas import CalibratedHourly, FitStatus
from aqdt.observation_store.archive import write_observations, write_partitioned, write_sites
from aqdt.observation_store.schemas import Observation, Site, SiteType, Source

H = timedelta(hours=1)
H10 = datetime(2026, 9, 20, 10, tzinfo=UTC)
H11, H12, H13, H14 = (H10 + k * H for k in (1, 2, 3, 4))
FIT_AS_OF = datetime(2026, 9, 20, 0, tzinfo=UTC)

#: Coarse enough to keep every test fast: about 36 x 24 cells over DC_METRO.
TEST_RESOLUTION_M = 2000

MONITOR = ("airnow:840110010043", 38.90, -77.10)
COLOCATED = [("purpleair:9001", 10.0), ("purpleair:9002", 20.0)]
COLOCATED_AT = (38.85, -77.20)
OUTSIDE = ("purpleair:9100", 39.30, -77.05)  # north of DC_METRO's 39.1
NO_SITE = "purpleair:9999"
#: Sensors with a site record whose only row at 10:00 is uncalibrated.
UNCALIBRATED = {
    "purpleair:8001": (38.80, -77.30),
    "purpleair:8002": (38.95, -76.90),
    "purpleair:8003": (39.00, -77.00),
}


def field(lon, lat):
    """The known PM2.5 surface, µg/m³: smooth, between roughly 7 and 17."""
    return (
        12.0
        + 3.0 * np.sin(4.0 * (np.asarray(lon) + 77.1))
        + 2.0 * np.cos(5.0 * (np.asarray(lat) - 38.9))
    )


def scattered_sensors(n: int = 60, seed: int = 20260920) -> dict[str, tuple[float, float]]:
    """``n`` sensors scattered over the box, inset from its edges; site_id -> (lat, lon)."""
    rng = np.random.default_rng(seed)
    lats = rng.uniform(38.74, 39.06, n)
    lons = rng.uniform(-77.46, -76.74, n)
    return {
        f"purpleair:{k + 1}": (float(lat), float(lon))
        for k, (lat, lon) in enumerate(zip(lats, lons))
    }


SENSORS = scattered_sensors()


def all_sensor_sites() -> dict[str, tuple[float, float]]:
    sites = dict(SENSORS)
    for site_id, _ in COLOCATED:
        sites[site_id] = COLOCATED_AT
    sites[OUTSIDE[0]] = (OUTSIDE[1], OUTSIDE[2])
    sites.update(UNCALIBRATED)
    return sites


def calibrated(site_id, hour, value, status=FitStatus.fitted) -> CalibratedHourly:
    return CalibratedHourly(
        site_id=site_id,
        hour=hour,
        pm25_calibrated=value,
        pm25_corrected_mean=value if value is not None else 1.0,
        n_snapshots=1,
        fit_as_of=None if status == FitStatus.uncalibrated else FIT_AS_OF,
        fit_status=status,
    )


def hour10_rows(offset: float = 0.0, hour: datetime = H10) -> list[CalibratedHourly]:
    rows = []
    for k, (site_id, (lat, lon)) in enumerate(SENSORS.items()):
        status = FitStatus.pooled if k % 3 == 0 else FitStatus.fitted
        rows.append(calibrated(site_id, hour, float(field(lon, lat)) + offset, status))
    for site_id, value in COLOCATED:
        rows.append(calibrated(site_id, hour, value + offset))
    rows.append(calibrated(OUTSIDE[0], hour, float(field(OUTSIDE[2], OUTSIDE[1])) + offset))
    return rows


#: Rows at 10:00 that fusion must not use.
EXCLUDED_AT_10 = 3 + 1  # three uncalibrated rows, one row with no site record
USED_AT_10 = len(SENSORS) + len(COLOCATED) + 1  # n_sensors
POINTS_AT_10 = len(SENSORS) + 1 + 1  # n_points: the co-located pair merges


def build_archive(uri: str, orphan: bool = True) -> str:
    """The archive above; ``orphan=False`` leaves out the calibrated row with no site record,
    which a PostGIS load (``site_id references sites``) would reject."""
    sites = [
        Site(
            site_id=MONITOR[0],
            source=Source.airnow,
            source_native_id="110010043",
            site_type=SiteType.reference_monitor,
            name="monitor",
            latitude=MONITOR[1],
            longitude=MONITOR[2],
        )
    ]
    for site_id, (lat, lon) in all_sensor_sites().items():
        sites.append(
            Site(
                site_id=site_id,
                source=Source.purpleair,
                source_native_id=site_id.split(":")[1],
                site_type=SiteType.low_cost_sensor,
                name=site_id,
                latitude=lat,
                longitude=lon,
            )
        )
    write_sites(sites, uri)
    write_observations(
        [
            Observation(
                site_id=MONITOR[0],
                source=Source.airnow,
                observed_at=H10,
                latitude=MONITOR[1],
                longitude=MONITOR[2],
                pm25_raw=500.0,
                pm25_channel_a=None,
                pm25_channel_b=None,
                humidity=None,
                pm25_corrected=None,
                qc_flags=[],
                raw={"Value": 500.0},
            )
        ],
        uri,
    )

    rows = hour10_rows()
    rows += [calibrated(s, H10, None, FitStatus.uncalibrated) for s in UNCALIBRATED]
    if orphan:
        rows.append(calibrated(NO_SITE, H10, 50.0))
    rows += [calibrated(s, H11, 10.0) for s in list(SENSORS)[:5]]
    rows += [calibrated(s, H12, 7.0) for s in list(SENSORS)[:12]]
    rows += hour10_rows(offset=2.0, hour=H14)
    write_partitioned(records_to_frame(rows, CalibratedHourly), uri, CALIBRATED_HOURLY)
    return uri


@pytest.fixture
def archive_uri(tmp_path):
    return build_archive(str(tmp_path / "archive"))


@pytest.fixture
def settings(monkeypatch):
    from aqdt.fusion.schemas import FusionSettings

    for name in ("AQDT_FUS_RESOLUTION_M", "AQDT_FUS_MIN_POINTS", "AQDT_BBOX"):
        monkeypatch.delenv(name, raising=False)
    return FusionSettings(resolution_m=TEST_RESOLUTION_M)
