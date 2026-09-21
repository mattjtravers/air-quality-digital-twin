"""AirNow quality control: missing-value, range, and flatline flags over one site's hours."""

from __future__ import annotations

from datetime import timedelta

from aqdt.airnow.models import AirNowRow
from aqdt.observation_store.schemas import QcFlag

PM_MIN = 0.0
PM_MAX = 1000.0  # µg/m³
HOUR = timedelta(hours=1)


def _flatline_rows(ordered: list[AirNowRow], flatline_hours: int) -> set[int]:
    """Positions (in ``ordered``) inside a run of >= ``flatline_hours`` consecutive hours with
    the same non-null value. A null, a change, or a gap in hours ends the run."""
    flagged: set[int] = set()
    run_start = 0
    for i in range(1, len(ordered) + 1):
        continues = (
            i < len(ordered)
            and ordered[i].value is not None
            and ordered[i - 1].value is not None
            and ordered[i].value == ordered[i - 1].value
            and ordered[i].utc == ordered[i - 1].utc + HOUR
        )
        if continues:
            continue
        if ordered[run_start].value is not None and i - run_start >= flatline_hours:
            flagged.update(range(run_start, i))
        run_start = i
    return flagged


# @spec AN-QC-001, AN-QC-002, AN-QC-003, AN-QC-004, AN-QC-005, AN-QC-007, AN-QC-008
def flags_for_site(rows: list[AirNowRow], flatline_hours: int) -> list[list[QcFlag]]:
    """Flags for each of one site's rows, in the order given; flatlines are evaluated over the
    rows sorted by hour."""
    order = sorted(range(len(rows)), key=lambda i: rows[i].utc)
    ordered = [rows[i] for i in order]
    flatlined = _flatline_rows(ordered, flatline_hours)
    result: list[list[QcFlag]] = [[] for _ in rows]
    for position, index in enumerate(order):
        row = rows[index]
        flags: set[QcFlag] = set()
        if row.value is None:
            flags.add(QcFlag.missing_value)
        elif not PM_MIN <= row.value <= PM_MAX:
            flags.add(QcFlag.out_of_range)
        if position in flatlined:
            flags.add(QcFlag.flatline)
        result[index] = sorted(flags, key=lambda flag: flag.value)
    return result
