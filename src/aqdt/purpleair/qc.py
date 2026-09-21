"""PurpleAir quality control: dual-channel, range, and missing-value flags, and the EPA
(Barkjohn et al. 2021) corrected value. Every check is a function of the row alone."""

from __future__ import annotations

from dataclasses import dataclass

from aqdt.observation_store.schemas import QcFlag
from aqdt.purpleair.models import PurpleAirSensorRecord

PM_MIN = 0.0
PM_MAX = 1000.0  # Plantower PMS5003 reporting ceiling, µg/m³
HUMIDITY_MIN = 0.0
HUMIDITY_MAX = 100.0

# EPA channel-agreement criteria (Barkjohn et al. 2021): both must hold to flag
DISAGREEMENT_ABSOLUTE = 5.0  # µg/m³
DISAGREEMENT_RELATIVE = 0.61

# EPA/Barkjohn 2021 nationwide correction: pm25 = 0.524 * mean(A, B) - 0.0862 * RH + 5.75
CORRECTION_SLOPE = 0.524
CORRECTION_HUMIDITY = 0.0862
CORRECTION_INTERCEPT = 5.75


@dataclass(frozen=True)
class QcResult:
    flags: list[QcFlag]  # de-duplicated, sorted by name
    pm25_corrected: float | None


def _out_of_range(record: PurpleAirSensorRecord) -> bool:
    for value in (record.pm2_5_cf_1, record.pm2_5_cf_1_a, record.pm2_5_cf_1_b):
        if value is not None and not PM_MIN <= value <= PM_MAX:
            return True
    humidity = record.humidity
    return humidity is not None and not HUMIDITY_MIN <= humidity <= HUMIDITY_MAX


def _channels_disagree(a: float, b: float) -> bool:
    difference = abs(a - b)
    if difference <= DISAGREEMENT_ABSOLUTE:
        return False
    mean = (a + b) / 2
    if mean == 0:
        return False
    return difference / mean > DISAGREEMENT_RELATIVE


# @spec PA-QC-001, PA-QC-002, PA-QC-003, PA-QC-004, PA-QC-005, PA-QC-006, PA-QC-007
# @spec PA-CORR-001, PA-CORR-002, PA-CORR-003, PA-CORR-004
def evaluate(record: PurpleAirSensorRecord) -> QcResult:
    """Every applicable flag for the row, and the corrected value when its preconditions hold."""
    a, b, humidity = record.pm2_5_cf_1_a, record.pm2_5_cf_1_b, record.humidity
    flags: set[QcFlag] = set()
    if record.pm2_5_cf_1 is None:
        flags.add(QcFlag.missing_value)
    if (a is None) != (b is None):
        flags.add(QcFlag.channel_missing)
    if _out_of_range(record):
        flags.add(QcFlag.out_of_range)
    if a is not None and b is not None and _channels_disagree(a, b):
        flags.add(QcFlag.channel_disagreement)

    withholding = {QcFlag.channel_missing, QcFlag.channel_disagreement, QcFlag.out_of_range}
    corrected: float | None = None
    if a is not None and b is not None and humidity is not None and not flags & withholding:
        value = (
            CORRECTION_SLOPE * (a + b) / 2 - CORRECTION_HUMIDITY * humidity + CORRECTION_INTERCEPT
        )
        corrected = max(0.0, value)
    return QcResult(flags=sorted(flags, key=lambda flag: flag.value), pm25_corrected=corrected)
