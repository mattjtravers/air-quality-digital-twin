"""Routine-window resolution — PIPE-WIN."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from aqdt.pipeline.windows import resolve_as_of, resolve_window

from .conftest import MIDNIGHT, NOW, NOW_H

H = timedelta(hours=1)


# @spec PIPE-WIN-001
def test_resolvers_are_pure_and_require_an_aware_now():
    assert resolve_window(NOW, None, None, None) == resolve_window(NOW, None, None, None)
    assert resolve_as_of(NOW, None) == resolve_as_of(NOW, None)
    with pytest.raises(ValueError):
        resolve_window(datetime(2026, 9, 21, 14, 37), None, None, None)
    with pytest.raises(ValueError):
        resolve_as_of(datetime(2026, 9, 21, 14, 37), None)


# @spec PIPE-WIN-002
def test_default_window_is_trailing_48_hours_excluding_the_hour_in_progress():
    start, end = resolve_window(NOW, None, None, None)
    assert end == NOW_H
    assert start == NOW_H - 48 * H
    assert resolve_window(NOW, None, None, None, default_hours=6) == (NOW_H - 6 * H, NOW_H)


# @spec PIPE-WIN-003
def test_hours_and_explicit_bounds():
    assert resolve_window(NOW, None, None, 5) == (NOW_H - 5 * H, NOW_H)
    start = datetime(2026, 9, 20, 10, 17, tzinfo=UTC)
    end = datetime(2026, 9, 20, 14, 45, tzinfo=UTC)
    assert resolve_window(NOW, start, end, None) == (
        datetime(2026, 9, 20, 10, tzinfo=UTC),
        datetime(2026, 9, 20, 14, tzinfo=UTC),
    )
    # a non-UTC offset is converted, then truncated
    plus_two = datetime(2026, 9, 20, 12, 30, tzinfo=timezone(timedelta(hours=2)))
    assert resolve_window(NOW, plus_two, end, None)[0] == datetime(2026, 9, 20, 10, tzinfo=UTC)


# @spec PIPE-WIN-004
def test_as_of_defaults_to_the_most_recent_midnight_utc():
    assert resolve_as_of(NOW, None) == MIDNIGHT
    assert resolve_as_of(MIDNIGHT, None) == MIDNIGHT  # exactly midnight: today, not yesterday
    just_before = MIDNIGHT - timedelta(seconds=1)
    assert resolve_as_of(just_before, None) == MIDNIGHT - timedelta(days=1)
    explicit = datetime(2026, 9, 10, 14, 30, tzinfo=UTC)
    assert resolve_as_of(NOW, explicit) == datetime(2026, 9, 10, 14, tzinfo=UTC)
