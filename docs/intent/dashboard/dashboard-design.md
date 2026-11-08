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
  and its freshness line still names the hour it shows.
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
- `calibrated_hourly` (calibration), from which the hour's kriging points are also rebuilt with
  fusion's `hour_points`, as evaluation rebuilds them.

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

For each `kriged` hour, three frames are rendered:

- the **estimate** (band 1, µg/m³);
- the **difference from the hour's average**: band 1 minus the mean of band 1's finite cells, the
  same mean as `surface_mean` before rounding (µg/m³);
- the **standard deviation**: the square root of band 2, clamped at 0 first (µg/m³).

A clean hour can span only a couple of µg/m³ across the metro, which on the estimate's fixed
0–`pm25_max` scale is a few neighbouring colours and reads as flat. The difference view keeps
that hour's spatial pattern visible on a scale centred on zero, while the estimate keeps every
hour comparable in absolute terms.

1. **Reproject** each array from the grid's EPSG:26918 to EPSG:3857 (Web Mercator, the basemap's
   CRS) with `rasterio.warp.reproject`. Nearest-neighbour resampling keeps each cell's own value,
   as Maps' figures do. The destination transform and size come from
   `calculate_default_transform` for the grid's bounds, and cells outside the source grid are
   NaN. Every hour at one resolution shares one grid, so every frame of a build shares one
   destination transform and one set of bounds.
2. **Colour** each array through Maps' `colorize` for its kind (`pm25`, `diff`, or `sd`), giving
   RGB pixels on the fixed scale, values beyond a limit in that end's colour.
3. **Fade** where the estimate is least supported, by setting each pixel's alpha from the hour's
   *support* array (below). NaN cells stay fully transparent.
4. **Encode** as PNG, with no text chunks or timestamps, so identical inputs produce identical
   bytes.

Frames are written to `data/frames/{pm25,diff,sd}_{YYYY-MM-DDTHH}.png`, named for the kind and
the hour's start.

**Support.** A kriged surface is least trustworthy where its standard deviation is high and
where no sensor is close enough for the variogram to carry information there. Each kriged hour
gets one alpha array on the source grid, shared by its three frames, built from two rules that
both apply:

- *Uncertainty.* Alpha falls linearly with the kriging standard deviation as a fraction of the
  hour's `√sill`: `α = 0.85 − 0.25 · min(sd / √sill, 1)`. The sill is the variance of a value
  known only to be one of the hour's, so `sd / √sill` near 1 says the estimate there is no better
  than the hour's mean, and near 0 that it is close to known. A cell at `√sill` or above takes
  0.60; the rule never fades further, because on most hours most of the grid lies near `√sill`
  and the Estimate view must stay readable on every hour. When the sill is 0 or missing, the rule
  leaves 0.85.
- *Distance.* A cell whose centre is farther from the nearest of the hour's kriging points than
  `max(range_m, 5 km)` takes 0.40. Beyond the variogram's range the sensors carry no information
  about the cell, and the estimate there is the kriged mean. The 5 km minimum keeps a degenerate
  fit, whose range is a few hundred metres, from fading the whole map; such an hour is flagged
  instead (§ Front End, *Pattern note*). When the hour's kriging points cannot be rebuilt (none
  remain in the archive), only the uncertainty rule applies.

The alpha array is reprojected with the frames (nearest neighbour) and written into each finite
pixel as `round(255 · α)`. The fade is baked into the PNGs, so the page draws the overlay at full
opacity and does no raster work. Measured against the hour's own sill, a small spread of standard
deviation across the grid gives a small spread of fade, and an hour whose variogram is mostly
nugget, whose map is mostly the hour's mean, reads faded throughout.

**Bounds.** A frame's `bounds_latlon` is its EPSG:3857 extent with the south-west and north-east
corners converted to WGS84, written `[[south, west], [north, east]]`. Leaflet's image overlay
stretches an image linearly between two corners in Web Mercator, which is exactly the
projection the frame was rendered in, so the overlay sits where the cells are.

**Frame statistics.** For each `kriged` hour, the manifest carries `surface_min`,
`surface_mean`, and `surface_max` over band 1's finite cells, and `sd_max` over the
standard-deviation array's finite cells, all on the source grid before reprojection. The page uses
them to tell whether a frame passes its scale's limits; for the difference view the frame's
extremes are `surface_min − surface_mean` and `surface_max − surface_mean`.

**Variogram.** For each `kriged` hour the manifest carries the fitted variogram's `range_km`
(the fit row's `range_m` in kilometres), `nugget`, and `sill`, and `nugget_share`, the nugget
divided by the sill (null when the sill is 0). The range is the distance beyond which two
locations' values are uncorrelated. The nugget share is the fraction of the hour's variance the
variogram attributes to noise at a point rather than to spatial structure: an hour near 1 is
nearly pure noise, and kriging returns most of the grid to the hour's mean.

**Monitor check.** Each monitor of a `kriged` hour carries `map_value`, band 1 of the raster cell
containing the monitor (the cell rule evaluation's matchups use: the monitor's EPSG:26918
coordinates through the inverted geotransform), null when the monitor lies outside the grid or
its cell is NaN. Its `difference` is `map_value − obs`, null when the observation is flagged or
either value is null. The hour carries `monitor_n`, the number of its monitors with a
`difference`, and `monitor_mean_difference`, their mean (null when `monitor_n` is 0), computed
before rounding. Monitors are not kriged, so these are an independent check on the hour's
surface, read with evaluation's caveat that a cell is a 100 m average and a monitor a point.

### Manifest

`data/manifest.json` describes the whole site. It is validated by a Pydantic model, `Manifest`,
whose JSON Schema is the manifest's contract:

```
generated_at        ISO 8601 UTC, the build's timestamp
latest_hour         ISO 8601 UTC, the latest kriged hour
window_start        ISO 8601 UTC, the first hour
window_end          ISO 8601 UTC, exclusive
scales
  pm25 / diff / sd  {min, max, cmap, units, label, stops}
hours[]             one per hour in the window, ascending and contiguous
  hour              ISO 8601 UTC (the hour's start)
  status            see Hour status
  pm25_png, diff_png, sd_png   site-relative paths, null unless kriged
  bounds_latlon     [[S, W], [N, E]], null unless kriged
  surface_min, surface_mean, surface_max, sd_max   null unless kriged
  n_sensors         from the fit row, null for missing
  variogram_model   from the fit row, null unless kriged
  range_km, nugget, sill   from the fit row, null unless kriged
  nugget_share      nugget / sill from the fit row, null unless kriged or when the sill is 0
  monitor_n         monitors with a difference; 0 unless kriged
  monitor_mean_difference  mean difference over them, null when monitor_n is 0
  monitors[]        {site_id, name, lat, lon, obs, qc_flags, map_value, difference}
  sensors[]         {sensor_id, lat, lon, pm25_calibrated, fit_status}
```

- `scales` comes from `MapSettings` and Maps' scale table. `min` and `max` are the fixed limits
  (`min` is 0 for `pm25` and `sd`, `−diff_max` for `diff`), `cmap` the colormap's name, `units`
  `µg/m³`, `label` the colorbar label, and `stops` eleven evenly spaced colours from `min` to
  `max` as `#rrggbb`, sampled by Maps' `colour_stops`. The page draws its
  legend from these, so it never names a colormap of its own.
- `monitors` are the AirNow observations with `observed_at` equal to the hour, joined to their site
  records for `name`, `lat`, and `lon`. `obs` is `pm25_raw` (null when the feed reported none), and
  `qc_flags` is the observation's flag list. This is the same hour matching evaluation uses.
  `map_value` and `difference` are as § Monitor check gives them, null unless the hour is kriged.
- `sensors` are every `calibrated_hourly` row for the hour whose `site_id` has a site record, with
  `pm25_calibrated` null for an `uncalibrated` row. This includes the sensors fusion did not krige,
  so a reader can see that a sensor reported and why it has no value.
- Monitors and sensors are listed for every hour, kriged or not. An hour without a surface still
  has observations worth showing.
- Floats are rounded to two decimal places (coordinates to five, `nugget` and `sill` to four). Lists are sorted by identifier,
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
    frames/{pm25,diff,sd}_{YYYY-MM-DDTHH}.png
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
Above the surface, Esri's World Boundaries and Places reference tiles draw place names and
county and state lines, with Esri's attribution beside OpenStreetMap's.

**Map.** On load the page fetches `data/manifest.json`, bypassing the browser cache: Pages
serves every file with a ten-minute cache lifetime, and the manifest is the one file that changes
under the same name each hour. Frames are named by hour and never change once kriged, so they cache
freely. The page then fits the map to the frames'
`bounds_latlon`, and shows the latest hour. The current frame is one image overlay at full
opacity: each pixel already carries its alpha (§ Support), 0.85 where the estimate is best
supported, opaque enough that neighbouring colours on the scale stay distinguishable with the
basemap's roads and rivers still showing through, and fading to 0.40 where it is least supported.
The names layer sits in its own pane above
the overlay and below the monitor and sensor markers. Its names carry a light halo, so they read
at full contrast on every colour of every scale, and the pane takes no pointer events, so a click
or tap reaches the marker or map beneath it.

**Estimate | Difference | Uncertainty.** A three-button toggle (`aria-pressed`), in that order,
switches every frame between the `pm25`, `diff`, and `sd` images. The legend is a horizontal
gradient drawn from the active scale's `stops`, labelled with the scale's `min` and `max`, its
units, and its label; the Difference view's label is "Above / below this hour's average (µg/m³)".
When the current frame passes a limit, the legend shows an arrow at that end (▲ at the top, ▼ at
the bottom) and the text "values beyond the scale shown in its end colours". On a kriged hour
the legend also says "Faded areas: far from sensors / high uncertainty".

**Pattern note.** On a kriged hour whose variogram range is under 1 km (`range_km < 1`), a small
note on the map reads "Spatial pattern not detectable this hour; map ≈ hour average." Such a range
is shorter than the spacing of almost any two sensors, so the fit found no spatial structure and
the surface is the hour's mean everywhere but at the sensors themselves.

**Hour caption.** Under the hour label, a kriged hour is described as
`Kriged from N sensors · {model} variogram · values {min}–{max} µg/m³ · variogram range {R} km ·
nugget {P}% of sill`, from `n_sensors`, `variogram_model`, `surface_min`, `surface_max`,
`range_km` (one decimal), and `nugget_share` (the nugget part is omitted when `nugget_share` is
null). "Values" names the spread of the estimate and "variogram range" the distance, so neither
reads as the other. A flat-looking hour explains itself: its values span little, its nugget share
is high, or both.

Under the caption, a kriged hour with `monitor_n` at least 1 reads
`Monitors: mean difference {D} µg/m³, n = {n}`, from `monitor_mean_difference` (one decimal,
signed) and `monitor_n`; the difference is map minus observed, so a negative value means the map
reads low at the monitors.

**Animation.**
- A play/pause button and a range slider span every hour of the window, gaps included.
- The hour label shows the hour's date and span in Eastern time on a 12-hour clock, with the
  zone's abbreviation: `Fri, Nov 6 · 2:00 PM–3:00 PM EST`. The date is the Eastern date of the
  hour's start. When the start and end fall in different zones (the hours daylight saving begins
  and ends), each end carries its own abbreviation: `1:00 AM EDT–1:00 AM EST`.
- All frames are preloaded before playback starts. Playback advances one hour per 500 ms and loops
  from the end back to the start.
- On a gap hour no overlay is drawn, and the label says "No surface: {reason}" in words for its
  status.

**Monitors and sensors** are drawn for the hour shown, with no fill colour, so nothing on the map
competes with the surface's colour scale.
- AirNow monitors are hollow triangles labelled with the observed value to one decimal, followed
  by `*` when the observation carries any QC flag (`n/a` for a null observation). When the
  monitor has a `map_value`, the label adds ` · map {value}`, and when it has a `difference`,
  ` ({difference})` signed, both to one decimal: `7.0 · map 4.5 (−2.5)`. A flagged hour reads
  `7.0* · map 4.5`, with no difference.
- Every monitor's label stays readable wherever monitors sit close together. A label sits to the
  right of its triangle unless that would overlap a label already placed or another monitor's
  triangle; it then takes the first clear side of left, below, and above, and stays on the right
  when no side is clear. Labels are placed one monitor at a time in manifest order, and placed
  afresh whenever the hour shown or the zoom changes, since both change what is close on screen
  and how wide each label is.
- PurpleAir sensors are small hollow circles, drawn with a fully transparent fill so that the
  whole disc, not only its outline, takes a click or tap. Clicking or tapping one opens a popup
  with the sensor ID, the calibrated value (or "no calibrated value"), and the fit status.

**Freshness.** A line under the title reads `Latest hour 2:00 PM EST, updated 3:55 PM EST`,
from `latest_hour` and `generated_at` in Eastern time, formatted as the hour label's times are. The page raises no alert about its age: an open page
does not reload itself, so an old latest hour usually means the page wants refreshing rather than
that the twin has stopped. The freshness line gives the reader the hours to judge by.

**About.** A collapsible panel (`<details>`) states:
- the data sources: PurpleAir sensors, corrected with the EPA US-wide correction (Barkjohn et al.
  2021) and calibrated against AirNow monitors, then kriged hourly;
- what the difference map shows: where the estimate is above (red) or below (blue) that hour's
  own average across the metro;
- what the uncertainty map shows: the kriging standard deviation, which is lowest near sensors and
  grows with distance from them;
- what the nugget share means: the part of sensor-to-sensor variation the variogram treats as
  short-scale noise or measurement error (typical for low-cost sensors), so a high share means the
  map smooths toward the hour's average;
- that faded areas are far from sensors or where the uncertainty is highest;
- what the monitor labels show: observed, map value, and map minus observed;
- that AirNow data are preliminary and unvalidated;
- that the page is a research prototype and not for regulatory or health decisions;
- a link to the repository.

**Accessibility.**
- Every control is a native button or range input, operable from the keyboard with a visible
  focus ring, and labelled for assistive technology.
- The overlay image carries alt text naming the kind and the hour, updated with each frame.
- The layout is a single column at phone width (360 px), with the controls below the map and
  touch targets at least 44 px.
- Text meets WCAG AA contrast.

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
Playwright's Chromium is not installed, so CI, which installs none, skips it. The devcontainer's
post-create script installs Chromium and the system libraries it needs, so the smoke test runs
in every fresh Codespace. A failed install there is reported and does not fail the Codespace's
creation, because only this one test depends on it.

## Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| Site form | Pre-rendered PNG frames plus one JSON manifest, served static | One image per hour per kind is a few hundred kilobytes at the default resolution, so 48 hours fit comfortably in a Pages site. A static file set needs no server and matches the HLD's no-application-server rule. |
| Frame CRS | Reprojected to EPSG:3857 at build time | Leaflet stretches an image overlay linearly in Web Mercator. A frame already in that projection sits exactly on the basemap, and the page does no raster work. |
| Resampling | Nearest neighbour | Each pixel shows a cell's own value, as Maps' figures do. Interpolation would invent values between cells. |
| Difference view | Estimate minus the hour's mean on a fixed symmetric diverging scale, beside the absolute estimate | Clean hours span a couple of µg/m³, which reads as one colour on an absolute scale that must also hold smoky hours. Subtracting the hour's own mean shows its spatial pattern without stretching the absolute scale per hour, and fixed symmetric limits keep difference maps comparable across hours. |
| Overlay opacity | Per pixel, baked into the frames: 0.85 where best supported, falling to 0.60 as the standard deviation approaches the hour's `√sill`, and 0.40 beyond the variogram range (at least 5 km) from the nearest sensor | Neighbouring colours on a fixed scale differ little, and a basemap seen through a thin overlay washes them together; 0.85 keeps them apart where the estimate deserves reading. Fading the rest tells the reader where not to read it, in the same image, on every view. Baking it at build time keeps the page free of raster work. |
| Support rules | Standard deviation as a fraction of `√sill`, stopping at 0.60, plus `max(range, 5 km)` as a distance cut to 0.40 | The standard deviation's absolute size changes with each hour's sill, so dividing by `√sill` makes one rule mean the same on every hour, and a narrow spread across the grid produces little fade. Most cells sit near `√sill` on most hours, so the uncertainty rule stops at 0.60 to keep the Estimate view readable on every hour. Cells beyond the range fade further, to 0.40, since there the variogram gives the sensors no weight beyond the mean; the 5 km minimum stops a degenerate sub-kilometre fit from fading the whole map, and the pattern note says what such an hour is. |
| Monitor check | Map value at the monitor's cell, map minus observed, and the hour's mean difference over unflagged monitors | The monitors are not kriged, so their residuals are the one independent check on each hour a reader can see. The cell rule is evaluation's, so the page and the evaluation report agree on every residual. |
| Colour scales | Maps' `colorize` and `colour_stops`, fixed limits from `MapSettings` | One definition of what a value looks like, shared with the evaluation figures. The legend is generated from the same colours, so it cannot drift from the frames. |
| Gap hours | Listed with a status and no frames | The timeline stays uniform in time, so the animation's speed is the clock's and a missing surface reads as missing. |
| Default window end | The latest kriged hour, searched back seven days | The page opens on a real surface even when the most recent hours were not kriged. The freshness line says how old that surface is. |
| Markers | Hollow triangles (monitors) and hollow circles (sensors), no fill | On a coloured surface over a coloured basemap, a filled marker reads as a value on the scale. Outlines locate a site without competing with the surface. |
| Monitor label | Observed value, map value, and their difference as text, `*` and no difference when flagged | The reference value is the number a reader compares the surface with, and the map value beside it makes the comparison without reading colours. The asterisk keeps a flagged hour visible without hiding the value (flag, never drop), and a flagged hour gives no difference to trust. |
| Displayed time | Eastern time only, through `Intl` with `America/New_York`, 12-hour clock with the zone's abbreviation | The twin covers the D.C. metro, whose readers keep Eastern time; the archive and manifest stay in UTC, and the page converts only for display. `America/New_York` lets the browser handle daylight saving, and the abbreviation tells apart the two 1 AM hours of the autumn change. |
| Label placement | In the page, greedy over four sides of the triangle, redone on each hour and zoom | Whether two labels collide depends on the zoom and on each label's text, which only the page knows. Four fixed sides keep each label beside its own triangle, so no leader line is needed to tell which value belongs to which monitor; manifest order makes the placement the same on every load. |
| Leaflet delivery | cdnjs, version-pinned with an SRI hash | No build step and no vendored library to update, and the integrity hash makes the pinned file the only one the browser will run. |
| Basemap | OpenStreetMap standard tiles with attribution | Free, familiar, and openly licensed. The page's traffic is far inside the tile usage policy. |
| Map names | Esri World Boundaries and Places tiles in a pane above the surface | Seen through the overlay, the basemap's own names are faint, and a thinner overlay to show them would let the basemap tint the surface and shrink the colour step between neighbouring values. Names drawn above the surface orient the reader without touching its colours. Esri's reference layer is transparent apart from its names and boundaries, and is served without an API key. |
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
- Esri World Boundaries and Places reference layer: attribution and terms of use.
- Barkjohn, K. K., Gantt, B., and Clements, A. L. (2021). Atmospheric Measurement Techniques 14,
  4617–4637.
