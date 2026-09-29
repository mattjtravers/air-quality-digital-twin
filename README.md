# Air Quality Digital Twin — DC Metro

A real-time digital twin fusing crowdsourced and regulatory air quality
sensor networks for the Washington, D.C. metro area, built for GGS 590
(Digital Twins) at George Mason University.

## What this is

PurpleAir's low-cost sensor network gives dense spatial coverage but
individually noisy readings; EPA AirNow's regulatory monitors are trusted
but sparse. This project ingests both, calibrates each PurpleAir sensor
against its nearest eligible AirNow monitor, and produces a single
corrected, queryable observation stream for the region — the foundation
for later transport modeling and forecasting.

**Status:** actively under development. Ingestion and calibration are
running; a NOAA HRRR-driven transport/forecasting layer is planned for a
later phase.

## How it works

- **Ingestion.** PurpleAir and AirNow are both pulled hourly, matching the
  model's hourly time step. Both are quality-checked and written to a
  partitioned GeoParquet archive in S3, the system of record.
- **Calibration.** Each PurpleAir sensor is matched to its nearest AirNow
  monitor within a 10 km radius (UTM 18N / EPSG:26918). A per-sensor OLS
  regression against that monitor is accepted once it has at least 72
  hourly pairs and a positive slope; sensors that don't yet qualify, or
  have no monitor in range, use a pooled, network-wide fallback fit built
  from already-accepted sensors. Fits refit daily over a rolling 30-day
  window.
- **Serving.** A PostGIS database, rebuildable from the S3 archive at any
  time, serves calibrated hourly observations for querying and mapping.

## Development approach

This project follows [Linked-Intent Development](https://github.com/jszmajda/lid),
a spec-driven workflow where every change flows through an explicit chain
of intent: high-level design → low-level design → EARS requirements →
tests → code. Design documents live under `docs/`, starting with
`docs/high-level-design.md`.

## Tech stack

Python, [uv](https://docs.astral.sh/uv/), PostGIS, AWS S3 (GeoParquet
archive), GitHub Actions (scheduled via AWS EventBridge), GitHub
Codespaces for development.

## Course context

Built incrementally across weekly assignments for GGS 590: Digital Twins
at George Mason University, Fall 2026.
