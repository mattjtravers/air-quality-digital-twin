"""The PurpleAir run: one snapshot → validated rows → QC → canonical records → the store."""

from __future__ import annotations

from collections import Counter
from typing import Any

import httpx
from pydantic import ValidationError

from aqdt.observation_store.archive import write_observations, write_sites
from aqdt.observation_store.postgis import load_partitions
from aqdt.observation_store.schemas import (
    IngestSummary,
    Observation,
    QcFlag,
    Site,
    SiteType,
    Source,
)
from aqdt.purpleair.client import fetch_sensors
from aqdt.purpleair.models import PurpleAirSensorRecord, PurpleAirSettings
from aqdt.purpleair.qc import evaluate


def _rejection_reason(error: ValidationError) -> str:
    loc = error.errors()[0]["loc"]
    return str(loc[0]) if loc else "row"


# @spec PA-MAP-001, PA-MAP-002, PA-MAP-003
def _records(row: dict[str, Any], record: PurpleAirSensorRecord) -> tuple[Site, Observation]:
    result = evaluate(record)
    site_id = f"purpleair:{record.sensor_index}"
    site = Site(
        site_id=site_id,
        source=Source.purpleair,
        source_native_id=str(record.sensor_index),
        site_type=SiteType.low_cost_sensor,
        name=record.name,
        latitude=record.latitude,
        longitude=record.longitude,
    )
    observation = Observation(
        site_id=site_id,
        source=Source.purpleair,
        observed_at=record.last_seen,
        latitude=record.latitude,
        longitude=record.longitude,
        pm25_raw=record.pm2_5_cf_1,
        pm25_channel_a=record.pm2_5_cf_1_a,
        pm25_channel_b=record.pm2_5_cf_1_b,
        humidity=record.humidity,
        pm25_corrected=result.pm25_corrected,
        qc_flags=result.flags,
        raw=row,
    )
    return site, observation


# @spec PA-RUN-001, PA-RUN-002, PA-RUN-003, PA-RUN-004, PA-RUN-005, PA-RUN-006, PA-RUN-007
def ingest_purpleair(
    settings: PurpleAirSettings,
    archive_uri: str,
    conn: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> IngestSummary:
    """Fetch one snapshot, write sites then observations, load PostGIS when ``conn`` is given.

    Each sensor yields at most one observation keyed by its ``last_seen``; sensors are not
    de-duplicated within a snapshot, so a repeated ``sensor_index`` fails at the store's
    within-batch duplicate check.
    """
    snapshot = fetch_sensors(settings, transport=transport)
    rejected: Counter[str] = Counter()
    sites: list[Site] = []
    observations: list[Observation] = []
    for row in snapshot.rows:
        try:
            record = PurpleAirSensorRecord.model_validate(row)
        except ValidationError as error:
            rejected[_rejection_reason(error)] += 1
            continue
        site, observation = _records(row, record)
        sites.append(site)
        observations.append(observation)

    partitions: list[str] = []
    if observations:
        partitions += write_sites(sites, archive_uri)
        partitions += write_observations(observations, archive_uri)
    if conn is not None:
        load_partitions(conn, archive_uri, partitions)

    flagged: Counter[QcFlag] = Counter(flag for obs in observations for flag in obs.qc_flags)
    return IngestSummary(
        source=Source.purpleair,
        fetched=len(snapshot.rows),
        rejected=dict(rejected),
        written=len(observations),
        flagged=dict(flagged),
        partitions=partitions,
        snapshot_at=snapshot.data_time_stamp,
        window_start=None,
        window_end=None,
    )
