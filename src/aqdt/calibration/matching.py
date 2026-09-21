"""Sensor-to-monitor matching by projected distance, in Python so it needs no database."""

from __future__ import annotations

import numpy as np
import pandas as pd

from aqdt.observation_store.schemas import SiteType

PROJECTED_CRS = "EPSG:26918"  # UTM zone 18N: covers the D.C. metro with negligible distortion
COLUMNS = ["site_id", "ref_site_id", "distance_m"]


# @spec CAL-IN-004, CAL-MATCH-001, CAL-MATCH-002, CAL-MATCH-003, CAL-MATCH-004, CAL-MATCH-005
def match_references(
    sites: pd.DataFrame, eligible_reference_ids: set[str], max_distance_m: float
) -> pd.DataFrame:
    """One row per ``low_cost_sensor`` site: the nearest eligible ``reference_monitor`` within
    ``max_distance_m`` (``ref_site_id``, ``distance_m``), or nulls when none is in range.

    Distances are metres between the points projected to EPSG:26918. Ties go to the smaller
    ``ref_site_id`` so the match is deterministic.
    """
    projected = sites.to_crs(PROJECTED_CRS)
    sensors = projected[projected["site_type"] == SiteType.low_cost_sensor.value].sort_values(
        "site_id"
    )
    references = projected[
        (projected["site_type"] == SiteType.reference_monitor.value)
        & projected["site_id"].isin(eligible_reference_ids)
    ].sort_values("site_id")

    ref_ids: list[str | None] = [None] * len(sensors)
    distances = np.full(len(sensors), np.nan)
    if len(sensors) and len(references):
        sx, sy = sensors.geometry.x.to_numpy(), sensors.geometry.y.to_numpy()
        rx, ry = references.geometry.x.to_numpy(), references.geometry.y.to_numpy()
        matrix = np.hypot(sx[:, None] - rx[None, :], sy[:, None] - ry[None, :])
        nearest = matrix.argmin(axis=1)  # first minimum: the smallest site_id among ties
        best = matrix[np.arange(len(sensors)), nearest]
        in_range = best <= max_distance_m
        reference_ids = references["site_id"].to_numpy()
        ref_ids = [reference_ids[j] if ok else None for j, ok in zip(nearest, in_range)]
        distances = np.where(in_range, best, np.nan)
    return pd.DataFrame(
        {
            "site_id": sensors["site_id"].to_numpy(),
            "ref_site_id": pd.Series(ref_ids, dtype="object"),
            "distance_m": distances,
        },
        columns=COLUMNS,
    ).reset_index(drop=True)
