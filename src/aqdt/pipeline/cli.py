"""The ``aqdt`` command: one entry point for every run, whatever triggers it.

Each sub-command resolves its window, constructs only the settings it needs, opens a PostGIS
connection when the environment offers one, invokes the run, and prints one JSON line.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from aqdt.airnow.ingest import ingest_airnow
from aqdt.airnow.models import AirNowSettings
from aqdt.calibration.apply import apply_calibrations
from aqdt.calibration.fit import fit_calibrations
from aqdt.calibration.schemas import CalibrationSettings, FitStatus
from aqdt.observation_store.archive import resolve_archive_uri
from aqdt.observation_store.postgis import DATABASE_URL_VAR, apply_schema, connect, rebuild
from aqdt.pipeline.windows import DEFAULT_HOURS, resolve_as_of, resolve_window
from aqdt.purpleair.ingest import ingest_purpleair
from aqdt.purpleair.models import PurpleAirSettings

log = logging.getLogger("aqdt.pipeline")


# @spec PIPE-CLI-012
def _timestamp(text: str) -> datetime:
    """ISO 8601; a value without an offset is UTC."""
    try:
        value = datetime.fromisoformat(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"not an ISO 8601 timestamp: {text!r}") from error
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _positive_int(text: str) -> int:
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _add_window_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--start", type=_timestamp, help="window start (ISO 8601, UTC if naive)")
    parser.add_argument("--end", type=_timestamp, help="window end, exclusive")
    parser.add_argument(
        "--hours", type=_positive_int, help=f"trailing window length (default {DEFAULT_HOURS})"
    )


# @spec PIPE-CLI-002, PIPE-CLI-008
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aqdt", description="Air quality digital twin runs.")
    parser.add_argument("--archive-uri", help="archive root (default: AQDT_ARCHIVE_URI)")
    parser.add_argument(
        "--no-postgis", action="store_true", help="never load PostGIS, even if DATABASE_URL is set"
    )
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="fetch a source into the archive")
    sources = ingest.add_subparsers(dest="source", required=True)
    sources.add_parser("purpleair", help="one snapshot of the bounding box")
    airnow = sources.add_parser("airnow", help="a window of hourly monitor values")
    _add_window_options(airnow)

    calibrate = commands.add_parser("calibrate", help="fit or apply sensor calibrations")
    steps = calibrate.add_subparsers(dest="step", required=True)
    fit = steps.add_parser("fit", help="refit every sensor for an as_of")
    fit.add_argument("--as-of", type=_timestamp, help="fit as_of (default: last 00:00 UTC)")
    apply_ = steps.add_parser("apply", help="calibrate sensor hours in a window")
    _add_window_options(apply_)

    db = commands.add_parser("db", help="PostGIS serving layer")
    actions = db.add_subparsers(dest="action", required=True)
    actions.add_parser("schema", help="create the tables")
    actions.add_parser("rebuild", help="reload every table from the archive")
    return parser


# @spec PIPE-CLI-011
def _check_window_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if not hasattr(args, "start"):
        return
    if (args.start is None) != (args.end is None):
        parser.error("--start and --end must be given together")
    if args.hours is not None and args.start is not None:
        parser.error("--hours cannot be combined with --start/--end")


class _StderrHandler(logging.StreamHandler):
    """Writes to whatever ``sys.stderr`` is at emit time, so redirection is honoured."""

    @property
    def stream(self):  # type: ignore[override]
        return sys.stderr

    @stream.setter
    def stream(self, value) -> None:
        pass


# @spec PIPE-CLI-009
def _configure_logging(level: str) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level))
    if not any(isinstance(handler, _StderrHandler) for handler in root.handlers):
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class _Connection:
    """Opens the PostGIS connection when the environment offers one; closes it afterwards."""

    def __init__(self, disabled: bool):
        self.conn: Any = None
        self.disabled = disabled

    # @spec PIPE-PG-001, PIPE-PG-002
    def __enter__(self) -> Any:
        if not self.disabled and os.environ.get(DATABASE_URL_VAR):
            self.conn = connect()
        return self.conn

    def __exit__(self, *exc_info: object) -> None:
        if self.conn is not None:
            self.conn.close()


# @spec PIPE-CLI-003, PIPE-CLI-004, PIPE-CLI-005, PIPE-CLI-006, PIPE-CLI-007, PIPE-CLI-010
# @spec PIPE-WIN-005, PIPE-OUT-001, PIPE-OUT-002, PIPE-OUT-003
def _dispatch(args: argparse.Namespace, archive_uri: str, now: datetime) -> dict[str, Any] | None:
    """Run the requested command and return what to print (``None`` for the db commands)."""
    if args.command == "db":
        url = os.environ.get(DATABASE_URL_VAR)
        if not url:
            raise RuntimeError(f"{DATABASE_URL_VAR} is not set; the db commands need PostGIS")
        with _Connection(disabled=False) as conn:
            if args.action == "schema":
                apply_schema(conn)
            else:
                rebuild(conn, archive_uri)
        return None

    if args.command == "ingest" and args.source == "purpleair":
        settings = PurpleAirSettings()
        with _Connection(args.no_postgis) as conn:
            summary = ingest_purpleair(settings, archive_uri, conn=conn)
        return summary.model_dump(mode="json")

    if args.command == "ingest" and args.source == "airnow":
        settings = AirNowSettings()
        start, end = resolve_window(now, args.start, args.end, args.hours)
        log.info("ingest airnow window [%s, %s)", _iso(start), _iso(end))
        with _Connection(args.no_postgis) as conn:
            summary = ingest_airnow(settings, archive_uri, start, end, conn=conn)
        return summary.model_dump(mode="json")

    if args.command == "calibrate" and args.step == "fit":
        settings = CalibrationSettings()
        as_of = resolve_as_of(now, args.as_of)
        log.info("calibrate fit as_of %s", _iso(as_of))
        with _Connection(args.no_postgis) as conn:
            fits = fit_calibrations(archive_uri, as_of, settings, conn=conn)
        counts = Counter(fit.status for fit in fits)
        return {
            "as_of": _iso(as_of),
            "window_start": _iso(as_of - timedelta(days=settings.window_days)),
            "window_end": _iso(as_of),
            "fits": {status.value: counts.get(status, 0) for status in FitStatus},
        }

    settings = CalibrationSettings()
    start, end = resolve_window(now, args.start, args.end, args.hours)
    log.info("calibrate apply window [%s, %s)", _iso(start), _iso(end))
    with _Connection(args.no_postgis) as conn:
        frame = apply_calibrations(archive_uri, start, end, settings, conn=conn)
    return {"window_start": _iso(start), "window_end": _iso(end), "rows": int(len(frame))}


# @spec PIPE-CLI-001, PIPE-OUT-004, PIPE-OUT-005, PIPE-PG-003
def main(argv: Sequence[str] | None = None, now: datetime | None = None) -> int:
    """Entry point. Exit 0 with one JSON line on stdout, or non-zero with the error on stderr."""
    parser = build_parser()
    args = parser.parse_args(argv)
    _check_window_options(parser, args)
    _configure_logging(args.log_level)
    try:
        archive_uri = resolve_archive_uri(args.archive_uri)
        result = _dispatch(args, archive_uri, now or datetime.now(UTC))
    except Exception:
        log.exception("run failed")
        return 1
    if result is not None:
        print(json.dumps(result, sort_keys=True))
    return 0
