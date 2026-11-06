---
parent: high-level-design
prefix: DASH
---

# Dashboard

## Context and Design Philosophy

The twin's surfaces live in the archive as two-band GeoTIFFs in a projected CRS, which QGIS and
rioxarray read well but a phone browser cannot read at all. The dashboard is the twin's one public
view: a static site on GitHub Pages that shows the latest hourly PM2.5 surface on a basemap,
animates the hours before it, and puts the reference monitors' observations and the sensors'
calibrated values beside each hour. It is built by `aqdt dashboard build` and published by the
dashboard workflow a few minutes after each fusion run.

Principles:

1. **Stateless, rebuilt whole.** Every build regenerates the entire site from the archive. Nothing
   is carried over from the previous build or the previous deployment, so a build is a pure
   function of the archive state and its window, and any build can be repeated.
2. **The archive, and nothing else.** The build reads archive products through the store's
   primitives. It opens no PostGIS connection and calls no source API, so a rebuild costs no
   PurpleAir points and runs where the scheduled runs run.
3. **Time never skips.** Every hour in the window appears, in order. An hour without a surface is
   still a step on the timeline, labelled with why it has none, so a gap in the twin is visible as
   a gap rather than hidden by an animation that jumps over it.
4. **Maps' scales.** Surfaces are coloured by Maps' colormaps and fixed limits, so a frame on the
   dashboard and the same hour in an evaluation figure read the same.
5. **Precomputed, served static.** Everything the page shows is computed at build time and written
   as files: images and one JSON manifest. The page fetches and draws; it computes nothing about
   the data beyond choosing which frame to show.

## Build

`build_site(archive_uri, out, *, hours, end, generated_at, map_settings) -> BuildSummary` is
invoked by `aqdt dashboard build [--hours N] [--end T] --out DIR` (pipeline segment). `hours`
defaults to 48. `generated_at` is the build's timestamp, supplied by the caller (the CLI passes
the current time), so the function itself never reads the clock.

### Window

The window is `hours` consecutive hours, labelled by their start in UTC as every hourly product
is.

- **Without `end`**, the window ends at the *latest hour*: the latest hour whose `surface_fits`
  row has status `kriged`, searched over the `surface_fits` partitions for the seven days up to
  `generated_at` and ignoring rows for hours at or after `generated_at`. The window is the `hours`
  hours ending with it, inclusive. If no hour in those seven days is kriged, the build fails
  naming the search span. A failed build deploys nothing, so the site already published stays up,
  and its own freshness banner reports it stale.
- **With `end`** (an hour boundary, exclusive, as the pipeline's windows are), the window is
  `[end − hours, end)`, and the latest hour is the latest kriged hour within it. A window with no
  kriged hour at all fails the build in the same way.

Seven days bounds the read when fusion has stopped for a while, and a twin that has produced no
surface for a week has nothing current to show.

### Inputs

All read from the archive through the store's primitives, for the window's dates only:

- `surface_fits` (fusion), one row per hour that fusion has seen.
- The surface rasters those rows name, read with Maps' `read_surface`.
- AirNow observations (`read_observations`, source `airnow`) and the site records
  (`read_sites`).
- `calibrated_hourly` (calibration).

### Hour status

Every hour in the window gets exactly one status:

| `status` | When | Frames |
|---|---|---|
| `kriged` | Its fit row says `kriged` and the raster it names was read | written |
| `insufficient_points` | Its fit row says so | none |
| `fit_failed` | Its fit row says so | none |
| `missing` | No fit row exists for the hour | none |
| `surface_missing` | Its fit row says `kriged`, but the raster it names cannot be read | none |

`surface_missing` is logged as a warning naming the hour and the URI. It does not fail the build,
because one unreadable raster should cost one frame, not the whole page.

### Frames

For each `kriged` hour, two frames are rendered: the estimate (band 1, µg/m³) and the standard
deviation (the square root of band 2, clamped at 0 first, µg/m³).

1. **Reproject** each array from the grid's EPSG:26918 to EPSG:3857 (Web Mercator, the basemap's
   CRS) with `rasterio.warp.reproject`. Nearest-neighbour resampling keeps each cell's own value,
   as Maps' figures do. The destination transform and size come from
   `calculate_default_transform` for the grid's bounds, and cells outside the source grid are
   NaN. Every hour at one resolution shares one grid, so every frame of a build shares one
   destination transform and one set of bounds.
2. **Colour** each array through Maps' `colorize` for its kind (`pm25` or `sd`), giving RGBA
   pixels on the fixed scale: NaN fully transparent, values above the limit in the top colour.
3. **Encode** as PNG, with no text chunks or timestamps, so identical inputs produce identical
   bytes.

Frames are written to `data/frames/pm25_{YYYY-MM-DDTHH}.png` and
`data/frames/sd_{YYYY-MM-DDTHH}.png`, named for the hour's start.

**Bounds.** A frame's `bounds_latlon` is its EPSG:3857 extent with the south-west and north-east
corners converted to WGS84, written `[[south, west], [north, east]]`. Leaflet's image overlay
stretches an image linearly between two corners in Web Mercator, which is exactly the
projection the frame was rendered in, so the overlay sits where the cells are.

**Frame statistics.** For each `kriged` hour, the manifest carries `surface_min`,
`surface_mean`, and `surface_max` over band 1's finite cells, and `sd_max` over the
standard-deviation array's finite cells, all on the source grid before reprojection. The page uses
the maxima to tell whether a frame exceeds its scale's top.

### Manifest

`data/manifest.json` describes the whole site. It is validated by a Pydantic model, `Manifest`,
whose JSON Schema is the manifest's contract:

```
generated_at        ISO 8601 UTC, the build's timestamp
latest_hour         ISO 8601 UTC, the latest kriged hour
window_start        ISO 8601 UTC, the first hour
window_end          ISO 8601 UTC, exclusive
scales
  pm25 / sd         {max, cmap, units, label, stops}
hours[]             one per hour in the window, ascending and contiguous
  hour              ISO 8601 UTC (the hour's start)
  status            see Hour status
  pm25_png, sd_png  site-relative paths, null unless kriged
  bounds_latlon     [[S, W], [N, E]], null unless kriged
  surface_min, surface_mean, surface_max, sd_max   null unless kriged
  n_sensors         from the fit row, null for missing
  variogram_model   from the fit row, null unless kriged
  monitors[]        {site_id, name, lat, lon, obs, qc_flags}
  sensors[]         {sensor_id, lat, lon, pm25_calibrated, fit_status}
```

- `scales` comes from `MapSettings` and Maps' scale table. `max` is the fixed upper limit, `cmap`
  the colormap's name, `units` `µg/m³`, `label` the colorbar label, and `stops` eleven evenly
  spaced colours from 0 to `max` as `#rrggbb`, sampled by Maps' `colour_stops`. The page draws its
  legend from these, so it never names a colormap of its own.
- `monitors` are the AirNow observations with `observed_at` equal to the hour, joined to their site
  records for `name`, `lat`, and `lon`. `obs` is `pm25_raw` (null when the feed reported none), and
  `qc_flags` is the observation's flag list. This is the same hour matching evaluation uses.
- `sensors` are every `calibrated_hourly` row for the hour whose `site_id` has a site record, with
  `pm25_calibrated` null for an `uncalibrated` row. This includes the sensors fusion did not krige,
  so a reader can see that a sensor reported and why it has no value.
- Monitors and sensors are listed for every hour, kriged or not. An hour without a surface still
  has observations worth showing.
- Floats are rounded to two decimal places (coordinates to five). Lists are sorted by identifier,
  and the JSON is written with sorted keys. Two builds over the same archive state therefore
  differ only in `generated_at`.

### Site output

The site is assembled in a fresh temporary directory beside `out` and then moved into place, so a
build that fails partway leaves `out` as it was:

```
out/
  index.html  app.js  style.css      copied from src/aqdt/dashboard/web/
  data/
    manifest.json
    frames/{pm25,sd}_{YYYY-MM-DDTHH}.png
```

The build replaces `out` only when it is absent, empty, or holds a `data/manifest.json` (a
previous build). It refuses any other directory, naming it, so a mistyped `--out` cannot delete
unrelated work.

**Summary.** `BuildSummary` carries the window, the latest hour, the count of hours by status, the
number of frames, the site's total size in bytes, and the elapsed seconds; the CLI prints it as
the run's JSON line. A build over five minutes or a site over 50 MB is logged as a warning naming
the target. Both are well above what a 48-hour build at the default resolution needs, so crossing
either means the resolution or the window has outgrown the hourly schedule.

## Front End

Static HTML, CSS, and JavaScript in `src/aqdt/dashboard/web/`, with no build step and no
framework. Leaflet 1.9.4 loads from cdnjs, pinned by version and by Subresource Integrity hash.
The basemap is OpenStreetMap's standard tiles with the attribution their usage policy requires.

**Map.** On load the page fetches `data/manifest.json`, bypassing the browser cache: Pages
serves every file with a ten-minute cache lifetime, and the manifest is the one file that changes
under the same name each hour. Frames are named by hour and never change once kriged, so they cache
freely. The page then fits the map to the frames'
`bounds_latlon`, and shows the latest hour. The current frame is one image overlay at opacity 0.7
over the basemap.

**Estimate / Uncertainty.** A two-button toggle (`aria-pressed`) switches every frame between the
`pm25` and `sd` images. The legend is a horizontal gradient drawn from the active scale's `stops`,
labelled `0` and the scale's `max` with its units and label. When the current frame's maximum
(`surface_max` or `sd_max`) exceeds `max`, the legend's upper end shows an arrow and the text
"values above max shown in top colour".

**Animation.**
- A play/pause button and a range slider span every hour of the window, gaps included.
- The hour label shows the hour in UTC (`14:00–15:00 UTC`) and in Eastern time through `Intl`
  with `America/New_York`, so daylight saving is handled by the browser.
- All frames are preloaded before playback starts. Playback advances one hour per 500 ms and loops
  from the end back to the start.
- On a gap hour no overlay is drawn, and the label says "No surface: {reason}" in words for its
  status.

**Monitors and sensors** are drawn for the hour shown, with no fill colour, so nothing on the map
competes with the surface's colour scale.
- AirNow monitors are hollow triangles labelled with the observed value to one decimal, followed
  by `*` when the observation carries any QC flag. A null observation is labelled `n/a`.
- PurpleAir sensors are small hollow circles. Clicking or tapping one opens a popup with the
  sensor ID, the calibrated value (or "no calibrated value"), and the fit status.

**Freshness.** A line under the title reads `Latest hour HH:00 UTC, updated HH:MM UTC`, from
`latest_hour` and `generated_at`. An amber banner appears when the latest hour *ended* more than
two hours before the viewer's clock, that is when `now − (latest_hour + 1 h) > 2 h`. It reports
how old the latest hour is. The age counts from the hour's end because the hour just kriged began
nearly two hours before the build that publishes it. Measured from the end, a page on schedule
reads 1–2 h old, and the banner appears once a whole update has been missed.

**About.** A collapsible panel (`<details>`) states:
- the data sources: PurpleAir sensors, corrected with the EPA US-wide correction (Barkjohn et al.
  2021) and calibrated against AirNow monitors, then kriged hourly;
- what the uncertainty map shows: the kriging standard deviation, which is lowest near sensors and
  grows with distance from them;
- that AirNow data are preliminary and unvalidated;
- that the page is a research prototype and not for regulatory or health decisions;
- a link to the repository.

**Accessibility.**
- Every control is a native button or range input, operable from the keyboard with a visible
  focus ring, and labelled for assistive technology.
- The overlay image carries alt text naming the kind and the hour, updated with each frame.
- The layout is a single column at phone width (360 px), with the controls below the map and
  touch targets at least 44 px.
- Text and the banner meet WCAG AA contrast.

## Package Layout

```
src/aqdt/dashboard/
  __init__.py
  build.py       # build_site, BuildSummary, window and status resolution
  frames.py      # reprojection to EPSG:3857, PNG encoding, bounds
  manifest.py    # Manifest and its parts (Pydantic)
  web/
    index.html  app.js  style.css
tests/dashboard/
```

`build.py` imports Maps, which needs `matplotlib` from the `analysis` dependency group, so the CLI
imports the dashboard only when the command runs and fails naming the group when it is absent.
The front-end smoke test drives a real browser through Playwright (the `dev` group). It skips when
Playwright's Chromium is not installed, so CI, which installs none, skips it.

## Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| Site form | Pre-rendered PNG frames plus one JSON manifest, served static | One image per hour per kind is a few hundred kilobytes at the default resolution, so 48 hours fit comfortably in a Pages site. A static file set needs no server and matches the HLD's no-application-server rule. |
| Frame CRS | Reprojected to EPSG:3857 at build time | Leaflet stretches an image overlay linearly in Web Mercator. A frame already in that projection sits exactly on the basemap, and the page does no raster work. |
| Resampling | Nearest neighbour | Each pixel shows a cell's own value, as Maps' figures do. Interpolation would invent values between cells. |
| Colour scales | Maps' `colorize` and `colour_stops`, fixed limits from `MapSettings` | One definition of what a value looks like, shared with the evaluation figures. The legend is generated from the same colours, so it cannot drift from the frames. |
| Gap hours | Listed with a status and no frames | The timeline stays uniform in time, so the animation's speed is the clock's and a missing surface reads as missing. |
| Default window end | The latest kriged hour, searched back seven days | The page opens on a real surface even when the most recent hours were not kriged. The freshness line says how old that surface is. |
| Markers | Hollow triangles (monitors) and hollow circles (sensors), no fill | On a coloured surface over a coloured basemap, a filled marker reads as a value on the scale. Outlines locate a site without competing with the surface. |
| Monitor label | Observed value as text, `*` when flagged | The reference value is the number a reader compares the surface with, and the asterisk keeps a flagged hour visible without hiding the value (flag, never drop). |
| Leaflet delivery | cdnjs, version-pinned with an SRI hash | No build step and no vendored library to update, and the integrity hash makes the pinned file the only one the browser will run. |
| Basemap | OpenStreetMap standard tiles with attribution | Free, familiar, and openly licensed. The page's traffic is far inside the tile usage policy. |
| Freshness threshold | Banner when the latest hour ended more than 2 h ago | An on-schedule page is 1–2 h behind the clock by the hour's end, so the banner marks one missed update and no false alarm. |
| Output directory | Built in a temporary directory, then moved into place; only an empty or previously built directory is replaced | A failed build leaves the last good site intact, and a mistyped path cannot remove unrelated files. |
| Determinism | PNGs without metadata chunks; manifest rounded, sorted, and sort-keyed | Byte-identical output for an unchanged archive makes "stateless and idempotent" testable, and keeps a Pages deployment from changing when nothing upstream has. |
| Clock | `generated_at` passed in by the caller | Keeps the build a function of its arguments; the pipeline remains the one place the clock is read. |
| Dependency group | `analysis` (matplotlib, via Maps) | The colormaps are matplotlib's, and Maps is the one place they are defined. |

## Open Questions & Future Decisions

1. Showing evaluation scores (bias, coverage of the uncertainty) on the page, from a scheduled or
   published evaluation run.
2. An AQI-category view beside the continuous scale (shared with the maps segment's open question).
3. A longer history than 48 hours: a frame archive or coarser frames for older hours, once the site
   size budget allows.
4. Self-hosting tiles or a lighter basemap if traffic ever approaches the OpenStreetMap usage
   policy's limits.

## References

- `docs/high-level-design.md`: Dashboard; Environment; Non-Goals (no application server).
- `docs/intent/maps/maps-design.md`: scales, `colorize`, `colour_stops`, `read_surface`.
- `docs/intent/fusion/fusion-design.md`: `surface_fits`, the surface raster, the grid.
- `docs/intent/pipeline/pipeline-design.md`: `aqdt dashboard build`, the dashboard workflow and
  its cadence.
- `docs/intent/infrastructure/infrastructure-design.md`: the `aqdt-dashboard` schedule.
- Leaflet 1.9.4: `L.imageOverlay`, `L.circleMarker`, `L.divIcon`.
- OpenStreetMap Tile Usage Policy.
- Barkjohn, K. K., Gantt, B., and Clements, A. L. (2021). Atmospheric Measurement Techniques 14,
  4617–4637.
