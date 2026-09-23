"""The aqdt command: dispatch, options, output, exit status, PostGIS loading — PIPE-CLI,
PIPE-OUT, PIPE-PG, PIPE-WIN-005, PIPE-CFG-002."""

import json
import logging
import subprocess
import sys
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from aqdt.airnow.models import AirNowSettings
from aqdt.calibration.schemas import CalibrationFit, CalibrationSettings, FitStatus
from aqdt.purpleair.models import PurpleAirSettings

from .conftest import ARCHIVE, MIDNIGHT, NOW_H, run

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / "src" / "aqdt" / "pipeline"
H = timedelta(hours=1)


# --- Entry point and sub-commands ---------------------------------------------


# @spec PIPE-CLI-001
def test_console_script_and_module_alias():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["scripts"]["aqdt"] == "aqdt.pipeline.cli:main"
    assert (ROOT / "src" / "aqdt" / "__main__.py").exists()
    result = subprocess.run(
        [sys.executable, "-m", "aqdt", "--help"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0
    assert "ingest" in result.stdout and "calibrate" in result.stdout


# @spec PIPE-CLI-002
def test_exactly_the_six_sub_commands(env, stubs):
    for argv in (
        ["ingest", "purpleair"],
        ["ingest", "airnow"],
        ["calibrate", "fit"],
        ["calibrate", "apply"],
    ):
        assert run(argv) == 0
    for argv in (["ingest", "nope"], ["calibrate", "nope"], ["db", "nope"], ["nope"]):
        with pytest.raises(SystemExit) as exc:
            run(argv)
        assert exc.value.code == 2


# @spec PIPE-CLI-003
def test_ingest_purpleair_dispatch(env, stubs):
    assert run(["ingest", "purpleair"]) == 0
    call = stubs.only("ingest_purpleair")
    settings, archive_uri = call.args[:2]
    assert isinstance(settings, PurpleAirSettings) and settings.api_key == "pa-key"
    assert archive_uri == ARCHIVE
    assert call.kwargs.get("conn") is None


# @spec PIPE-CLI-004
def test_ingest_airnow_dispatch_with_routine_window(env, stubs):
    assert run(["ingest", "airnow"]) == 0
    call = stubs.only("ingest_airnow")
    settings, archive_uri, start, end = call.args[:4]
    assert isinstance(settings, AirNowSettings) and settings.api_key == "an-key"
    assert archive_uri == ARCHIVE
    assert (start, end) == (NOW_H - 48 * H, NOW_H)
    assert call.kwargs.get("conn") is None


# @spec PIPE-CLI-005
def test_calibrate_fit_dispatch(env, stubs):
    assert run(["calibrate", "fit"]) == 0
    call = stubs.only("fit_calibrations")
    archive_uri, as_of, settings = call.args[:3]
    assert archive_uri == ARCHIVE
    assert as_of == MIDNIGHT
    assert isinstance(settings, CalibrationSettings)
    assert call.kwargs.get("conn") is None


# @spec PIPE-CLI-006
def test_calibrate_apply_dispatch(env, stubs):
    assert run(["calibrate", "apply", "--hours", "6"]) == 0
    call = stubs.only("apply_calibrations")
    archive_uri, start, end, settings = call.args[:4]
    assert archive_uri == ARCHIVE
    assert (start, end) == (NOW_H - 6 * H, NOW_H)
    assert isinstance(settings, CalibrationSettings)


# @spec PIPE-CLI-007
def test_db_commands_need_database_url(env, stubs, monkeypatch, capsys):
    assert run(["db", "schema"]) != 0
    assert "DATABASE_URL" in capsys.readouterr().err
    assert not [c for c in stubs.calls if c.name in ("apply_schema", "rebuild")]

    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert run(["db", "schema"]) == 0
    assert stubs.only("apply_schema").args == (stubs.connection,)
    assert run(["db", "rebuild"]) == 0
    assert stubs.only("rebuild").args == (stubs.connection, ARCHIVE)
    assert stubs.connection.closed


# @spec PIPE-CLI-008
def test_archive_uri_is_resolved_before_dispatch(env, stubs, monkeypatch, capsys):
    assert run(["--archive-uri", "/elsewhere", "ingest", "purpleair"]) == 0
    assert stubs.only("ingest_purpleair").args[1] == "/elsewhere"

    monkeypatch.delenv("AQDT_ARCHIVE_URI")
    stubs.calls.clear()
    assert run(["ingest", "purpleair"]) != 0
    assert "AQDT_ARCHIVE_URI" in capsys.readouterr().err
    assert stubs.calls == []


# @spec PIPE-CLI-009
def test_log_level_configures_logging(env, stubs):
    assert run(["--log-level", "DEBUG", "ingest", "purpleair"]) == 0
    assert logging.getLogger().level == logging.DEBUG
    assert run(["ingest", "purpleair"]) == 0
    assert logging.getLogger().level == logging.INFO


# @spec PIPE-CLI-013
@pytest.mark.parametrize("level", ["DEBUG", "INFO"])
def test_http_client_loggers_never_log_request_urls(env, stubs, level):
    assert run(["--log-level", level, "ingest", "airnow"]) == 0
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        assert not logger.isEnabledFor(logging.INFO), name


# @spec PIPE-CLI-010
def test_each_command_constructs_only_its_own_settings(env, stubs, monkeypatch):
    monkeypatch.delenv("PURPLEAIR_API_KEY")
    assert run(["ingest", "airnow"]) == 0
    assert run(["calibrate", "fit"]) == 0
    assert run(["calibrate", "apply"]) == 0
    monkeypatch.delenv("AIRNOW_API_KEY")
    assert run(["calibrate", "fit"]) == 0
    assert run(["ingest", "airnow"]) != 0


# @spec PIPE-CLI-011
@pytest.mark.parametrize(
    "argv",
    [
        ["ingest", "airnow", "--start", "2026-09-20T10:00"],
        ["ingest", "airnow", "--end", "2026-09-20T10:00"],
        [
            "ingest",
            "airnow",
            "--hours",
            "6",
            "--start",
            "2026-09-20T10:00",
            "--end",
            "2026-09-20T14:00",
        ],
        ["ingest", "airnow", "--hours", "0"],
        ["calibrate", "apply", "--hours", "-3"],
    ],
)
def test_inconsistent_window_options_are_usage_errors(env, stubs, argv):
    with pytest.raises(SystemExit) as exc:
        run(argv)
    assert exc.value.code == 2
    assert stubs.calls == []


# @spec PIPE-CLI-012
def test_timestamps_are_iso_8601_and_naive_means_utc(env, stubs):
    assert (
        run(["ingest", "airnow", "--start", "2026-09-20T10:00", "--end", "2026-09-20T14:00"]) == 0
    )
    start, end = stubs.only("ingest_airnow").args[2:4]
    assert start == datetime(2026, 9, 20, 10, tzinfo=UTC)
    assert end == datetime(2026, 9, 20, 14, tzinfo=UTC)
    stubs.calls.clear()
    assert run(["calibrate", "fit", "--as-of", "2026-09-20T02:00+02:00"]) == 0
    assert stubs.only("fit_calibrations").args[1] == datetime(2026, 9, 20, 0, tzinfo=UTC)


# @spec PIPE-WIN-005
def test_resolved_window_is_logged_before_the_run(env, stubs, caplog):
    seen_at_call: list[str] = []
    real = stubs.make("ingest_airnow")

    def recording(*args, **kwargs):
        seen_at_call.append(caplog.text)
        return real(*args, **kwargs)

    stubs.results["ingest_airnow"] = stubs.results["ingest_airnow"]
    import aqdt.pipeline.cli as cli

    cli.ingest_airnow = recording
    with caplog.at_level(logging.INFO):
        assert run(["ingest", "airnow", "--hours", "6"]) == 0
    (text,) = seen_at_call
    assert "2026-09-21T08:00:00Z" in text and "2026-09-21T14:00:00Z" in text


# --- Output and exit status ---------------------------------------------------


# @spec PIPE-OUT-001
def test_ingest_prints_the_summary_as_one_json_line(env, stubs, capsys):
    assert run(["ingest", "purpleair"]) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert json.loads(out) == stubs.results["ingest_purpleair"].model_dump(mode="json")
    assert json.loads(out)["snapshot_at"] == "2026-09-21T14:00:00Z"


# @spec PIPE-OUT-002
def test_fit_prints_counts_by_status(env, stubs, capsys):
    def fit(site_id, status):
        return CalibrationFit(
            site_id=site_id,
            as_of=MIDNIGHT,
            status=status,
            ref_site_id=None,
            distance_m=None,
            window_start=MIDNIGHT - timedelta(days=30),
            window_end=MIDNIGHT,
            n_pairs=0,
            slope=None,
            intercept=None,
            r2=None,
            rmse=None,
        )

    stubs.results["fit_calibrations"] = [
        fit("purpleair:1", FitStatus.fitted),
        fit("purpleair:2", FitStatus.pooled),
        fit("purpleair:3", FitStatus.pooled),
    ]
    assert run(["calibrate", "fit"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "as_of": "2026-09-21T00:00:00Z",
        "window_start": "2026-08-22T00:00:00Z",
        "window_end": "2026-09-21T00:00:00Z",
        "fits": {"fitted": 1, "pooled": 2, "uncalibrated": 0},
    }


# @spec PIPE-OUT-003
def test_apply_prints_window_and_row_count(env, stubs, capsys):
    stubs.results["apply_calibrations"] = pd.DataFrame({"site_id": ["a", "b", "c"]})
    assert run(["calibrate", "apply", "--hours", "6"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "window_start": "2026-09-21T08:00:00Z",
        "window_end": "2026-09-21T14:00:00Z",
        "rows": 3,
    }


# @spec PIPE-OUT-004
# @spec PIPE-OUT-005
def test_a_failing_run_exits_non_zero_logs_and_prints_nothing(env, stubs, capsys):
    stubs.errors["ingest_purpleair"] = RuntimeError("PurpleAir request failed with HTTP 503")
    code = run(["ingest", "purpleair"])
    captured = capsys.readouterr()
    assert code != 0
    assert captured.out == ""
    assert "PurpleAir request failed with HTTP 503" in captured.err
    assert len([c for c in stubs.calls if c.name == "ingest_purpleair"]) == 1


# --- PostGIS loading ----------------------------------------------------------


# @spec PIPE-PG-001
def test_database_url_means_load_and_close(env, stubs, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert run(["ingest", "purpleair"]) == 0
    assert stubs.only("ingest_purpleair").kwargs["conn"] is stubs.connection
    assert stubs.connection.closed


# @spec PIPE-PG-002
def test_no_database_url_or_no_postgis_means_archive_only(env, stubs, monkeypatch):
    assert run(["calibrate", "apply"]) == 0
    assert stubs.only("apply_calibrations").kwargs["conn"] is None
    assert not [c for c in stubs.calls if c.name == "connect"]
    stubs.calls.clear()
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    assert run(["--no-postgis", "calibrate", "apply"]) == 0
    assert stubs.only("apply_calibrations").kwargs["conn"] is None
    assert not [c for c in stubs.calls if c.name == "connect"]


# @spec PIPE-PG-003
def test_load_failure_after_archive_write_is_non_zero_and_closes(env, stubs, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    stubs.errors["ingest_airnow"] = RuntimeError("could not connect to server")
    assert run(["ingest", "airnow"]) != 0
    assert "could not connect" in capsys.readouterr().err
    assert stubs.connection.closed


# --- Configuration ------------------------------------------------------------


# @spec PIPE-CFG-002
def test_no_configuration_files_are_read():
    source = "\n".join(p.read_text() for p in PACKAGE.rglob("*.py"))
    assert "env_file" not in source and "dotenv" not in source
