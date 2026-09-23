"""The AirNow run: a half-open window (extended back for flatline lookback) → validated rows →
site normalization → per-site QC over the whole fetch → canonical records for the window → the
store."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import ValidationError

from aqdt.airnow.client import fetch_rows
from aqdt.airnow.models import AirNowRow, AirNowSettings
from aqdt.airnow.qc import flags_for_site
from aqdt.airnow.sites import site_id_for, source_native_id
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

HOUR = timedelta(hours=1)
DUPLICATE_SITE_HOUR = "duplicate_site_hour"


# @spec AN-RUN-005
def _hour_bound(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware UTC datetime")
    return value.astimezone(UTC).replace(minute=0, second=0, microsecond=0)


def _rejection_reason(error: ValidationError) -> str:
    loc = error.errors()[0]["loc"]
    return str(loc[0]) if loc else "row"


# @spec AN-MAP-001, AN-MAP-002, AN-MAP-003
def _site_records(
    site_id: str,
    resolved: bool,
    entries: list[tuple[dict[str, Any], AirNowRow]],
    flatline_hours: int,
    start: datetime,
) -> tuple[Site | None, list[Observation]]:
    """One ``Site`` (metadata from its latest hour) and an ``Observation`` per row at or after
    ``start``; the earlier rows are lookback context for the flags only. ``None`` for a site
    whose rows are all context."""
    rows = [row for _, row in entries]
    flags = flags_for_site(rows, flatline_hours)
    latest = rows[-1]
    if latest.utc < start:
        return None, []
    site = Site(
        site_id=site_id,
        source=Source.airnow,
        source_native_id=source_native_id(
            latest.full_aqs_code, latest.intl_aqs_code, latest.site_name
        ),
        site_type=SiteType.reference_monitor,
        name=latest.site_name,
        latitude=latest.latitude,
        longitude=latest.longitude,
    )
    observations = []
    for (raw, row), row_flags in zip(entries, flags, strict=True):
        if row.utc < start:
            continue
        if not resolved:
            row_flags = [*row_flags, QcFlag.site_id_unresolved]
        observations.append(
            Observation(
                site_id=site_id,
                source=Source.airnow,
                observed_at=row.utc,
                latitude=row.latitude,
                longitude=row.longitude,
                pm25_raw=row.value,
                pm25_channel_a=None,
                pm25_channel_b=None,
                humidity=None,
                pm25_corrected=None,
                qc_flags=row_flags,
                raw=raw,
            )
        )
    return site, observations


# @spec AN-RUN-001, AN-RUN-002, AN-RUN-003, AN-RUN-004, AN-RUN-007, AN-RUN-008, AN-RUN-009
# @spec AN-MODEL-006, AN-QC-006
def ingest_airnow(
    settings: AirNowSettings,
    archive_uri: str,
    start: datetime,
    end: datetime,
    conn: Any = None,
    transport: httpx.BaseTransport | None = None,
) -> IngestSummary:
    """Ingest the half-open window ``[start, end)``.

    The fetch is extended back by ``flatline_hours − 1`` so flatline detection sees the hours
    preceding the window. Only the window's hours are written: a lookback hour was written by the
    run whose window held it, with more history behind it than it has here.
    """
    start, end = _hour_bound(start, "start"), _hour_bound(end, "end")
    if end <= start:
        raise ValueError("end must be after start (half-open window)")
    lookback = (settings.flatline_hours - 1) * HOUR
    rows = fetch_rows(settings, start - lookback, end, transport=transport)

    rejected: Counter[str] = Counter()
    seen: set[tuple[str, datetime]] = set()
    by_site: dict[str, list[tuple[dict[str, Any], AirNowRow]]] = {}
    resolved_by_site: dict[str, bool] = {}
    for raw in rows:
        try:
            row = AirNowRow.model_validate(raw)
        except ValidationError as error:
            rejected[_rejection_reason(error)] += 1
            continue
        site_id, resolved = site_id_for(row.full_aqs_code, row.intl_aqs_code, row.site_name)
        key = (site_id, row.utc)
        if key in seen:
            rejected[DUPLICATE_SITE_HOUR] += 1
            continue
        seen.add(key)
        by_site.setdefault(site_id, []).append((raw, row))
        resolved_by_site[site_id] = resolved

    sites: list[Site] = []
    observations: list[Observation] = []
    context = 0
    for site_id in sorted(by_site):
        entries = sorted(by_site[site_id], key=lambda entry: entry[1].utc)
        site, site_observations = _site_records(
            site_id, resolved_by_site[site_id], entries, settings.flatline_hours, start
        )
        context += len(entries) - len(site_observations)
        if site is not None:
            sites.append(site)
        observations.extend(site_observations)

    partitions: list[str] = []
    if observations:
        partitions += write_sites(sites, archive_uri)
        partitions += write_observations(observations, archive_uri)
    if conn is not None:
        load_partitions(conn, archive_uri, partitions)

    flagged: Counter[QcFlag] = Counter(flag for obs in observations for flag in obs.qc_flags)
    return IngestSummary(
        source=Source.airnow,
        fetched=len(rows),
        rejected=dict(rejected),
        written=len(observations),
        context=context,
        flagged=dict(flagged),
        partitions=partitions,
        snapshot_at=None,
        window_start=start,
        window_end=end,
    )
