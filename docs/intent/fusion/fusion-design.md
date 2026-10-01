---
parent: high-level-design
prefix: FUS
---

# Fusion

## Context and Design Philosophy

Calibration leaves the twin with PM2.5 on the reference monitors' scale at a hundred-odd scattered
PurpleAir sites. Mapping, exposure estimates, and transport all need a value *everywhere* in the
metro, and a statement of how far to trust it. Fusion turns each hour's calibrated sensor values
into a continuous surface on a fixed grid, with the kriging variance beside it as a direct,
per-cell measure of uncertainty.

This version is ordinary kriging: the surface is a spatially weighted interpolation of the
sensors, with weights set by a variogram fitted to that hour's data alone. Regression or
external-drift kriging — a covariate or the reference monitors as a drift term — is a later
refinement of the same component, and nothing here forecloses it.

Principles, in order of precedence when they conflict:

1. **Every hour is fitted on its own.** Spatial structure in PM2.5 changes with the weather; the
   variogram model, range, sill, and nugget are all refit per hour, and the fit is recorded with
   the surface it produced.
2. **Uncertainty is an output, not an add-on.** Every estimate cell has a variance cell, written
   in the same raster and at the same time.
3. **A function of the archive.** Given the archive as of a run, an hour's surface is fully
   determined; re-running the hour against an unchanged archive writes the same bytes. Fusion
   reads the archive only, never PostGIS.
4. **Bounded memory at any resolution.** Prediction is chunked, so a finer grid costs time, not
   memory; the grid resolution is chosen by what a scheduled run can finish, not by what fits in
   RAM.

## Inputs

All inputs come from the archive through the observation store's primitives.

- **Calibrated sensor hours** — the calibration segment's `calibrated_hourly` product, read with
  `read_partitioned`, rows with `fit_status` `fitted` or `pooled` and non-null `pm25_calibrated`.
  Both statuses are calibration's best estimate at the site; an `uncalibrated` row has no value
  to krige.
- **Sites** — `read_sites`, for each sensor's coordinates.

Reference monitors are not an input: they stay out of the surface so that evaluation can score
the surface at sites it never saw (HLD § Fusion is observational).

Each hour is kriged from its own rows only, including sensors whose site lies outside the grid's
extent (they still inform cells near the edge). A calibrated row whose `site_id` has no site
record has no coordinates and is not used. Sensors are projected to EPSG:26918 (UTM zone 18N,
metres — the projection calibration matches distances in), and sensors whose projected
coordinates coincide when rounded to the metre are merged into one point carrying the mean of
their values: co-located sensors are one location's estimate, and duplicate locations make the
kriging system singular. `n_sensors` counts the rows used; `n_points` counts the distinct
locations kriged.

## Grid

One grid, defined in EPSG:26918, shared by every hour at a given resolution:

- **Extent** — the envelope of the project bounding box (`DC_METRO`, or `AQDT_BBOX` when set —
  the same extent the ingesters query) projected to EPSG:26918, with the west and north edges
  snapped down/up to a multiple of the resolution, so grids at the same resolution align cell for
  cell across hours and runs.
- **Shape** — `width = ceil((x_max − x_min) / resolution_m)` columns, `height = ceil((y_max −
  y_min) / resolution_m)` rows; cell centres at `x_min + (i + ½)·res`, `y_max − (j + ½)·res`.
- **Resolution** — `resolution_m`, a setting, default 100 m (§ Decisions).

For `DC_METRO` the envelope is about 70.4 × 46.0 km: roughly 13 000 cells at 500 m, 81 000 at
200 m, 324 000 at 100 m, and 1.3 million at 50 m.

## Kriging

### Variogram

For each hour with at least `min_points` distinct locations, an experimental variogram is built
from the points (PyKrige defaults: 6 lag bins, unweighted least-squares fit) and each candidate
model — `spherical`, `exponential` — is fitted to it, giving that model's sill, range, and
nugget. PyKrige's cross-validation statistics (Kitanidis 1997) are computed for each candidate
from its leave-one-out residuals: `Q1` (mean standardized residual, ideally 0), `Q2` (mean
squared standardized residual, ideally 1), and `cR` (a measure of residual size). The candidate
with the smallest `cR` is the hour's variogram; a tie goes to `spherical`. A candidate whose fit or kriging system fails, or
whose `cR` is not finite, is not eligible.

`sill` is the total sill (partial sill + nugget), `range_m` the practical range in metres as
PyKrige parameterizes it, `nugget` the nugget, all in the units of the semivariance
((µg/m³)²) or metres. A fitted variogram with no spatial structure (partial sill 0) is a valid
fit: the surface is the hour's weighted mean everywhere, and the variance says so.

### Prediction

Ordinary kriging of the hour's points onto every grid cell centre, through PyKrige's vectorized
point backend, in chunks of at most `chunk_cells` cells (default 50 000). Each chunk's working set
is proportional to `chunk_cells × n_points`, independent of the grid's size; only the two output
arrays (float32, one value per cell) grow with the grid.

The estimate is clamped at 0 µg/m³ — ordinary kriging can extrapolate below zero, and a negative
concentration is not a value any consumer can use — matching calibration's clamp. The variance is
clamped at 0 (PyKrige can return tiny negative variances from floating-point error at data
points). Both are float32 in the raster.

### Hours that are not kriged

Every hour in a run's window gets exactly one `SurfaceFit` row, whatever happened to it:

| `status` | When | Raster |
|---|---|---|
| `kriged` | A candidate variogram was eligible and prediction succeeded | written |
| `insufficient_points` | Fewer than `min_points` distinct locations (including zero) | none |
| `fit_failed` | No candidate variogram was eligible (e.g. every value identical) | none |

A non-kriged hour's row carries `n_sensors` and `n_points` and nulls for everything describing a
variogram or a raster. If a previous run wrote a raster for an hour that is now not kriged (the
archive changed under it), the raster is deleted, so a raster exists for an hour exactly when its
row says `kriged`.

## Outputs

### Surface raster

One two-band Cloud-Optimized GeoTIFF per kriged hour:

- Band 1 `pm25` — kriged PM2.5, µg/m³. Band 2 `pm25_variance` — kriging variance, (µg/m³)².
- CRS EPSG:26918, the grid's geotransform, float32, NaN nodata, DEFLATE compression, 512-pixel
  internal tiles, internal overviews (the GDAL COG driver's defaults where they exist).
- Self-describing: band descriptions and units, and dataset tags `hour`, `variogram_model`,
  `sill`, `range_m`, `nugget`, `n_points`, so the file is interpretable in QGIS or rioxarray with
  no project code and no PostGIS.
- Deterministic: identical inputs produce identical bytes (no timestamp tags).

QGIS opens it directly from the archive (`/vsis3/<bucket>/<prefix>/fusion/surfaces/...`).

### `SurfaceFit`

One row per hour: `hour`, `status`, `n_sensors`, `n_points`, `variogram_model`, `sill`,
`range_m`, `nugget`, `q1`, `q2`, `cr`, `resolution_m`, `width`, `height`, `surface_uri`,
`surface_sha256`. `surface_uri` is the raster's full archive URI and `surface_sha256` the SHA-256
of its bytes, so a consumer can confirm the raster in the archive is the one the row describes.
Fields describing the variogram or the raster are null unless `status = kriged`.

## Storage

```
{archive_uri}/fusion/
  surface_fits/date={YYYY-MM-DD}/surface_fits.parquet
  surfaces/date={YYYY-MM-DD}/pm25_{YYYY-MM-DDTHH}.tif
```

`surface_fits` is an observation-store `Product` (prefix `fusion/surface_fits`, partition key
`date` = UTC date of `hour`, key `(hour)`, file `surface_fits.parquet`, table `surface_fits`)
registered in `aqdt.registry.PRODUCTS` after calibration's products, with a Pydantic record,
a Pandera frame model, the record-to-frame conformance test, and SQL under
`src/aqdt/fusion/sql/`. That registration is how PostGIS gets the table: `apply_schema` creates
it, `load_partitions` loads it after a run, `rebuild` reloads it. Fusion never writes to PostGIS
otherwise, and the rasters are never loaded into it.

### Writing a raster

Rasters are whole objects, not merged partitions, so they are not written through
`write_partitioned`; they go through the store's guarded-object primitive, which gives one object
the same guarantees a partition has — atomic replacement, and on S3 a conditional `PUT` against
the token read before the write (an advisory lock on a local archive).

For each hour, fusion takes the raster's current token **before** reading that hour's inputs,
kriges, and writes conditionally on the token. If another writer replaced the raster in between,
the write is refused, and fusion re-reads the hour's inputs, re-kriges, and tries again, up to the
store's attempt bound before failing the run. A successful write therefore always comes from
inputs read after every earlier competing write landed: concurrent runs over the same hour can
never leave an older surface over a newer one. Deleting a stale raster (§ Hours that are not
kriged) is guarded the same way.

The hour's `SurfaceFit` row is written through `write_partitioned` (incoming wins) immediately
after its raster is written or deleted, so a row never points at a raster that does not exist,
and a run that fails partway leaves every hour it finished complete. Two concurrent runs over the same hour can still leave the
row from one beside the raster from the other; `surface_sha256` makes that visible, and the next
run over the hour repairs it.

## Runs

`krige_surfaces(archive_uri, start, end, settings, conn=None) -> frame` — for every hour `H` in
the half-open window `[start, end)`, in order: read the hour's inputs, krige, write or delete the
raster, write the hour's `SurfaceFit` row. Given a PostGIS connection, the run ends by loading
the `surface_fits` partitions it wrote through the store's `load_partitions`. Returns the rows as a validated
frame. `start` and `end` must be tz-aware; each is converted to UTC and truncated to the hour, and
a naive value or `end ≤ start` fails validation.

A window with no calibrated rows at all is a successful run whose rows all say
`insufficient_points`. Runs are idempotent for a given archive state: same rows, byte-identical
rasters and partitions.

`aqdt fuse`, its routine window (the trailing three hours), its workflow, and its hourly cadence
belong to the pipeline segment; the schedule that dispatches it belongs to the infrastructure
segment.

Settings (`FusionSettings`, Pydantic settings from the environment): `AQDT_FUS_RESOLUTION_M`
(grid resolution in metres, default 100), `AQDT_FUS_MIN_POINTS` (default 10), and the shared `AQDT_BBOX`
(default `DC_METRO`). All optional; a non-positive resolution or `min_points` below 3 fails
validation naming the setting. `chunk_cells` is an argument of the prediction function, not
an environment setting.

## Benchmark

`python -m aqdt.fusion.benchmark` measures one hourly surface at each of a list of resolutions
(default 500, 200, 100, 50 m): wall-clock seconds for the whole hour (read, fit, predict, encode)
and for prediction alone, and peak resident memory. Each resolution runs in a fresh subprocess so
one resolution's peak cannot mask another's. It kriges a named archive hour, or, with no archive,
a synthetic field of a given point count, and prints a table together with the machine's CPU
model, core count, and memory. It writes nothing to the archive. It exists to choose
`resolution_m` and to re-check that choice when the machine or the sensor count changes.

## Package Layout

```
src/aqdt/fusion/
  schemas.py    # SurfaceFit, SurfaceStatus, FusionSettings
  frames.py     # SurfaceFitFrame (Pandera)
  products.py   # SURFACE_FITS
  grid.py       # Grid, grid_for(bbox, resolution_m)
  points.py     # hour_points: read, filter, project, merge co-located sensors
  krige.py      # select_variogram, predict_chunked
  raster.py     # encode_surface (COG bytes), surface paths
  run.py        # krige_surfaces
  benchmark.py  # resolution benchmark (python -m aqdt.fusion.benchmark)
  sql/          # surface_fits table
tests/fusion/
```

Dependencies: `pykrige`, `rasterio` (its wheels bundle GDAL with the COG driver).

## Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| Method | Ordinary kriging with PyKrige | The geostatistical standard for interpolating sparse point measurements with a per-cell uncertainty; PyKrige is pure Python, keeps the pipeline in one language, and runs on an Actions runner with no licence. |
| Variogram per hour | Model, sill, range, nugget refit every hour | PM2.5 spatial structure follows the boundary layer and wind, which change hour to hour; a fixed variogram would be wrong most hours. |
| Model selection | Smallest cross-validation `cR` among spherical and exponential | `cR` is computed from the leave-one-out residuals of the kriging itself, so it scores what the surface is used for rather than the curve fit to binned semivariances; spherical and exponential are the standard bounded models for urban PM2.5 and are numerically stable without a nugget. |
| Kriging coordinates | EPSG:26918 metres | Euclidean distance in a conformal projection over a 70 km domain is accurate to well under a cell, ranges come out in metres, and calibration already uses the same projection. |
| Grid CRS and alignment | EPSG:26918, edges snapped to multiples of the resolution | Resolutions are stated in metres; snapping makes every hour's cells coincide, so surfaces can be differenced or averaged without resampling. |
| Co-located sensors | Merged to one point (mean) at metre precision | Two sensors at one address measure one location; duplicates make the kriging matrix singular. |
| Minimum points | 10 distinct locations | Below that the experimental variogram has too few pairs per lag bin to fit three parameters; the hour is recorded as `insufficient_points` rather than kriged badly. |
| Chunked prediction | 50 000 cells per chunk | Bounds memory independently of resolution, so resolution is limited only by run time; 50 000 cells × ~100 points is tens of MB per array. |
| Grid resolution | 100 m by default | The sensors are about 4.7 km apart on average (roughly 140 locations over the 3 200 km² box), so 100 m already resolves far finer structure than the network carries; a finer grid renders more smoothly but adds no information. One hourly surface at 100 m (325 000 cells) takes about 3.5 s and under 600 MiB on a 2-vCPU development machine, so a scheduled run of three surfaces finishes in a small fraction of its workflow timeout. |
| Clamp estimate at 0 | Yes | A negative concentration is unusable downstream; calibration clamps the same way. |
| Uncertainty output | Kriging variance, in the same raster as the estimate | The variance is what ordinary kriging computes and is additive in later error budgets; one file per hour keeps estimate and uncertainty from drifting apart. |
| Surface format | Two-band Cloud-Optimized GeoTIFF per hour | A 2-D surface is a raster; a COG streams from S3 into QGIS and GDAL by HTTP range requests and opens in rioxarray, with no project code. Byte-for-byte determinism holds for a given GDAL build, which `uv.lock` pins through the `rasterio` wheel for every environment. |
| Raster write | Store's guarded-object primitive; token taken before inputs are read | Gives a whole-object write the same atomicity and one-writer guarantee as a partition, and guarantees no older surface ever replaces a newer one. |
| Fit metadata | `surface_fits` product, one row per hour, loaded into PostGIS | Which hours have surfaces and how they were fitted becomes a SQL query, while PostGIS holds no cells. |
| Every hour gets a row | Yes, including `insufficient_points` and `fit_failed` | "What is the surface for hour H?" always has an answer; a missing surface is a recorded fact, not an absence. |
| Raster deleted when an hour stops being kriged | Yes | A raster exists exactly when its row says `kriged`, so the archive never serves a surface its own metadata disowns. |
| Partitioning | By UTC date for both products | Twenty-four objects per day directory is easy to browse in QGIS and keeps the fits product to one small Parquet file per day. |

## Open Questions & Future Decisions

1. Skipping an hour whose inputs are unchanged since its surface was written (e.g. by recording an
   input digest on the row), if re-kriging the routine window becomes a meaningful cost.
2. Regression or external-drift kriging, with the reference monitors or a land-use covariate as
   the drift term; evaluation then moves to leave-one-out over the monitors.
3. Anisotropy, if the per-hour variograms show a persistent directional structure (e.g. along the
   prevailing wind or the I-95 corridor).
4. Weighting a sensor's point by `n_snapshots`, or its fit status (`pooled` vs `fitted`), through
   a per-point nugget, if `pooled` sensors prove noisier.
5. A domain mask for open water (Potomac, Anacostia, Chesapeake tributaries) once exposure
   estimates need population-weighted cells.

## References

- `docs/high-level-design.md` — Fusion; Fusion is observational; Gridded fields live outside
  PostGIS.
- `docs/intent/calibration/calibration-design.md` — `CalibratedHourly` semantics.
- `docs/intent/observation-store/observation-store-design.md` — `Product`, `write_partitioned`,
  one writer per partition, the guarded-object primitive.
- Kitanidis, P. K. (1997). *Introduction to Geostatistics: Applications in Hydrogeology.*
  Cambridge University Press — the `Q1`, `Q2`, `cR` cross-validation statistics.
- PyKrige documentation — `pykrige.ok.OrdinaryKriging`, variogram models and statistics.
- O'Regan et al. (2026) — the reference architecture's kriging step.
- Cloud Optimized GeoTIFF specification; GDAL COG driver.
