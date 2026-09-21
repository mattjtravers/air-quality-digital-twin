"""Routine-window resolution: the project's only dependence on the clock.

Both functions are pure; ``main`` supplies ``now`` from ``datetime.now(UTC)`` and everything
downstream receives explicit bounds.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

DEFAULT_HOURS = 48
HOUR = timedelta(hours=1)


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _to_hour(value: datetime, name: str) -> datetime:
    return _aware(value, name).replace(minute=0, second=0, microsecond=0)


# @spec PIPE-WIN-001, PIPE-WIN-002, PIPE-WIN-003
def resolve_window(
    now: datetime,
    start: datetime | None,
    end: datetime | None,
    hours: int | None,
    default_hours: int = DEFAULT_HOURS,
) -> tuple[datetime, datetime]:
    """The half-open window a run covers.

    Explicit ``start``/``end`` are converted to UTC and truncated to the hour; otherwise the
    window is the trailing ``hours`` (default ``default_hours``) ending at ``now`` truncated to
    the hour, so the hour in progress is never requested.
    """
    now_h = _to_hour(now, "now")
    if start is not None and end is not None:
        return _to_hour(start, "start"), _to_hour(end, "end")
    if start is not None or end is not None:
        raise ValueError("start and end must be given together")
    length = default_hours if hours is None else hours
    return now_h - length * HOUR, now_h


# @spec PIPE-WIN-001, PIPE-WIN-004
def resolve_as_of(now: datetime, as_of: datetime | None) -> datetime:
    """The fit ``as_of``: as given (UTC, truncated to the hour), else the most recent 00:00 UTC
    at or before ``now``."""
    if as_of is not None:
        return _to_hour(as_of, "as_of")
    return _aware(now, "now").replace(hour=0, minute=0, second=0, microsecond=0)
