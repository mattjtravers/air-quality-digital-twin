---
design: dashboard-design
prefix: DASH
---

# Dashboard — EARS Specs

Facets: `WIN` (the window and the latest hour), `IN` (what the build reads), `GAP` (hour status),
`FRAME` (reprojection, colouring, encoding), `MAN` (the manifest), `SITE` (the output directory
and the build summary), `WEB` (the front end), `TEST` (how this segment is tested).

## Window

- [x] **DASH-WIN-001**: The dashboard shall expose `build_site(archive_uri, out, *, hours=48, end=None, generated_at, map_settings=None) -> BuildSummary`, requiring a timezone-aware `generated_at` and a positive `hours`, and shall not read the clock.
- [x] **DASH-WIN-002**: When `build_site` is called without `end`, the dashboard shall take as the latest hour the latest hour whose `surface_fits` row has status `kriged`, among rows for hours before `generated_at` in the `surface_fits` partitions of the seven days up to `generated_at`. The window shall be the `hours` consecutive hours ending with the latest hour, inclusive.
- [x] **DASH-WIN-003**: When `build_site` is called with `end`, the dashboard shall convert `end` to UTC, truncate it to the hour, take the window `[end − hours, end)`, and take as the latest hour the latest hour in that window whose `surface_fits` row has status `kriged`.
- [x] **DASH-WIN-004**: If no hour in the search span (DASH-WIN-002) or the window (DASH-WIN-003) is kriged, then the dashboard shall raise an error naming the span searched, before writing anything to `out`.

## Inputs

- [x] **DASH-IN-001**: The dashboard build shall read `surface_fits`, the surface rasters those rows name, AirNow observations, site records, and `calibrated_hourly` from the archive through the observation store's primitives and Maps' `read_surface`, restricted to the window's dates. It shall open no PostGIS connection and issue no request to any source API.

## Hour Status

- [x] **DASH-GAP-001**: The dashboard shall give every hour in the window exactly one status: `kriged` when its fit row says `kriged` and its raster was read; `insufficient_points` or `fit_failed` when its fit row says so; `missing` when it has no fit row; and `surface_missing` when its fit row says `kriged` but its raster cannot be read.
- [x] **DASH-GAP-002**: When an hour's status is `surface_missing`, the dashboard shall log a warning naming the hour and the raster URI and continue the build.
- [x] **DASH-GAP-003**: When an hour's status is not `kriged`, the dashboard shall write no frames for it, and its manifest entry shall carry null `pm25_png`, `diff_png`, `sd_png`, `bounds_latlon`, `surface_min`, `surface_mean`, `surface_max`, `sd_max`, `variogram_model`, and `nugget_share`.

## Frames

- [x] **DASH-FRAME-001**: For each `kriged` hour, the dashboard shall reproject three arrays from EPSG:26918 to EPSG:3857 with `rasterio.warp.reproject` and nearest-neighbour resampling: band 1; band 1 minus the mean of band 1's finite cells (the unrounded `surface_mean`); and the square root of band 2 (each variance clamped at 0 first). The destination transform and size shall come from `calculate_default_transform` for the grid's bounds, and destination cells outside the source grid shall be NaN.
- [x] **DASH-FRAME-002**: The dashboard shall colour the reprojected estimate with Maps' `colorize(kind="pm25")`, the reprojected difference with `colorize(kind="diff")`, and the reprojected standard deviation with `colorize(kind="sd")`, using the build's `MapSettings`, and shall add no axes, colorbar, title, or marker to a frame.
- [x] **DASH-FRAME-003**: The dashboard shall encode each frame as an RGBA PNG carrying no text, time, or other metadata chunks, so that identical arrays produce identical bytes, and shall write it to `data/frames/{kind}_{YYYY-MM-DDTHH}.png` for `kind` in `pm25`, `diff`, `sd` and the hour's start in UTC.
- [x] **DASH-FRAME-004**: For each `kriged` hour, the dashboard shall set `bounds_latlon` to the frame's EPSG:3857 extent with its south-west and north-east corners converted to WGS84, written `[[south, west], [north, east]]`.
- [x] **DASH-FRAME-005**: For each `kriged` hour, the dashboard shall compute `surface_min`, `surface_mean`, and `surface_max` over band 1's finite cells, and `sd_max` over the standard-deviation array's finite cells, all on the source grid before reprojection.
- [x] **DASH-FRAME-006**: For each `kriged` hour, the dashboard shall set `nugget_share` to the hour's fit-row `nugget` divided by its `sill`, rounded to two decimal places, and to null when the sill is 0 or either value is missing.

## Manifest

- [x] **DASH-MAN-001**: The dashboard shall write `data/manifest.json` validated by the Pydantic model `Manifest`, carrying `generated_at`, `latest_hour`, `window_start`, `window_end` (exclusive), `scales`, and `hours`, with every timestamp an ISO 8601 UTC string.
- [x] **DASH-MAN-002**: The manifest's `hours` shall hold one entry per hour of the window, ascending and contiguous, each carrying `hour`, `status`, `pm25_png`, `diff_png`, `sd_png`, `bounds_latlon`, `surface_min`, `surface_mean`, `surface_max`, `sd_max`, `n_sensors`, `variogram_model`, `nugget_share`, `monitors`, and `sensors`. `n_sensors` and `variogram_model` shall come from the hour's fit row (`n_sensors` null when it has none), and the frame paths shall be relative to the site root.
- [x] **DASH-MAN-003**: The manifest's `scales` shall carry, for each of `pm25`, `diff`, and `sd`, the limits `min` and `max` and the colormap name `cmap` from Maps' `scale`, `units` `µg/m³`, the colorbar `label` Maps uses for the kind, and `stops`: eleven colours evenly spaced from `min` to `max`, as `#rrggbb` strings from Maps' `colour_stops`.
- [x] **DASH-MAN-004**: Each hour's `monitors` shall be the AirNow observations whose `observed_at` equals the hour, joined to their site records, each with `site_id`, `name`, `lat`, `lon`, `obs` (the observation's `pm25_raw`, null when absent), and `qc_flags` (its flag list). This holds for every hour, whatever its status.
- [x] **DASH-MAN-005**: Each hour's `sensors` shall be every `calibrated_hourly` row for the hour whose `site_id` has a site record, each with `sensor_id` (the `site_id`), `lat`, `lon`, `pm25_calibrated` (null for an `uncalibrated` row), and `fit_status`. This holds for every hour, whatever its status.
- [x] **DASH-MAN-006**: The dashboard shall round coordinates in the manifest to five decimal places and every other float to two, sort `monitors` by `site_id` and `sensors` by `sensor_id`, and write the JSON with sorted keys, so that two builds over the same archive state with the same arguments differ only in `generated_at`.

## Site Output

- [x] **DASH-SITE-001**: The dashboard shall assemble the site in a new temporary directory beside `out` and move it into place only after every file is written, so that a build that raises leaves `out` unchanged.
- [x] **DASH-SITE-002**: If `out` exists, is not empty, and holds no `data/manifest.json`, then the dashboard shall raise an error naming `out` without modifying it.
- [x] **DASH-SITE-003**: The dashboard shall copy every file under `src/aqdt/dashboard/web/` (`index.html`, `app.js`, `style.css`) to the root of the site.
- [x] **DASH-SITE-004**: `BuildSummary` shall carry `window_start`, `window_end`, `latest_hour`, the count of hours by status, the number of frames written, the site's total size in bytes, and the elapsed seconds.
- [x] **DASH-SITE-005**: When a build's elapsed time exceeds 300 seconds or its site exceeds 50 MB (50 × 10⁶ bytes), the dashboard shall log a warning naming the target exceeded and the measured value.

## Front End

- [x] **DASH-WEB-001**: The front end shall be static HTML, CSS, and JavaScript with no build step. It shall load Leaflet 1.9.4 from cdnjs with Subresource Integrity hashes on both the script and the stylesheet, and draw OpenStreetMap standard tiles with OpenStreetMap's attribution.
- [x] **DASH-WEB-002**: When the page loads, the front end shall fetch `data/manifest.json` bypassing the browser's HTTP cache, fit the map to the frames' `bounds_latlon`, and show the latest hour's frame as one image overlay at opacity 0.85.
- [x] **DASH-WEB-003**: The front end shall provide a toggle of three buttons carrying `aria-pressed`, labelled and ordered Estimate, Difference, Uncertainty, switching the overlay between each hour's `pm25_png`, `diff_png`, and `sd_png`.
- [x] **DASH-WEB-004**: The front end shall draw a legend for the active kind as a gradient through that scale's `stops`, labelled with its `min`, its `max`, its `units`, and its `label`. When the current hour's values pass the scale's `max` it shall show ▲ at the legend's upper end, and when they pass its `min` ▼ at the lower end, each with the text "values beyond the scale shown in its end colours". The current hour's values run from `surface_min` to `surface_max` (Estimate), from `surface_min − surface_mean` to `surface_max − surface_mean` (Difference), and up to `sd_max` (Uncertainty).
- [x] **DASH-WEB-005**: The front end shall provide a play/pause button and a range slider spanning every hour of the manifest, gap hours included. Playback shall advance one hour per 500 ms and loop to the first hour after the last.
- [x] **DASH-WEB-006**: The front end shall label the hour shown in Eastern time only, computed with `Intl.DateTimeFormat` in `America/New_York`, as the Eastern date of the hour's start followed by the hour's span on a 12-hour clock with the zone's abbreviation (`Fri, Nov 6 · 2:00 PM–3:00 PM EST`). When the start and end of the hour carry different zone abbreviations, each end shall carry its own (`1:00 AM EDT–1:00 AM EST`).
- [x] **DASH-WEB-007**: The front end shall preload the frames of every kriged hour before playback starts.
- [x] **DASH-WEB-008**: When the hour shown is not `kriged`, the front end shall draw no overlay and show "No surface:" followed by a plain-language reason for the hour's status.
- [x] **DASH-WEB-009**: The front end shall draw the shown hour's AirNow monitors as hollow triangles with no fill, labelled with `obs` to one decimal place followed by `*` when `qc_flags` is non-empty, or `n/a` when `obs` is null.
- [x] **DASH-WEB-010**: The front end shall draw the shown hour's PurpleAir sensors as small hollow circles with no fill. Activating one shall open a popup with its `sensor_id`, its calibrated value with units (or "no calibrated value"), and its `fit_status`.
- [x] **DASH-WEB-011**: The front end shall show `Latest hour h:mm AM|PM {zone}, updated h:mm AM|PM {zone}` from `latest_hour` and `generated_at` in Eastern time, formatted as DASH-WEB-006 formats the hour label's times.
- [x] **DASH-WEB-012**: When the viewer's clock is more than two hours past the end of the latest hour (`now − (latest_hour + 1 h) > 2 h`), the front end shall show an amber banner stating how many hours old the latest hour is.
- [x] **DASH-WEB-013**: The front end shall provide a collapsible About panel stating the data sources (PurpleAir sensors with the EPA US-wide correction of Barkjohn et al. 2021, calibrated against AirNow monitors, kriged hourly), what the difference map shows, what the uncertainty map shows, that AirNow data are preliminary, and that the page is not for regulatory or health decisions, with a link to the repository.
- [x] **DASH-WEB-014**: Every control shall be a native button or range input operable from the keyboard with a visible focus indicator and an accessible name. The overlay image shall carry alt text naming the kind and hour shown, updated with each frame. At a viewport 360 px wide the page shall lay out in one column with no horizontal scrolling.
- [x] **DASH-WEB-015**: When the hour shown is `kriged`, the front end shall caption it `Kriged from N sensors · {variogram_model} variogram · range {surface_min}–{surface_max} µg/m³ · nugget {P}% of sill`, with the range to one decimal place and `P` the `nugget_share` as a whole percentage, omitting the nugget part when `nugget_share` is null.
- [x] **DASH-WEB-016**: Whenever the hour shown or the map's zoom changes, the front end shall place the shown hour's AirNow monitor labels one at a time in the order of the hour's `monitors`, each on the first side of its triangle — right, left, below, above — where the label's box overlaps no label already placed and no other monitor's triangle, or on the right when no side is clear. PurpleAir sensor markers and the map's edges shall not constrain the placement.

## Testing

- [x] **DASH-TEST-001**: Dashboard tests shall build sites from a fixture archive in a temporary directory, containing at least a kriged hour, a non-kriged hour, and an hour with no fit row. They shall require no network, no AWS credentials, and no database, apart from DASH-TEST-002.
- [x] **DASH-TEST-002**: A smoke test shall build the site from the fixture archive, serve it over local HTTP, and load `index.html` in Playwright's Chromium. It shall assert that the frames load, that moving the slider changes the hour label and the overlay image, and that the toggle switches the overlay to the `sd` frame. It shall answer OpenStreetMap tile requests locally with a blank tile, and shall skip when Playwright or its Chromium is not installed.
- [x] **DASH-TEST-003**: The devcontainer's post-create script shall install Playwright's Chromium together with its system libraries (`playwright install --with-deps chromium`), after `uv sync --all-groups`, so that DASH-TEST-002 runs in a fresh Codespace. If the install fails, the script shall report it and continue, without failing the Codespace's creation.
