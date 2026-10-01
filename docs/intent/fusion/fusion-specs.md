---
design: fusion-design
prefix: FUS
---

# Fusion — EARS Specs

Facets: `IN` (inputs read from the archive), `PTS` (kriging points), `GRID` (the prediction grid),
`VAR` (per-hour variogram fit and selection), `PRED` (chunked prediction), `STATUS` (hours that
are and are not kriged), `RASTER` (the hourly surface raster), `STORE` (the `surface_fits`
product and its PostGIS table), `RUN` (entry point), `CFG` (settings and tests), `BENCH` (the
resolution benchmark).

## Inputs

- [x] **FUS-IN-001**: When reading inputs for an hour, fusion shall read calibrated sensor hours from the archive through the observation store's `read_partitioned` with calibration's `calibrated_hourly` product and site coordinates through `read_sites`, and shall not read from PostGIS.
- [x] **FUS-IN-002**: When selecting the rows to krige for hour `H`, fusion shall use only `calibrated_hourly` rows with `hour == H`, `fit_status` `fitted` or `pooled`, and non-null `pm25_calibrated`.
- [x] **FUS-IN-003**: Fusion shall not use AirNow observations or `reference_monitor` sites as kriging points.
- [x] **FUS-IN-004**: If a selected `calibrated_hourly` row's `site_id` has no site record, then fusion shall exclude the row from the hour's points and from `n_sensors`.

## Kriging Points

- [x] **FUS-PTS-001**: When building an hour's points, fusion shall project each sensor's site coordinates from EPSG:4326 to EPSG:26918.
- [x] **FUS-PTS-002**: When two or more of an hour's sensors have projected coordinates that are equal after rounding each coordinate to the nearest metre, fusion shall merge them into one point at the rounded coordinates whose value is the arithmetic mean of their `pm25_calibrated`.
- [x] **FUS-PTS-003**: When building an hour's points, fusion shall set `n_sensors` to the number of rows used (after FUS-IN-002 and FUS-IN-004) and `n_points` to the number of distinct points after merging (FUS-PTS-002).
- [x] **FUS-PTS-004**: Fusion shall use a sensor as a kriging point whether or not its site lies inside the grid's extent.

## Grid

- [x] **FUS-GRID-001**: When building the grid for a bounding box and `resolution_m`, fusion shall project the box's four corners to EPSG:26918, take their envelope `(min_x, min_y, max_x, max_y)`, and set `x_min = floor(min_x / resolution_m) × resolution_m`, `y_max = ceil(max_y / resolution_m) × resolution_m`, `width = ceil((max_x − x_min) / resolution_m)`, and `height = ceil((y_max − min_y) / resolution_m)`.
- [x] **FUS-GRID-002**: Fusion shall place the centre of the grid cell in column `i` (counting east from 0) and row `j` (counting south from 0, row 0 northernmost) at `(x_min + (i + 0.5) × resolution_m, y_max − (j + 0.5) × resolution_m)` in EPSG:26918.
- [x] **FUS-GRID-003**: Fusion shall build the grid from the bounding box in `FusionSettings` (`DC_METRO` unless `AQDT_BBOX` is set), so that the same settings always yield the same grid.

## Variogram

- [x] **FUS-VAR-001**: When an hour has at least `min_points` points, fusion shall fit a PyKrige `OrdinaryKriging` model to the points for each candidate variogram model, `spherical` and `exponential`, in Euclidean EPSG:26918 coordinates with 6 lag bins, unweighted fitting, and cross-validation statistics enabled.
- [x] **FUS-VAR-002**: When selecting an hour's variogram, fusion shall choose the eligible candidate with the smallest `cR`, and shall choose `spherical` when the candidates' `cR` values are equal.
- [x] **FUS-VAR-003**: If fitting a candidate raises, or the candidate's `cR` is not finite, then fusion shall treat that candidate as not eligible.
- [x] **FUS-VAR-004**: When an hour is kriged, fusion shall record the selected candidate's `variogram_model`, `sill` (partial sill plus nugget), `range_m`, `nugget`, and its cross-validation statistics `q1`, `q2`, and `cr`.
- [x] **FUS-VAR-005**: Fusion shall fit each hour's variogram from that hour's points alone, carrying no variogram parameter from one hour to another.

## Prediction

- [x] **FUS-PRED-001**: When an hour is kriged, fusion shall predict the ordinary-kriging estimate and kriging variance at every grid cell centre using the hour's selected variogram and points.
- [x] **FUS-PRED-002**: When predicting, fusion shall pass the grid cell centres to PyKrige in consecutive chunks of at most `chunk_cells` cells (default 50 000), one prediction call per chunk, and predictions for the same hour with different `chunk_cells` shall agree to within a relative tolerance of 1e-6.
- [x] **FUS-PRED-003**: When storing predictions, fusion shall clamp the estimate and the variance at 0 and store both as float32.

## Hours Kriged and Not Kriged

- [x] **FUS-STATUS-001**: When processing a window, fusion shall produce exactly one `SurfaceFit` row for every hour `H` with `start <= H < end`, with `status` one of `kriged`, `insufficient_points`, `fit_failed`.
- [x] **FUS-STATUS-002**: If an hour has fewer than `min_points` points (including none), then fusion shall record `status = insufficient_points` without fitting a variogram.
- [x] **FUS-STATUS-003**: If an hour has at least `min_points` points and no candidate variogram is eligible (FUS-VAR-003), then fusion shall record `status = fit_failed`.
- [x] **FUS-STATUS-004**: When an hour is not kriged, fusion shall record its `n_sensors` and `n_points` and set `variogram_model`, `sill`, `range_m`, `nugget`, `q1`, `q2`, `cr`, `resolution_m`, `width`, `height`, `surface_uri`, and `surface_sha256` to null.
- [x] **FUS-STATUS-005**: When an hour is not kriged and a raster exists at that hour's raster key, fusion shall delete the raster through the store's `replace_object`, so that a raster exists for an hour exactly when the hour's row has `status = kriged`.

## Surface Raster

- [x] **FUS-RASTER-001**: When an hour `H` is kriged, fusion shall write its raster at the archive key `fusion/surfaces/date={YYYY-MM-DD}/pm25_{YYYY-MM-DDTHH}.tif`, both rendered from `H` in UTC.
- [x] **FUS-RASTER-002**: The surface raster shall be a Cloud-Optimized GeoTIFF with two float32 bands — band 1 described `pm25` with unit `µg/m³`, band 2 described `pm25_variance` with unit `(µg/m³)²` — CRS EPSG:26918, geotransform `(x_min, resolution_m, 0, y_max, 0, −resolution_m)`, NaN as nodata, DEFLATE compression, and 512-pixel internal tiles.
- [x] **FUS-RASTER-003**: The surface raster shall carry dataset tags `hour` (ISO 8601 UTC), `variogram_model`, `sill`, `range_m`, `nugget`, and `n_points`, equal to the hour's `SurfaceFit` row.
- [x] **FUS-RASTER-004**: Two encodings of the same hour from the same points, variogram, and grid shall produce byte-identical rasters, and the raster shall carry no tag whose value depends on when it was written.
- [x] **FUS-RASTER-005**: The surface raster shall open with `rasterio` without importing project code and report the COG layout, its two bands, its CRS, and its tags.
- [x] **FUS-RASTER-006**: When kriging an hour, fusion shall write the raster through the store's `replace_object` with a `produce` callable that reads the hour's inputs, kriges, and returns the encoded raster (or `None` when the hour is not kriged), so that the hour's inputs are always read after the raster's token.

## Storage

- [x] **FUS-STORE-001**: Fusion shall define the enumeration `SurfaceStatus` (`kriged`, `insufficient_points`, `fit_failed`) and the Pydantic record `SurfaceFit` (`hour`, `status`, `n_sensors`, `n_points`, `variogram_model`, `sill`, `range_m`, `nugget`, `q1`, `q2`, `cr`, `resolution_m`, `width`, `height`, `surface_uri`, `surface_sha256`) under `aqdt.fusion.schemas`, with `hour` timezone-aware UTC.
- [x] **FUS-STORE-002**: Fusion shall define a Pandera frame model `SurfaceFitFrame` under `aqdt.fusion.frames`, and the fusion test suite shall include the record-to-frame conformance test the observation store applies to its schemas (each record field present in the frame with matching nullability and compatible dtype, no extra data columns).
- [x] **FUS-STORE-003**: When validating a `SurfaceFitFrame`, fusion shall reject a frame in which `hour` is not unique.
- [x] **FUS-STORE-004**: Fusion shall define the observation-store `Product` `surface_fits` (prefix `fusion/surface_fits`, partition key `date` = UTC date of `hour`, key `(hour)`, file `surface_fits.parquet`, table `surface_fits`) and write its rows through `write_partitioned`.
- [x] **FUS-STORE-005**: Fusion shall keep SQL under `src/aqdt/fusion/sql/` defining the table `surface_fits` with primary key `hour` and columns matching the `SurfaceFit` record.
- [x] **FUS-STORE-006**: Fusion shall register `surface_fits` in `aqdt.registry.PRODUCTS` after calibration's products, so that the observation store's `apply_schema`, `load_partitions`, and `rebuild` handle it; fusion shall not write to PostGIS by any other path and shall load no raster into PostGIS.
- [x] **FUS-STORE-007**: When an hour is kriged, fusion shall set `surface_uri` to the raster's full archive URI and `surface_sha256` to the lowercase hexadecimal SHA-256 of the raster bytes written.

## Runs

- [x] **FUS-RUN-001**: Fusion shall expose `krige_surfaces(archive_uri, start, end, settings, conn=None) -> frame`, which shall process each hour `H` in `[start, end)` in ascending order — write or delete the hour's raster (FUS-RASTER-006, FUS-STATUS-005), then write the hour's `SurfaceFit` row — and, when `conn` is given, call the store's `load_partitions` with every `surface_fits` partition it wrote, and return the window's rows as a validated `SurfaceFitFrame`.
- [x] **FUS-RUN-002**: `krige_surfaces` shall require `start` and `end` to be timezone-aware datetimes, shall convert each to UTC and truncate it to the hour before use, and shall fail with a validation error if either is naive or if `end <= start` after truncation.
- [x] **FUS-RUN-003**: When `krige_surfaces` fails while processing an hour, every earlier hour of the window shall already have both its raster (or its absence) and its `SurfaceFit` row in the archive.
- [x] **FUS-RUN-004**: Two `krige_surfaces` runs over the same window with the same settings against an unchanged archive shall produce equal `SurfaceFit` rows, byte-identical rasters, and byte-identical `surface_fits` partitions.
- [x] **FUS-RUN-005**: When the window contains no calibrated rows at all, `krige_surfaces` shall complete successfully and write a row with `status = insufficient_points` for every hour.

## Settings and Tests

- [x] **FUS-CFG-001**: Fusion shall define `FusionSettings` (Pydantic settings) with `resolution_m` (from `AQDT_FUS_RESOLUTION_M`, default 100), `min_points` (from `AQDT_FUS_MIN_POINTS`, default 10), and `bbox` (from `AQDT_BBOX`, default `DC_METRO`), each optional in the environment.
- [x] **FUS-CFG-002**: If `resolution_m` is not positive or `min_points` is less than 3, then fusion shall fail with a validation error naming the setting.
- [x] **FUS-CFG-003**: Fusion tests shall run against a temporary local archive populated from small synthetic frames, with the S3 path of raster writes exercised through `moto`, and shall require neither a network connection nor a PostGIS connection except for tests of FUS-STORE-005 and FUS-STORE-006, which shall be skipped while `DATABASE_URL` is unset.

## Benchmark

- [x] **FUS-BENCH-001**: Fusion shall provide `python -m aqdt.fusion.benchmark`, accepting a list of resolutions in metres (default 500, 200, 100, 50) and either an archive hour (`--hour`, using `AQDT_ARCHIVE_URI` or `--archive-uri`) or a synthetic point count (`--synthetic-points`, default 100) as the hour to krige.
- [x] **FUS-BENCH-002**: When benchmarking, fusion shall krige the hour at each resolution in a fresh subprocess and report for each the cell count, the wall-clock seconds for the whole hour (read, fit, predict, encode), the wall-clock seconds for prediction alone, and the subprocess's peak resident memory.
- [x] **FUS-BENCH-003**: When benchmarking, fusion shall report the machine's CPU model, logical core count, and total memory, and the Python, PyKrige, and GDAL versions.
- [x] **FUS-BENCH-004**: The benchmark shall write nothing to the archive.
