"""The PurpleAir ``/v1/sensors`` client: one bounding-box snapshot per call, zipped into rows."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from aqdt.purpleair.models import PurpleAirSettings


class PurpleAirRequestError(RuntimeError):
    """The snapshot request failed: a non-retryable status, or every attempt exhausted."""


@dataclass(frozen=True)
class SensorSnapshot:
    """One ``/v1/sensors`` response: rows keyed by field name and the response's timestamp."""

    rows: list[dict[str, Any]]
    data_time_stamp: datetime


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


# @spec PA-API-005, PA-API-006, PA-API-007
def _get_with_retry(
    client: httpx.Client, settings: PurpleAirSettings, **kwargs: Any
) -> httpx.Response:
    """Up to ``max_attempts`` GETs with exponential backoff on 429, 5xx, and transport errors.

    Any other 4xx fails immediately. Exhausting the attempts raises with the last status or
    exception.
    """
    last: str = ""
    for attempt in range(1, settings.max_attempts + 1):
        try:
            response = client.get(settings.base_url, **kwargs)
        except httpx.TransportError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.is_success:
                return response
            if not _retryable(response.status_code):
                raise PurpleAirRequestError(
                    f"PurpleAir request failed with HTTP {response.status_code}"
                )
            last = f"HTTP {response.status_code}"
        if attempt < settings.max_attempts:
            time.sleep(settings.retry_backoff_seconds * 2 ** (attempt - 1))
    raise PurpleAirRequestError(
        f"PurpleAir request failed after {settings.max_attempts} attempts; last error: {last}"
    )


# @spec PA-API-001, PA-API-002, PA-API-003, PA-API-004, PA-API-008, PA-API-009
def fetch_sensors(
    settings: PurpleAirSettings, transport: httpx.BaseTransport | None = None
) -> SensorSnapshot:
    """Fetch one outdoor-sensor snapshot for the configured bounding box.

    ``transport`` lets tests substitute a mock; production uses httpx's default.
    """
    bbox = settings.bbox
    params = {
        "fields": ",".join(settings.fields),
        "location_type": settings.location_type,
        "max_age": settings.max_age,
        "nwlng": bbox.nwlng,
        "nwlat": bbox.nwlat,
        "selng": bbox.selng,
        "selat": bbox.selat,
    }
    with httpx.Client(
        transport=transport, timeout=httpx.Timeout(settings.timeout_seconds)
    ) as client:
        response = _get_with_retry(
            client, settings, params=params, headers={"X-API-Key": settings.api_key}
        )
    payload = response.json()
    fields = payload["fields"]
    rows = [dict(zip(fields, row, strict=True)) for row in payload["data"]]
    return SensorSnapshot(
        rows=rows, data_time_stamp=datetime.fromtimestamp(payload["data_time_stamp"], UTC)
    )
