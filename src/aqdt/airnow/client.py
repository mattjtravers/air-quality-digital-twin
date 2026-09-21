"""The AirNow ``/aq/data/`` client: a half-open UTC window fetched as 24-hour requests."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from aqdt.airnow.models import AirNowSettings

HOUR = timedelta(hours=1)
DATE_FORMAT = "%Y-%m-%dT%H"


class AirNowRequestError(RuntimeError):
    """A window request failed: a non-retryable status, or every attempt exhausted."""


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


# @spec AN-API-005, AN-API-006, AN-API-007
def _get_with_retry(
    client: httpx.Client, settings: AirNowSettings, params: dict[str, Any]
) -> httpx.Response:
    """Up to ``max_attempts`` GETs with exponential backoff on 429, 5xx, and transport errors.

    Any other 4xx fails immediately. Exhausting the attempts raises with the last status or
    exception.
    """
    last: str = ""
    for attempt in range(1, settings.max_attempts + 1):
        try:
            response = client.get(settings.base_url, params=params)
        except httpx.TransportError as exc:
            last = f"{type(exc).__name__}: {exc}"
        else:
            if response.is_success:
                return response
            if not _retryable(response.status_code):
                raise AirNowRequestError(f"AirNow request failed with HTTP {response.status_code}")
            last = f"HTTP {response.status_code}"
        if attempt < settings.max_attempts:
            time.sleep(settings.retry_backoff_seconds * 2 ** (attempt - 1))
    raise AirNowRequestError(
        f"AirNow request failed after {settings.max_attempts} attempts; last error: {last}"
    )


def chunks(start: datetime, end: datetime, hours: int) -> list[tuple[datetime, datetime]]:
    """Consecutive half-open ``[t, t + hours)`` windows covering ``[start, end)`` exactly."""
    out = []
    t = start
    while t < end:
        t_end = min(t + hours * HOUR, end)
        out.append((t, t_end))
        t = t_end
    return out


# @spec AN-API-001, AN-API-002, AN-API-003, AN-API-004, AN-API-008, AN-API-009
def fetch_rows(
    settings: AirNowSettings,
    start: datetime,
    end: datetime,
    transport: httpx.BaseTransport | None = None,
) -> list[dict[str, Any]]:
    """Every row for the half-open window ``[start, end)``, in window order, as received.

    The API's ``startDate``/``endDate`` are inclusive hours, so each request ends at
    ``end − 1 h``. ``transport`` lets tests substitute a mock.
    """
    bbox = settings.bbox
    fixed = {
        "API_KEY": settings.api_key,
        "BBOX": f"{bbox.nwlng},{bbox.selat},{bbox.selng},{bbox.nwlat}",
        "parameters": settings.parameters,
        "dataType": settings.data_type,
        "monitorType": settings.monitor_type,
        "verbose": settings.verbose,
        "includerawconcentrations": settings.include_raw_concentrations,
        "format": settings.response_format,
    }
    rows: list[dict[str, Any]] = []
    with httpx.Client(
        transport=transport, timeout=httpx.Timeout(settings.timeout_seconds)
    ) as client:
        for chunk_start, chunk_end in chunks(start, end, settings.chunk_hours):
            params = {
                **fixed,
                "startDate": chunk_start.strftime(DATE_FORMAT),
                "endDate": (chunk_end - HOUR).strftime(DATE_FORMAT),
            }
            payload = _get_with_retry(client, settings, params).json()
            if not isinstance(payload, list):
                raise AirNowRequestError("AirNow response is not a JSON array of rows")
            rows.extend(payload)
    return rows
