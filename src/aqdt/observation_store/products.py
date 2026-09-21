"""Product definitions: how any archive product is partitioned, keyed, validated, and loaded.

Every product in the project — observations, sites, and each downstream component's outputs —
is one ``Product``. The registry (``aqdt.registry.PRODUCTS``) lists them in load order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pandera.pandas as pa

from aqdt.observation_store.frames import ObservationsFrame, SitesFrame

SQL_DIR = Path(__file__).parent / "sql"


# @spec OBS-ARCHIVE-014
@dataclass(frozen=True, eq=False)
class Product:
    """One archive product.

    ``partition_keys`` maps key name → function of the frame returning each row's partition
    value; the store renders values to path segments (``render_partition_value``). Keys are
    derived, never stored as columns. The archive path of a partition is
    ``{archive_uri}/{prefix}/{k1}={v1}/.../{filename}``.
    """

    prefix: str
    partition_keys: Mapping[str, Callable[[pd.DataFrame], pd.Series]]
    key_cols: list[str]
    frame_model: type[pa.DataFrameModel]
    filename: str
    table: str
    sql_dir: Path | None

    @property
    def has_geometry(self) -> bool:
        return "geometry" in self.frame_model.to_schema().columns


# @spec OBS-ARCHIVE-022
def render_partition_value(value: Any) -> str:
    """Render a partition value as a path segment: dates ``YYYY-MM-DD``, datetimes
    ``YYYY-MM-DDTHH`` (UTC), strings and enumerations as their value."""
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        return value.strftime("%Y-%m-%dT%H")
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


SITES = Product(
    prefix="",
    partition_keys={"source": lambda f: f["source"]},
    key_cols=["site_id"],
    frame_model=SitesFrame,
    filename="sites.parquet",
    table="sites",
    sql_dir=SQL_DIR,
)

OBSERVATIONS = Product(
    prefix="",
    partition_keys={
        "source": lambda f: f["source"],
        "date": lambda f: f["observed_at"].dt.tz_convert("UTC").dt.date,
    },
    key_cols=["site_id", "observed_at"],
    frame_model=ObservationsFrame,
    filename="observations.parquet",
    table="observations",
    sql_dir=SQL_DIR,
)
