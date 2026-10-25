"""Splitting a calibration fit's residual variance into detector noise, reference measurement
error, and the remainder (representativeness and model error). Diagnostic only."""

from __future__ import annotations

import pandas as pd

from aqdt.purpleair.qc import CORRECTION_SLOPE

#: Hourly precision (1σ, µg/m³) of the monitors' methods, at two published bounds: Teledyne
#: T640 specified 1-hour precision, and Met One BAM-1020 median hourly noise across units.
REFERENCE_SD = {"T640": 0.5, "BAM-1020": 1.8}


# @spec EVAL-DECOMP-001
def detector_term(slope: float, snapshots: pd.DataFrame) -> float:
    """Mean over the pairs of ``(0.524 b)² · mean((A − B)² / 4) / k``; ``snapshots`` has one row
    per snapshot aggregated into a pair (``hour``, ``a``, ``b``, and ``site_id`` when the pairs
    span several sensors, as the pooled fit's do — a pair is one sensor's hour)."""
    if snapshots.empty:
        return float("nan")
    per_snapshot = (snapshots["a"] - snapshots["b"]) ** 2 / 4.0
    keys = [snapshots[c] for c in ("site_id", "hour") if c in snapshots.columns]
    per_hour = per_snapshot.groupby(keys).agg(["mean", "size"])
    variance_of_mean = per_hour["mean"] / per_hour["size"]
    return float((CORRECTION_SLOPE * slope) ** 2 * variance_of_mean.mean())


# @spec EVAL-DECOMP-002
def split_residual(sigma2: float, detector: float, ref_sd: float) -> dict[str, float | bool]:
    """``a`` detector, ``b`` reference, ``c`` remainder (0 when negative, with ``clamped``)."""
    reference = ref_sd**2
    remainder = sigma2 - detector - reference
    return {
        "a": detector,
        "b": reference,
        "c": max(remainder, 0.0),
        "clamped": remainder < 0,
    }
