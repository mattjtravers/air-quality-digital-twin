"""Evaluation settings and the summary a run returns."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


# @spec EVAL-CFG-001
class EvaluationSettings(BaseSettings):
    """``AQDT_EVAL_*`` environment variables, all optional."""

    model_config = SettingsConfigDict(env_prefix="AQDT_EVAL_", extra="ignore", frozen=True)

    mc_hours: int = Field(default=24, ge=1)
    mc_draws: int = Field(default=500, ge=1)
    seed: int = Field(default=0, ge=0)


class EvaluationSummary(BaseModel):
    """What ``evaluate`` returns: the window, where the report is, and the headline numbers."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    window_start: datetime
    window_end: datetime
    out_dir: Path
    kriged_hours: int
    days: int
    headline: dict[str, float | int | None]
