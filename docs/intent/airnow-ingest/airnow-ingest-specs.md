---
design: airnow-ingest-design
prefix: AN
---

# AirNow Ingest — EARS Specs

Facets: `API` (HTTP client and request contract), `MODEL` (payload boundary model and rejections),
`SITE` (site-identifier normalization), `QC` (flags, including flatline), `MAP` (mapping to
canonical records), `RUN` (the `ingest_airnow` entry point and summary), `CFG` (settings).

## API Contract

- [ ] **AN-API-001**: When fetching a window, the AirNow ingester shall issue `GET https://www.airnowapi.org/aq/data/` with the query parameter `API_KEY` set to the configured AirNow key.
- [ ] **AN-API-002**: When fetching a window, the AirNow ingester shall pass the configured bounding box as `BBOX=minLon,minLat,maxLon,maxLat`, rendered from the store's `BoundingBox` as `selng,selat,nwlng,nwlat`.
- [ ] **AN-API-003**: When fetching the half-open window `[start, end)`, the AirNow ingester shall pass `startDate = start` and `endDate = end − 1 h`, each formatted as `YYYY-MM-DDTHH` in UTC, because the API treats both bounds as inclusive hours.
- [ ] **AN-API-004**: When fetching a window, the AirNow ingester shall pass `parameters=PM25`, `dataType=B`, `monitorType=0`, `verbose=1`, `includerawconcentrations=1`, and `format=application/json`.
- [ ] **AN-API-005**: The AirNow ingester shall apply a 30-second timeout to each `/aq/data/` request.
- [ ] **AN-API-006**: If an `/aq/data/` request returns HTTP 429 or any 5xx status, or fails with a connection or timeout error, then the AirNow ingester shall retry with exponential backoff up to a total of 3 attempts, and if all attempts fail, fail the run with an error that includes the last status or exception.
- [ ] **AN-API-007**: If an `/aq/data/` request returns any 4xx status other than 429, then the AirNow ingester shall fail the run immediately without retrying.
- [ ] **AN-API-008**: When the fetch window (after the flatline extension of AN-RUN-002) spans more than 24 hours, the AirNow ingester shall split it into consecutive half-open 24-hour requests `[t, t + 24 h)` (the last one shorter if needed) covering the whole window with no gap and no overlap, and concatenate their rows in window order.
- [ ] **AN-API-009**: When an `/aq/data/` response is received, the AirNow ingester shall parse it as a JSON array of row objects and pass each row object unchanged to boundary validation.

## Payload Model

- [ ] **AN-MODEL-001**: The AirNow ingester shall define a Pydantic `AirNowRow` with the aliased fields `utc: datetime` (`UTC`), `parameter: str` (`Parameter`), `latitude: float` (`Latitude`), `longitude: float` (`Longitude`), `value: float | None` (`Value`), `raw_concentration: float | None` (`RawConcentration`), `aqi: int | None` (`AQI`), `category: int | None` (`Category`), `unit: str` (`Unit`), `site_name: str | None` (`SiteName`), `agency_name: str | None` (`AgencyName`), `full_aqs_code: str | None` (`FullAQSCode`), `intl_aqs_code: str | None` (`IntlAQSCode`), and shall validate each response row against it.
- [ ] **AN-MODEL-002**: When validating a row, the AirNow ingester shall parse `UTC` (`YYYY-MM-DDTHH:MM`) as a timezone-aware UTC datetime.
- [ ] **AN-MODEL-003**: When validating a row, the AirNow ingester shall map a `Value`, `RawConcentration`, or `AQI` of `-999` (AirNow's missing-value sentinel) to `None`.
- [ ] **AN-MODEL-004**: When validating a row, the AirNow ingester shall coerce `FullAQSCode` and `IntlAQSCode` to strings with surrounding whitespace stripped, preserving any leading zeros present.
- [ ] **AN-MODEL-005**: If a response row has a missing or unparseable `UTC`, a missing or out-of-range `Latitude` or `Longitude`, a `Parameter` other than `PM2.5`, or a `Unit` other than `UG/M3`, then the AirNow ingester shall treat the row as a boundary rejection: count it in the run summary under a reason naming the failing field, and produce no `Site` or `Observation` from it.
- [ ] **AN-MODEL-006**: If, within the rows of one run, a second row arrives for the same normalized site identifier and the same `UTC` hour, then the AirNow ingester shall keep the first row, count each subsequent one as a boundary rejection under the reason `duplicate_site_hour`, and produce no observation from it.
- [ ] **AN-MODEL-007**: When a row has a null `Value` (after AN-MODEL-003), the AirNow ingester shall accept the row at the boundary; a null value is a QC condition, not a rejection.

## Site Identifier Normalization

- [ ] **AN-SITE-001**: The AirNow ingester shall expose `normalize_site_id(full_aqs_code, intl_aqs_code)` that returns the canonical 12-digit international AQS code (3-digit country + 2-digit state + 3-digit county + 4-digit site) or reports the site as unresolved.
- [ ] **AN-SITE-002**: When normalizing a single AQS code, the AirNow ingester shall map by digit count: 12 digits → as is; 11 digits → first 3 digits + `0` + remaining 8; 9 digits → `840` + code; 8 digits → `840` + `0` + code; any other length, or any non-digit character → unresolvable.
- [ ] **AN-SITE-003**: When both `FullAQSCode` and `IntlAQSCode` normalize successfully and agree, the AirNow ingester shall use that value as the normalized code.
- [ ] **AN-SITE-004**: When exactly one of `FullAQSCode`, `IntlAQSCode` is present or normalizes successfully, the AirNow ingester shall use that code's normalized value.
- [ ] **AN-SITE-005**: If both codes normalize successfully but to different values, or neither normalizes, then the AirNow ingester shall treat the site as unresolved.
- [ ] **AN-SITE-006**: When a site is resolved, the AirNow ingester shall set `site_id = "airnow:{12-digit normalized code}"`.
- [ ] **AN-SITE-007**: When a site is unresolved, the AirNow ingester shall set `site_id = "airnow:unresolved:{key}"` where `key` is `IntlAQSCode` as received if present, else `FullAQSCode` as received if present, else `SiteName`, and shall raise `site_id_unresolved` on every observation from that site.
- [ ] **AN-SITE-008**: When normalizing a 12-digit code, the AirNow ingester shall accept any 3-digit country prefix unchanged; the `840` prefix is added only when padding a 9- or 8-digit code.

## Quality Control

- [ ] **AN-QC-001**: When building an observation from a validated AirNow row, the AirNow ingester shall evaluate every QC check in AN-QC-002 through AN-QC-005 and raise every flag whose condition holds; the checks are not mutually exclusive.
- [ ] **AN-QC-002**: When an AirNow row's `Value` is null (AirNow sent `-999`), the AirNow ingester shall raise `missing_value` and shall not raise `out_of_range` for that value.
- [ ] **AN-QC-003**: When an AirNow row's `Value` is present and less than 0 or greater than 1000 µg/m³, the AirNow ingester shall raise `out_of_range`.
- [ ] **AN-QC-004**: When, for one site, `Value` is non-null and identical across at least `flatline_hours` (default 3) consecutive reported hours — consecutive meaning each hour's `UTC` is exactly one hour after the previous reported row for that site — the AirNow ingester shall raise `flatline` on every observation in that run of hours.
- [ ] **AN-QC-005**: When evaluating flatlines for a site, the AirNow ingester shall treat a null `Value`, a value differing from its predecessor, or a gap in the site's reported hours as ending the current run; a run shorter than `flatline_hours` shall raise no flag.
- [ ] **AN-QC-006**: When evaluating flatlines, the AirNow ingester shall evaluate over every hour fetched in the run (the requested window plus the extension of AN-RUN-002), so that a flatline that begins before the requested window is detected within it.
- [ ] **AN-QC-007**: The AirNow ingester shall never raise `channel_missing` or `channel_disagreement`; those flags belong to dual-channel sources.
- [ ] **AN-QC-008**: The AirNow ingester shall not apply any future-timestamp check to `UTC`; the request's `endDate` bounds what AirNow returns.

## Mapping to Canonical Records

- [ ] **AN-MAP-001**: When building a `Site` from a site's validated rows, the AirNow ingester shall set `site_id` per AN-SITE, `source = airnow`, `source_native_id` = `FullAQSCode` as received (falling back to `IntlAQSCode` as received), `site_type = reference_monitor`, `name` from `SiteName`, and `latitude`/`longitude` as reported without rounding.
- [ ] **AN-MAP-002**: When building an `Observation` from a validated AirNow row, the AirNow ingester shall set `site_id`, `source`, `latitude`, `longitude` as for the `Site`, `observed_at = UTC` (which labels the start of the averaging hour; no shift is applied), `pm25_raw` from `Value` (null when AirNow sent `-999`), `pm25_channel_a`, `pm25_channel_b`, `humidity`, and `pm25_corrected` all null, and `qc_flags` per AN-QC.
- [ ] **AN-MAP-003**: When building an `Observation`, the AirNow ingester shall set `raw` to the response row object exactly as received, with no keys added, removed, renamed, or values altered (so `AQI`, `Category`, `RawConcentration`, `AgencyName`, and both AQS codes remain available, and `-999` sentinels remain `-999` in `raw`).

## Run

- [ ] **AN-RUN-001**: The AirNow ingester shall expose `ingest_airnow(settings, archive_uri, start, end, conn=None) -> IngestSummary` as its entry point, which shall fetch the extended window, validate rows, normalize site identifiers, apply QC per site, build records, write sites then observations to the observation store, and — when `conn` is given — call the store's `load_partitions` with the partitions it wrote before returning the summary; without `conn` it shall touch the archive only.
- [ ] **AN-RUN-002**: When a run begins, the AirNow ingester shall extend the fetch window backwards by `flatline_hours − 1` hours, fetching the half-open window `[start − (flatline_hours − 1) h, end)`, so that flatline detection has the hours preceding the requested window.
- [ ] **AN-RUN-003**: When a run completes, the AirNow ingester shall write an `Observation` for every fetched hour, including the hours before `start` fetched for flatline lookback, so that the store's incoming-wins merge applies the flags and any upstream revisions the longer series now justifies.
- [ ] **AN-RUN-004**: When a run completes, the AirNow ingester shall call the store's `write_sites` before `write_observations`, so that no observation is archived for a site that is not.
- [ ] **AN-RUN-005**: The AirNow ingester shall require `start` and `end` to be timezone-aware UTC datetimes bounding the half-open window `[start, end)`, shall truncate each to the hour before use, and shall fail with a validation error if either is naive or if `end <= start`.
- [ ] **AN-RUN-007**: When a run completes, the AirNow ingester shall return an `IngestSummary` with `source = airnow`, `fetched` = total rows across all responses, `rejected` = boundary-rejection reason → count (including `duplicate_site_hour`), `written` = number of observations handed to the store, `flagged` = flag → number of observations carrying it, `partitions` = the URIs the store reported, `window_start`/`window_end` = the requested (unextended) half-open `start`/`end`, and `snapshot_at` unset; the summary shall satisfy `fetched == written + sum(rejected.values())` (OBS-SCHEMA-014).
- [ ] **AN-RUN-008**: When every response in a run contains zero rows, the AirNow ingester shall complete the run successfully, write nothing, and return a summary with `fetched = 0`, `written = 0`, and empty `partitions`.
- [ ] **AN-RUN-009**: Two runs over the same window against identical `/aq/data/` responses shall produce equal `Site` and `Observation` records and, through the store, byte-identical archive partitions.

## Settings

- [ ] **AN-CFG-001**: The AirNow ingester shall define `AirNowSettings` (Pydantic settings) that reads `AIRNOW_API_KEY` and `AQDT_BBOX` (`nwlng,nwlat,selng,selat`, parsed into the store's `BoundingBox`) from the environment, exposes `flatline_hours` (default 3) as an overridable setting, and exposes the endpoint URL, timeout, retry count, chunk length, and fixed query parameters as overridable defaults.
- [ ] **AN-CFG-002**: If `AIRNOW_API_KEY` or `AQDT_BBOX` is unset when `AirNowSettings` is constructed, then the AirNow ingester shall fail with an error naming the missing variable.
- [ ] **AN-CFG-003**: The AirNow ingester shall read no configuration from a file in the repository.
- [ ] **AN-CFG-004**: AirNow ingester tests shall exercise the client against recorded response fixtures under `tests/fixtures/airnow/` (at least: a clean response, a flatline, and malformed AQS codes) through a mocked HTTP transport, and shall make no network requests.
