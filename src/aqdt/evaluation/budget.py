"""The error budget: every error source the pipeline knows of, where it enters, what kind it is,
and how evaluation handles it."""

from __future__ import annotations

import pandas as pd

DETECTOR = "PurpleAir detector noise (channel A/B disagreement)"
BARKJOHN = "Barkjohn correction coefficients"
REFERENCE = "AirNow reference measurement error"
AIRNOW_DATA = "AirNow preliminary data, Value vs RawConcentration, flatline flags"
SENSOR_FIT = "Per-sensor calibration fit parameters"
POOLED_FIT = "Pooled calibration fit parameters"
RESIDUAL = "Calibration residual (representativeness, siting, model error)"
DILUTION = "Regression dilution (calibration slopes below 1)"
KRIGING = "Kriging interpolation error"
VARIOGRAM = "Variogram fit (binning, range, model choice)"
DEPENDENCE = "Monitors as calibration targets for nearby sensors"

#: (source, stage, type, handling, magnitude when the run does not quantify it)
ROWS = [
    (DETECTOR, "ingest (PurpleAir)", "random", "quantified-diagnostic", "not quantified"),
    (
        BARKJOHN,
        "ingest (PurpleAir)",
        "systematic-shared",
        "listed-not-modeled",
        "nationwide fit (Barkjohn et al. 2021); not quantified here",
    ),
    (
        REFERENCE,
        "ingest (AirNow)",
        "random",
        "quantified-diagnostic",
        "0.5 µg/m³ (T640) to 1.8 µg/m³ (BAM-1020) hourly 1σ; 10% CV goal (40 CFR 58 App. A)",
    ),
    (AIRNOW_DATA, "ingest (AirNow)", "systematic-shared", "listed-not-modeled", "not quantified"),
    (SENSOR_FIT, "calibration", "random", "quantified-included", "not quantified"),
    (POOLED_FIT, "calibration", "systematic-shared", "quantified-included", "not quantified"),
    (RESIDUAL, "calibration", "random", "quantified-included", "not quantified"),
    (DILUTION, "calibration", "systematic-shared", "listed-not-modeled", "not quantified"),
    (KRIGING, "fusion", "random", "quantified-included", "not quantified"),
    (VARIOGRAM, "fusion", "systematic-shared", "quantified-diagnostic", "not quantified"),
    (DEPENDENCE, "evaluation", "systematic-shared", "listed-not-modeled", "not quantified"),
]
COLUMNS = ["source", "stage", "type", "handling", "magnitude"]


# @spec EVAL-BUDGET-001, EVAL-BUDGET-002, EVAL-BUDGET-003
def error_budget(magnitudes: dict[str, str]) -> pd.DataFrame:
    """The budget, with each source's magnitude from ``magnitudes`` where the run quantified it."""
    return pd.DataFrame(
        [
            (source, stage, kind, handling, magnitudes.get(source, default))
            for source, stage, kind, handling, default in ROWS
        ],
        columns=COLUMNS,
    )
