---
parent: high-level-design
prefix: AN
---

# AirNow Ingest

## Context and Design Philosophy

AirNow is the sparse, trusted layer of the twin: EPA's near-real-time feed of regulatory reference
monitors, a handful of sites in the D.C. metro bounding box reporting hourly PM2.5. These are the
monitors every PurpleAir sensor is eventually calibrated against, so their identity and their
hourly values must be exactly right — and the feed does not make that easy. AirNow labels its data
preliminary and unvalidated; hours are revised after first publication; a site can report a
physically implausible unchanging value for hours (a stuck monitor); and the site identifiers come
in inconsistent shapes (with and without the country prefix, occasionally with a lost leading
zero) that would defeat a naive join.

This component owns everything AirNow-specific: the API client and its date-windowed query, the
payload shape, site-identifier normalization, flatline detection, and the mapping to the canonical
`Site` and `Observation` records handed to the observation store. Reference monitors receive no
correction; their value is the reference. Like every ingester, it flags rather than filters.

## API Contract

| Item | Value |
|---|---|
| Endpoint | `GET https://www.airnowapi.org/aq/data/` |
| Auth | `API_KEY` query parameter from `AIRNOW_API_KEY` |
| Spatial filter | `BBOX=minLon,minLat,maxLon,maxLat` from the project bounding box (`nwlng,selat,selng,nwlat`) |
| Time window | `startDate = start`, `endDate = end − 1 h`, as `YYYY-MM-DDTHH` UTC; the API's hours are inclusive, the project's windows are half-open `[start, end)` |
| Parameter | `parameters=PM25` |
| Data type | `dataType=B` (concentration and AQI) |
| Monitor type | `monitorType=0` (permanent monitors only) |
| Detail | `verbose=1` (site name, agency, AQS codes), `includerawconcentrations=1` |
| Format | `format=application/json` |
| Timeout | 30 s |
| Retry | Up to 3 attempts with exponential backoff on 429, 5xx, and connection/timeout errors; any other 4xx fails the run immediately. When retries are exhausted the run fails with an error carrying the last status or exception |
| Chunking | Windows longer than 24 hours are fetched as consecutive half-open 24-hour requests `[t, t + 24 h)` |

The response is a JSON array of row objects, one per site per hour per parameter. Field
semantics:

- `UTC` — the hour the value is for, `YYYY-MM-DDTHH:MM`, UTC. It labels the *start* of the
  averaging hour: a value stamped `17:00` was measured from 17:00 to 17:59 UTC (AirNow Hourly
  Data fact sheet). AirNow revises the preceding 48 hours on every hourly update, so any window
  fetched within two days of the present may return values that differ from an earlier fetch.
- `Parameter` — `PM2.5` for the rows requested.
- `Value` — the hourly concentration in `Unit` (`UG/M3`). `-999` is AirNow's missing-value
  sentinel.
- `RawConcentration` — the concentration before AirNow's own processing; `-999` when missing.
- `AQI`, `Category` — the AQI value and category number derived from the hour.
- `FullAQSCode` — the 9-digit AQS site identifier (2-digit state, 3-digit county, 4-digit site).
- `IntlAQSCode` — the same identifier with a 3-digit country prefix (`840` for the United States).
- `SiteName`, `AgencyName`, `Latitude`, `Longitude`.

`dataType=B` is required because fusion operates on concentrations, not AQI; AQI is retained in
`raw`. Mobile monitors are excluded at the query because a moving monitor has no fixed site
identity to join on.

## Payload Model

`AirNowRow` (Pydantic) is the boundary model for one response row:

| Field | Type | Validation |
|---|---|---|
| `utc` | `datetime` | alias `UTC`; required; parsed as tz-aware UTC |
| `parameter` | `str` | alias `Parameter`; must equal `PM2.5` |
| `latitude` | `float` | alias `Latitude`; required, in [-90, 90] |
| `longitude` | `float` | alias `Longitude`; required, in [-180, 180] |
| `value` | `float \| None` | alias `Value`; `-999` → `None` |
| `raw_concentration` | `float \| None` | alias `RawConcentration`; `-999` → `None` |
| `aqi` | `int \| None` | alias `AQI`; `-999` → `None` |
| `category` | `int \| None` | alias `Category` |
| `unit` | `str` | alias `Unit` |
| `site_name` | `str \| None` | alias `SiteName` |
| `agency_name` | `str \| None` | alias `AgencyName` |
| `full_aqs_code` | `str \| None` | alias `FullAQSCode`; coerced to string, whitespace stripped |
| `intl_aqs_code` | `str \| None` | alias `IntlAQSCode`; coerced to string, whitespace stripped |

Boundary rejections (counted with a reason, not turned into observations): missing or unparseable
`UTC`, missing coordinates, a `Parameter` other than `PM2.5`, a `Unit` other than `UG/M3`, and a
second row for the same site and hour within one run (the first is kept; the rest are counted
under `duplicate_site_hour`). "Same site" is judged on the normalized identifier, so the duplicate
check runs after normalization. A null `Value` is valid at the boundary and becomes a flag.

## Site Identifier Normalization

The canonical identifier is the 12-digit international AQS code: 3-digit country + 2-digit state
+ 3-digit county + 4-digit site. `site_id` is `airnow:{12 digits}`.

Normalization takes both codes as received and applies, to each:

| Digits | Interpretation | Normalized |
|---|---|---|
| 12 | international code | as is |
| 11 | international code with a lost leading zero in the state | first 3 digits + `0` + remaining 8 |
| 9 | full AQS code | `840` + code |
| 8 | full AQS code with a lost leading zero in the state | `840` + `0` + code |
| other, or non-numeric | unresolvable | — |

The result is the normalized value the two codes agree on. If only one code is present or
resolvable, its result is used. If both resolve and disagree, or neither resolves, the site is
unresolved: `site_id` becomes `airnow:unresolved:{IntlAQSCode or FullAQSCode as received, or
SiteName}` and every observation from that site carries `site_id_unresolved`.

The `840` prefix assumes United States sites, which the D.C. bounding box guarantees; a 12-digit
code with any country prefix is accepted unchanged.

## Quality Control

| Flag | Condition |
|---|---|
| `missing_value` | `Value` is null (AirNow sent `-999`). |
| `out_of_range` | `Value` is `< 0` or `> 1000` µg/m³. |
| `flatline` | For a site, `Value` is non-null and identical across at least `flatline_hours` (default 3) consecutive reported hours; every hour in such a run is flagged. A gap in reported hours or a null value breaks the run. |
| `site_id_unresolved` | As above. |

Flatline detection needs the hours before the requested window. The run therefore fetches
`[start − (flatline_hours − 1) h, end)` and evaluates flatlines over the whole fetched series per
site. Every fetched hour is written — the earlier hours re-emitted with whatever flags the longer
series now justifies, and with any upstream revisions AirNow has published since — and the
store's incoming-wins merge applies them.

The trailing edge is the mirror case: a flatline that begins in the last `flatline_hours − 1`
hours of a window cannot be recognised until a later run re-fetches those hours. Routine runs
overlap their predecessor by far more than that, so the flags arrive one run late rather than
never; this is expected, not a defect.

No corrected value is produced: `pm25_corrected` is null for reference monitors, and `pm25_raw`
is the value calibration treats as truth.

## Mapping to Canonical Records

`Site`:

| Canonical | Source |
|---|---|
| `site_id` | normalized, as above |
| `source` | `airnow` |
| `source_native_id` | `FullAQSCode` as received, else `IntlAQSCode`, else `SiteName` |
| `site_type` | `reference_monitor` |
| `name` | `SiteName` |
| `latitude`, `longitude` | as reported, unrounded |

`Observation`:

| Canonical | Source |
|---|---|
| `site_id`, `source`, `latitude`, `longitude` | as for `Site` |
| `observed_at` | `UTC` |
| `pm25_raw` | `Value` (null when `-999`) |
| `pm25_channel_a`, `pm25_channel_b`, `humidity`, `pm25_corrected` | null |
| `qc_flags` | as above |
| `raw` | the response row, unmodified (including `AQI`, `Category`, `RawConcentration`, `AgencyName`, both AQS codes) |

## Run

`ingest_airnow(settings, archive_uri, start, end, conn=None) -> IngestSummary`:

1. Extend the window backwards by `flatline_hours − 1` hours to `[start − (flatline_hours − 1) h, end)`;
   fetch in 24-hour chunks.
2. Validate each row into `AirNowRow`; collect boundary rejections.
3. Normalize site identifiers; reject site-hour duplicates; group rows by site and sort by hour.
4. Apply QC per site; build `Site` and `Observation` records.
5. Write sites then observations to the store.
6. If a PostGIS connection was given, load the partitions just written (`load_partitions`).
7. Return the summary.

`start` and `end` are explicit, tz-aware UTC, truncated to the hour, and bound the half-open
window `[start, end)`; a naive datetime fails validation. The caller chooses the window — a
routine run covers the last 48 hours, matching the period AirNow re-issues on every hourly
update, so each run also collects the revisions to preliminary data; a backfill passes a larger
window. A response with zero rows is a successful run reporting `fetched=0`.

`IngestSummary` is the observation store's summary model, with `window_start`/`window_end` set
to the requested (unextended) half-open window and `snapshot_at` unset. `written` counts every
fetched hour handed to the store, lookback hours included, so `fetched == written + Σ rejected`.

Settings (`AirNowSettings`, Pydantic settings from the environment): `AIRNOW_API_KEY`
(required), `bbox` (the store's `DC_METRO` unless `AQDT_BBOX` overrides it), `flatline_hours`,
and the constants in the API contract table as overridable defaults. Constructing the settings
with the API key unset fails immediately with an error naming the variable.

## Package Layout

```
src/aqdt/airnow/
  client.py     # fetch_rows(settings, start, end) -> list[dict]
  models.py     # AirNowRow, AirNowSettings
  sites.py      # normalize_site_id
  qc.py         # flags incl. flatline
  ingest.py     # ingest_airnow (entry point)
tests/airnow/
tests/fixtures/airnow/   # recorded responses: clean, AQI-0 flatline, malformed AQS codes
```

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| `dataType` | `B` (concentration + AQI) | `A` (AQI only); `C` (concentration only) | Fusion and calibration need µg/m³; AQI is a categorical transform of concentration and cannot be inverted exactly. `B` keeps AQI in `raw` at no cost. |
| Canonical site identifier | 12-digit international AQS code | 9-digit full AQS code; `SiteName`; AirNow's own site id | The international code is the most specific published identifier and is what AQS itself uses; site names change and are not unique. Normalizing *to* the longer form loses nothing. |
| Lost-leading-zero repair | Pad 8→9 and 11→12 digits | Reject as unresolvable | A numeric-typed identifier losing its leading zero is a known, mechanical failure mode of upstream pipelines; the repair is unambiguous for AQS codes, whose fixed-width structure makes the missing position certain. |
| Conflicting codes | Unresolved, flagged | Prefer `IntlAQSCode`; prefer `FullAQSCode` | Two identifiers that disagree after normalization mean the row's identity is genuinely uncertain; guessing would silently join observations to the wrong site. |
| Flatline lookback | Over-fetch `flatline_hours − 1` hours before the window | Read prior hours from the archive | Over-fetching removes a read dependency on the archive, and re-fetching recent hours also picks up AirNow's revisions to preliminary data, which the archive wants anyway. |
| Flatline threshold | 3 consecutive identical hours, configurable | 6; 12; zero-only rule | Matches the observed fault; hourly urban PM2.5 at 0.1 µg/m³ resolution rarely repeats three times by chance, and a false positive only moves a reading out of the trusted set, it does not remove it. |
| Duplicate site-hour in one run | Keep first, count the rest as boundary rejections | Fail the run; keep last | AirNow does not expose the AQS parameter-occurrence code, so two co-located PM2.5 instruments at one site can legitimately appear as two rows; failing the run for a plausible upstream shape is too brittle, and dropping silently hides it. The count makes it visible. |
| Mobile monitors | Excluded at the query (`monitorType=0`) | Ingest and flag | A mobile monitor has no fixed site identity; nothing downstream could join it. |
| Corrected value | Null | Copy `pm25_raw` | Reference monitors are the reference; a corrected column equal to the raw one implies a correction happened. |
| Routine window | 48 hours | 24 hours; since the last run | AirNow re-issues the preceding 48 hours on every hourly update; a 48-hour window collects every revision at the cost of one extra request per run. |
| Unresolved-site key | `IntlAQSCode`, else `FullAQSCode`, else `SiteName` as received | Coordinates; a hash of the row | Two distinct unresolvable sites sharing a `SiteName` — reachable only when both codes are absent — would merge into one flagged site and count as duplicates. Accepted: the metro's monitors always carry both codes, and an unresolved site is already excluded from trusted data. |
| Request chunking | 24-hour requests | One request per window; per-hour requests | Keeps each request well inside AirNow's response and rate limits while making a week-long backfill seven calls, not 168. |
| HTTP client | `httpx` (sync) | `requests` | Same reasoning as the PurpleAir ingester; one client library across ingesters. |

## Open Questions & Future Decisions

### Resolved

1. ✅ `Value` and `RawConcentration` sentinels (`-999`) become nulls at the boundary and surface
   as `missing_value`, not `out_of_range`.
2. ✅ A hour missing from a site's series breaks a flatline run rather than bridging it.
3. ✅ No future-timestamp check: the query's `endDate` bounds what AirNow returns, and there is
   no response-level timestamp to compare against without depending on run time.
4. ✅ `UTC` labels the start of the averaging hour (AirNow Hourly Data fact sheet), so
   `observed_at = UTC` with no shift, and the canonical timestamp is always the hour start.

### Deferred

1. Tune `flatline_hours` after observing the false-positive rate on real data.
2. Whether `RawConcentration` (AirNow's pre-processing value) should be promoted to a schema
   column if calibration prefers it to `Value`. It is in `raw` meanwhile.
3. Persisting boundary rejections (shared with the observation store's open question).

## References

- EPA AirNow API, Data Query (`/aq/data/`) — parameters, `dataType`, `monitorType`,
  `verbose`, `-999` sentinel.
- AirNow Hourly Data File fact sheet (`docs.airnowapi.org/docs/HourlyDataFactSheet.pdf`) —
  timestamps are GMT and mark the beginning of the measurement period; the preceding 48 hours
  are re-issued every hour.
- EPA AQS site identifier structure (state–county–site; international prefix `840`).
- `docs/intent/observation-store/observation-store-design.md` — canonical records, flag
  vocabulary, `BoundingBox`, write semantics.
