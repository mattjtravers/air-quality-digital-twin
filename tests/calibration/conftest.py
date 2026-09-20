"""A synthetic archive for calibration tests.

One reference monitor with data (M1), one without (M2), and eight sensors whose corrected values
are exact functions of M1's hourly value, so every fit has a known answer:

| sensor | relation to M1 (y)     | distance to M1 | expected status                       |
|--------|------------------------|----------------|---------------------------------------|
| S1     | y = 2 + 1.5 x, 100 h   | 555 m          | fitted                                |
| S2     | y = x, 50 h            | 1 110 m        | pooled  (n_pairs < min_pairs)         |
| S3     | y = x, 100 h           | 44 km          | pooled  (no monitor within range)     |
| S4     | y = x, 100 h           | 7 862 m        | fitted  (M2 is nearer but has no data)|
| S5     | silent                 | 282 m          | pooled  (no pairs)                    |
| S6     | y = 30 - x, 100 h      | ~600 m         | pooled  (negative slope)              |
| S7     | x = 7 constant, 100 h  | ~600 m         | pooled  (constant input)              |
| S8     | y = 1 + 1.2 x, 100 h   | ~600 m         | fitted                                |

M1's value for hour h is 5 + (h mod 20). M1's hour ``as_of - 50 h`` is flagged (excluded from
pairs). S1's hours also carry one flagged and one uncorrected snapshot that would skew the mean
if they were included.
"""

from datetime import UTC, datetime, timedelta

import pytest

from aqdt.observation_store.archive import write_observations, write_sites
from aqdt.observation_store.schemas import Observation, QcFlag, Site, SiteType, Source

AS_OF = datetime(2026, 9, 20, 0, 0, tzinfo=UTC)
H = timedelta(hours=1)
DAY = timedelta(days=1)

M1 = ("airnow:840110010043", 38.90, -77.03)
M2 = ("airnow:840110010099", 38.95, -77.10)

SENSORS = {
    "purpleair:1": (38.905, -77.03),
    "purpleair:2": (38.91, -77.03),
    "purpleair:3": (39.30, -77.03),
    "purpleair:4": (38.945, -77.10),
    "purpleair:5": (38.902, -77.032),
    "purpleair:6": (38.9054, -77.031),
    "purpleair:7": (38.9052, -77.029),
    "purpleair:8": (38.9048, -77.0305),
}

# corrected value x as a function of the monitor's value y
RELATIONS = {
    "purpleair:1": lambda y: (y - 2) / 1.5,
    "purpleair:2": lambda y: y,
    "purpleair:3": lambda y: y,
    "purpleair:4": lambda y: y,
    "purpleair:6": lambda y: 30 - y,
    "purpleair:7": lambda y: 7.0,
    "purpleair:8": lambda y: (y - 1) / 1.2,
}
HOURS = {"purpleair:2": 50}
FLAGGED_MONITOR_HOUR = AS_OF - 50 * H


def monitor_value(hour: datetime) -> float:
    return 5.0 + (int((hour - AS_OF) / H) % 20)


def fit_hours(n: int = 100) -> list[datetime]:
    """The last ``n`` hours before as_of, newest first: as_of - 1 h ... as_of - n h."""
    return [AS_OF - k * H for k in range(1, n + 1)]


def sensor_obs(site_id, hour, corrected, minute, flags=(), uncorrected=False) -> Observation:
    lat, lon = SENSORS[site_id]
    return Observation(
        site_id=site_id,
        source=Source.purpleair,
        observed_at=hour + timedelta(minutes=minute),
        latitude=lat,
        longitude=lon,
        pm25_raw=corrected if corrected is not None else 1.0,
        pm25_channel_a=(corrected or 1.0),
        pm25_channel_b=(corrected or 1.0),
        humidity=None if uncorrected else 50.0,
        pm25_corrected=None if uncorrected else corrected,
        qc_flags=list(flags),
        raw={"sensor_index": int(site_id.split(":")[1]), "minute": minute},
    )


def monitor_obs(site, hour, value, flags=()) -> Observation:
    site_id, lat, lon = site
    return Observation(
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
        qc_flags=list(flags),
        raw={"Value": value},
    )


def build_archive(uri: str) -> str:
    sites = [
        Site(
            site_id=M1[0],
            source=Source.airnow,
            source_native_id="110010043",
            site_type=SiteType.reference_monitor,
            name="M1",
            latitude=M1[1],
            longitude=M1[2],
        ),
        Site(
            site_id=M2[0],
            source=Source.airnow,
            source_native_id="110010099",
            site_type=SiteType.reference_monitor,
            name="M2",
            latitude=M2[1],
            longitude=M2[2],
        ),
    ]
    for site_id, (lat, lon) in SENSORS.items():
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

    observations: list[Observation] = []
    # M1: hourly, one flagged hour; plus rows on and just outside the window boundaries
    for hour in fit_hours(100):
        flags = [QcFlag.flatline] if hour == FLAGGED_MONITOR_HOUR else []
        observations.append(monitor_obs(M1, hour, monitor_value(hour), flags))
    for hour in (AS_OF, AS_OF - 30 * DAY, AS_OF - 30 * DAY - H):
        observations.append(monitor_obs(M1, hour, monitor_value(hour)))
    # M2: present but every hour flagged -> never an eligible reference
    for hour in fit_hours(100):
        observations.append(monitor_obs(M2, hour, 1.0, [QcFlag.flatline]))
    # sensors: two snapshots per hour straddling the exact value
    for site_id, relation in RELATIONS.items():
        hours = fit_hours(HOURS.get(site_id, 100))
        if site_id == "purpleair:1":
            hours = hours + [AS_OF, AS_OF - 30 * DAY, AS_OF - 30 * DAY - H]
        for hour in hours:
            x = relation(monitor_value(hour))
            observations.append(sensor_obs(site_id, hour, x + 0.5, 10))
            observations.append(sensor_obs(site_id, hour, x - 0.5, 40))
            if site_id == "purpleair:1":
                observations.append(sensor_obs(site_id, hour, 999.0, 20, [QcFlag.out_of_range]))
                observations.append(sensor_obs(site_id, hour, None, 50, uncorrected=True))
    write_observations(observations, uri)
    return uri


@pytest.fixture
def archive_uri(tmp_path):
    return build_archive(str(tmp_path / "archive"))


@pytest.fixture
def settings(monkeypatch):
    from aqdt.calibration.schemas import CalibrationSettings

    for name in ("AQDT_CAL_MAX_DISTANCE_M", "AQDT_CAL_WINDOW_DAYS", "AQDT_CAL_MIN_PAIRS"):
        monkeypatch.delenv(name, raising=False)
    return CalibrationSettings()
