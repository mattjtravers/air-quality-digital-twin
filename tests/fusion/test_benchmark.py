"""The resolution benchmark — FUS-BENCH."""

import json
import os
from pathlib import Path

import pytest

from aqdt.fusion import benchmark

from .conftest import H10


def _run(capsys, argv):
    assert benchmark.main(argv) == 0
    return capsys.readouterr().out


# @spec FUS-BENCH-001
# @spec FUS-BENCH-002
def test_synthetic_benchmark_reports_each_resolution_from_its_own_subprocess(capsys):
    out = _run(capsys, ["--synthetic-points", "40", "--resolutions", "5000", "2500", "--json"])
    lines = [json.loads(line) for line in out.splitlines() if line.startswith("{")]
    machine, *rows = lines
    assert [r["resolution_m"] for r in rows] == [5000, 2500]
    for r in rows:
        assert r["cells"] > 0
        assert r["total_s"] >= r["predict_s"] > 0
        assert r["peak_rss_mib"] > 0
        assert r["pid"] != os.getpid()
    assert rows[0]["pid"] != rows[1]["pid"]
    assert rows[1]["cells"] > rows[0]["cells"]


# @spec FUS-BENCH-001
def test_default_resolutions_and_point_count():
    args = benchmark.parse_args([])
    assert args.resolutions == [500, 200, 100, 50]
    assert args.synthetic_points == 100
    assert args.hour is None


# @spec FUS-BENCH-001
# @spec FUS-BENCH-004
def test_archive_hour_benchmark_writes_nothing(archive_uri, capsys):
    before = sorted(p for p in Path(archive_uri).rglob("*") if p.is_file())
    out = _run(
        capsys,
        [
            "--archive-uri",
            archive_uri,
            "--hour",
            H10.isoformat(),
            "--resolutions",
            "5000",
            "--json",
        ],
    )
    after = sorted(p for p in Path(archive_uri).rglob("*") if p.is_file())
    assert before == after
    rows = [json.loads(line) for line in out.splitlines() if line.startswith("{")][1:]
    assert rows[0]["n_points"] > 0


# @spec FUS-BENCH-003
def test_machine_and_versions_are_reported(capsys):
    out = _run(capsys, ["--synthetic-points", "30", "--resolutions", "5000", "--json"])
    machine = json.loads(out.splitlines()[0])
    for key in ("cpu_model", "logical_cores", "memory_gib", "python", "pykrige", "gdal"):
        assert machine[key], key
    table = _run(capsys, ["--synthetic-points", "30", "--resolutions", "5000"])
    assert machine["cpu_model"] in table
    for header in ("resolution", "cells", "total", "predict", "peak"):
        assert header in table.lower()


# @spec FUS-BENCH-001
def test_hour_requires_an_archive(monkeypatch):
    monkeypatch.delenv("AQDT_ARCHIVE_URI", raising=False)
    with pytest.raises(SystemExit):
        benchmark.main(["--hour", H10.isoformat(), "--resolutions", "5000"])
