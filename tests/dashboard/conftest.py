"""Dashboard test scaffolding: the evaluation segment's synthetic archive, built by the real
pipeline, read by the dashboard build.

Hours (UTC, from ``tests/evaluation/conftest.py``): 10:00, 11:00, 12:00 kriged; 13:00
``insufficient_points``; 14:00 no ``surface_fits`` row. Monitor M2 is flagged at 11:00 and M3 has a
null value at 12:00. The grid is 2 km, so frames are small.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ..evaluation.conftest import H10, H11, H12, H13, H14, build_archive

__all__ = ["H", "H10", "H11", "H12", "H13", "H14", "build", "GENERATED_AT"]

H = timedelta(hours=1)
#: A build two hours after the fixture's last hour, so the default window ends at 12:00.
GENERATED_AT = H14 + 2 * H


@pytest.fixture(autouse=True)
def _map_environment(monkeypatch):
    """Maps' default limits, whatever the developer's environment says."""
    for name in ("AQDT_MAP_PM25_MAX", "AQDT_MAP_SD_MAX", "AQDT_BBOX"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def archive(tmp_path_factory) -> str:
    return build_archive(str(tmp_path_factory.mktemp("dashboard") / "archive"))


def build(archive: str, out: Path, *, generated_at: datetime = GENERATED_AT, **kwargs):
    from aqdt.dashboard.build import build_site

    return build_site(archive, out, generated_at=generated_at, **kwargs)


@pytest.fixture(scope="session")
def site(archive, tmp_path_factory):
    """One build over the fixture's whole span: ``[10:00, 15:00)``, five hours."""
    out = tmp_path_factory.mktemp("site") / "site"
    with pytest.MonkeyPatch.context() as mp:
        for name in ("AQDT_MAP_PM25_MAX", "AQDT_MAP_SD_MAX", "AQDT_BBOX"):
            mp.delenv(name, raising=False)
        summary = build(archive, out, hours=5, end=H14 + H)
    return out, summary
