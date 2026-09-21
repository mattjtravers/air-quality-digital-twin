---
parent: high-level-design
prefix: PA
---

# PurpleAir Ingest

## Context and Design Philosophy

PurpleAir sensors are the dense layer of the twin: on the order of a hundred outdoor sensors in the
D.C. metro bounding box, each reporting roughly every two minutes. They are also the noisy layer.
Each sensor has two independent laser particle counters (channels A and B) measuring the same air;
when the channels agree the reading is usable, and when they diverge — one channel drifting into
the thousands of µg/m³ while the other reads single digits — the sensor has a hardware fault and
the reading must be flagged. Readings also need humidity correction before they are comparable to
regulatory monitors; EPA publishes a nationwide correction for exactly this sensor.

This component owns everything PurpleAir-specific: the API client, the payload shape, the
dual-channel and range checks, the EPA correction, and the mapping to the canonical `Site` and
`Observation` records it hands to the observation store. It applies published EPA criteria rather
than project-specific thresholds, and it flags rather than filters: a faulty reading is written
with its flags and its raw channel values intact.

## API Contract

| Item | Value |
|---|---|
| Endpoint | `GET https://api.purpleair.com/v1/sensors` |
| Auth | `X-API-Key` header, a READ key, from `PURPLEAIR_API_KEY` |
| Spatial filter | `nwlng`, `nwlat`, `selng`, `selat` from the project bounding box |
| Sensor filter | `location_type=0` (outdoor sensors only) |
| Freshness filter | `max_age=86400` (sensors not seen in 24 h are omitted by the API) |
| Fields requested | `sensor_index, name, latitude, longitude, last_seen, humidity, pm2.5_cf_1, pm2.5_cf_1_a, pm2.5_cf_1_b` |
| Timeout | 30 s |
| Retry | Up to 3 attempts with exponential backoff on 429, 5xx, and connection/timeout errors; any other 4xx fails the run immediately. When retries are exhausted the run fails with an error carrying the last status or exception |

The response is `{"fields": [...], "data": [[...], ...], "data_time_stamp": <epoch>, ...}` —
column names once, then one positional row per sensor. The client zips each row with `fields`
into a dict before anything else looks at it.

Field semantics (PurpleAir API v1):

- `pm2.5_cf_1` is the sensor-level value PurpleAir reports — the two-channel average when both
  channels are healthy by PurpleAir's own reckoning, otherwise the surviving channel.
- `pm2.5_cf_1_a` and `pm2.5_cf_1_b` are the individual channels.
- `last_seen` is epoch seconds UTC of the sensor's latest report.
- `humidity` is the sensor's onboard relative-humidity reading, in percent.

Indoor sensors are excluded at the query because they do not measure ambient air; this is scoping
the source, not a quality judgement on a reading.

## Payload Model

`PurpleAirSensorRecord` (Pydantic) is the boundary model for one zipped row:

| Field | Type | Validation |
|---|---|---|
| `sensor_index` | `int` | required |
| `name` | `str \| None` | |
| `latitude` | `float` | required, in [-90, 90] |
| `longitude` | `float` | required, in [-180, 180] |
| `last_seen` | `datetime` | required; epoch seconds → tz-aware UTC |
| `humidity` | `float \| None` | |
| `pm2_5_cf_1` | `float \| None` | alias `pm2.5_cf_1` |
| `pm2_5_cf_1_a` | `float \| None` | alias `pm2.5_cf_1_a` |
| `pm2_5_cf_1_b` | `float \| None` | alias `pm2.5_cf_1_b` |

A row that fails this model (missing `sensor_index`, missing or non-numeric coordinates,
unparseable `last_seen`) is a boundary rejection: it is counted in the run summary with its reason
and is not turned into an observation. Nulls in any PM or humidity field are *valid* at the
boundary — they become QC flags, not rejections.

## Quality Control

Checks run in the order listed; every applicable flag is raised (they are not mutually exclusive).

| Flag | Condition |
|---|---|
| `missing_value` | `pm2.5_cf_1` is null. |
| `channel_missing` | Exactly one of `pm2.5_cf_1_a`, `pm2.5_cf_1_b` is null. |
| `out_of_range` | Any present PM value is `< 0` or `> 1000` µg/m³ (the Plantower PMS5003 reporting ceiling), or humidity is outside [0, 100]. |
| `channel_disagreement` | Both channels present and both `\|A − B\| > 5` µg/m³ and `\|A − B\| / mean(A, B) > 0.61`, per the EPA channel-agreement criteria (Barkjohn et al. 2021). |

The agreement criteria were published for 24-hour averages; applied here to each snapshot reading
they are stricter, which is the conservative direction for a flag that is never used to drop data.
Both the absolute and relative conditions must hold, so two channels reading 1 and 3 µg/m³
(relative difference 100%, absolute 2) are not flagged, and 500 vs 506 (absolute 6, relative 1%)
are not flagged either. When `mean(A, B)` is 0 the relative condition is treated as not met rather
than evaluated, so two channels reading zero — a common clean-air reading — never raise the flag
or a division error.

### Corrected value

`pm25_corrected` is the EPA/Barkjohn 2021 nationwide correction:

```
pm25_corrected = 0.524 × mean(A, B) − 0.0862 × RH + 5.75
```

computed only when both channels and humidity are present and none of `channel_missing`,
`channel_disagreement`, `out_of_range` is raised; otherwise null. A negative result is clamped to
0. The input is the mean of the two `cf_1` channels, as in the published fit, not PurpleAir's
sensor-level field.

The correction is a fixed, published equation applied per reading; it is not the project's
distance-aware calibration against AirNow monitors, which is a later component consuming
`pm25_corrected` as its input.

## Mapping to Canonical Records

`Site`:

| Canonical | Source |
|---|---|
| `site_id` | `purpleair:{sensor_index}` |
| `source` | `purpleair` |
| `source_native_id` | `str(sensor_index)` |
| `site_type` | `low_cost_sensor` |
| `name` | `name` |
| `latitude`, `longitude` | as reported, unrounded |

`Observation`:

| Canonical | Source |
|---|---|
| `site_id`, `source`, `latitude`, `longitude` | as for `Site` |
| `observed_at` | `last_seen` |
| `pm25_raw` | `pm2.5_cf_1` (PurpleAir's sensor-level value, as reported) |
| `pm25_channel_a`, `pm25_channel_b` | `pm2.5_cf_1_a`, `pm2.5_cf_1_b` |
| `humidity` | `humidity` |
| `pm25_corrected` | as above |
| `qc_flags` | as above |
| `raw` | the zipped row dict, unmodified |

## Run

`ingest_purpleair(settings, archive_uri, conn=None) -> IngestSummary`:

1. Fetch one snapshot for the bounding box.
2. Zip rows; validate each into `PurpleAirSensorRecord`; collect boundary rejections.
3. Apply QC and correction; build `Site` and `Observation` records.
4. Write sites then observations to the store.
5. If a PostGIS connection was given, load the partitions just written (`load_partitions`).
6. Return the summary.

`IngestSummary` is the observation store's summary model, with `snapshot_at` set to the
response's `data_time_stamp` and the window fields unset.

A payload with zero rows is a successful run: the summary reports `fetched=0` and nothing is
written. The ingester does not deduplicate sensors within a snapshot; if the API ever returns the
same `sensor_index` twice, the store's within-batch duplicate check fails the run, which is the
right outcome for an upstream anomaly worth seeing.

A run is a snapshot, not a window: each sensor contributes at most one observation, keyed by its
`last_seen`. Two runs a minute apart in which a sensor has not reported again produce the same
`(site_id, observed_at)` and the store's merge makes the second a no-op for that sensor. History
accumulates by running repeatedly; the per-sensor `/sensors/:id/history` endpoint is not used.

Settings (`PurpleAirSettings`, Pydantic settings from the environment): `PURPLEAIR_API_KEY`
(required), `bbox` (the store's `DC_METRO` unless `AQDT_BBOX`, `nwlng,nwlat,selng,selat`,
overrides it), and the constants in the API contract table as overridable defaults. Constructing
the settings with the API key unset fails immediately with an error naming the variable.

## Package Layout

```
src/aqdt/purpleair/
  client.py     # fetch_sensors(settings) -> SensorSnapshot (zipped rows + data_time_stamp)
  models.py     # PurpleAirSensorRecord, PurpleAirSettings
  qc.py         # flags + correction
  ingest.py     # ingest_purpleair (entry point)
tests/purpleair/
tests/fixtures/purpleair/   # recorded snapshot payloads, including a channel-B fault
```

## Decisions & Alternatives

| Decision | Chosen | Alternatives Considered | Rationale |
|----------|--------|------------------------|-----------|
| Channel-agreement criteria | EPA/Barkjohn: `\|A−B\| > 5` µg/m³ **and** relative difference `> 0.61`, on each snapshot | Absolute-only threshold; relative-only; PurpleAir's own `confidence` / `channel_flags` fields | The published criteria are the sector standard and are what EPA's own PurpleAir products use; a lone absolute threshold over-flags at high concentrations and a lone relative one over-flags near zero. PurpleAir's flags are opaque and undocumented in their derivation. |
| Correction equation | Barkjohn 2021 linear form | EPA's 2022 extended (piecewise, for high smoke concentrations); no correction | The linear form is the published, widely-cited baseline and is exact for the concentration range D.C. sees on ordinary days. The extended form matters during wildfire-smoke episodes and is a deferred upgrade. Storing no corrected value would push a fixed, well-known step onto every downstream consumer. |
| Correction input | Mean of channels A and B | PurpleAir's sensor-level `pm2.5_cf_1`; channel A only | Matches the published fit, whose input was the A/B mean; the sensor-level field's fallback behaviour when one channel is degraded is PurpleAir's, not EPA's. |
| `pm25_raw` | PurpleAir's sensor-level `pm2.5_cf_1`, as reported | A/B mean computed here; channel A | Raw means what the source said; the channels are stored beside it, so any recomputation is possible downstream. |
| Correction when `pm2.5_cf_1` is null but both channels and humidity are present | Computed | Withhold (treat `missing_value` like the other flags) | The correction's inputs are the channels and humidity, not the sensor-level field, so its preconditions are met; the row is untrusted regardless because it carries `missing_value`. |
| Corrected value when flagged | Null | Compute regardless; compute from the surviving channel | A corrected number implies the input met the correction's preconditions; publishing one for a faulty reading invites misuse. The raw channels remain for anyone who wants to do otherwise. |
| Indoor sensors | Excluded at the query (`location_type=0`) | Ingest and flag | Indoor air is a different measurand, not a low-quality reading of ambient air; flag-never-drop governs readings of the thing being measured. |
| Snapshot vs. history endpoint | `/sensors` snapshot per run | `/sensors/:id/history` per sensor | One request per run versus one per sensor per run; snapshot polling matches the batch execution model and history depth is not a runtime prerequisite. |
| Retry policy | 3 attempts, exponential backoff, on 429/5xx/network errors | No retry; unbounded retry | Runs are idempotent so retrying is safe; bounded so a dead API fails the run loudly instead of hanging. |
| HTTP client | `httpx` (sync) | `requests`; `aiohttp` | Sync matches the batch model; `httpx` is the current standard with a clean mockable transport for tests. |

## Open Questions & Future Decisions

### Resolved

1. ✅ `max_age=86400`: a sensor silent for over a day is omitted by the API rather than fetched
   and re-written unchanged; staleness within a day is a query-time question for consumers.
2. ✅ Humidity from the sensor's onboard sensor is used as-is, as in the published fit.
3. ✅ An empty payload is a successful, visible no-op; a duplicated sensor within one payload fails
   the run at the store's duplicate check.

### Deferred

1. Adopt the EPA 2022 extended correction (piecewise, valid above ~250 µg/m³) if smoke episodes
   become a use case; the coefficients must be taken from the published source at that time.
2. Flatline detection for PurpleAir (a sensor reporting an unchanging value across many
   snapshots). The dual-channel check catches the observed fault mode; a stuck sensor with both
   channels stuck alike would not be caught.
3. Persisting boundary rejections (shared with the observation store's open question).
4. Verify the relative-difference threshold (0.61, "2 SD" in the source) against Barkjohn et al.
   2021 §2 before the constant is committed to code.
5. Sensor clock-skew detection (a `last_seen` ahead of the response's `data_time_stamp`). A flag
   computed against the response timestamp changes from poll to poll for the same
   `(site_id, last_seen)` and is removed by the store's incoming-wins merge, and storing the
   response timestamp on the row would make identical sensor rows non-identical across polls.
   No deterministic formulation has been found; a clock-ahead sensor lands in the partition its
   own `last_seen` names and is otherwise unaffected.

## References

- PurpleAir API v1, `GET /v1/sensors` — field list and bounding-box parameters.
- Barkjohn, K. K., Gantt, B., and Clements, A. L. (2021). *Development and application of a United
  States-wide correction for PM2.5 data collected with PurpleAir sensors.* Atmos. Meas. Tech., 14,
  4617–4637. Channel-agreement criteria and the correction equation.
- `docs/intent/observation-store/observation-store-design.md` — canonical records, flag
  vocabulary, `BoundingBox`, write semantics.
