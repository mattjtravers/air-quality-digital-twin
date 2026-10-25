---
design: maps-design
prefix: MAP
---

# Maps — EARS Specs

Facets: `RENDER` (drawing one surface), `SCALE` (colormaps and limits), `CLIP` (values beyond the
limits), `MARK` (sensor and monitor overlays), `MULTI` (small multiples), `READ` (reading a
surface raster), `SAVE` (figure conventions), `CFG` (settings and tests).

## Rendering

- [x] **MAP-RENDER-001**: Maps shall expose `render_surface(ax, array, grid, *, kind, sensors=None, monitors=None, settings=None) -> RenderResult`, which draws a `(height, width)` array on fusion's grid into `ax` with extent `[x_min, x_min + width·res, y_max − height·res, y_max]` converted to kilometres, row 0 at the top, nearest-cell sampling, and NaN cells transparent.
- [x] **MAP-RENDER-002**: When rendering, maps shall label the x axis `Easting (km, EPSG:26918)` and the y axis `Northing (km)` and set an equal aspect ratio.
- [x] **MAP-RENDER-003**: `RenderResult` shall carry the image's mappable, whether any finite cell exceeds the kind's upper limit, the fraction of finite cells that do, and the array's finite maximum.

## Scales

- [x] **MAP-SCALE-001**: When `kind` is `pm25`, maps shall draw with the `viridis` colormap and continuous normalisation from 0 to `settings.pm25_max`.
- [x] **MAP-SCALE-002**: When `kind` is `sd`, maps shall draw with the `magma` colormap and continuous normalisation from 0 to `settings.sd_max`.
- [x] **MAP-SCALE-003**: Maps shall use the same limits for every array of a kind, whatever the array's own range.

## Clipping and Colorbars

- [x] **MAP-CLIP-001**: Maps shall expose `add_colorbar(fig, mappable, axes, *, kind, clipped)`, labelling the bar `PM2.5 (µg/m³)` for `pm25` and `Standard deviation (µg/m³)` for `sd`, and drawing it with an arrow at the upper end (`extend="max"`) if and only if `clipped` is true.
- [x] **MAP-CLIP-002**: When a cell's value exceeds the upper limit, maps shall draw it in the colormap's top colour.

## Markers

- [x] **MAP-MARK-001**: When `sensors` are given, maps shall draw them as small circles with white fill and a black edge, with the legend label `PurpleAir sensors`.
- [x] **MAP-MARK-002**: When `monitors` are given, maps shall draw them as upward triangles larger than the sensor markers, with red-orange fill and a white edge, with the legend label `AirNow monitors`, above the sensors.

## Small Multiples

- [x] **MAP-MULTI-001**: Maps shall expose `small_multiples(panels, grid, *, kind, settings=None, ncols=2, width_in=6.5) -> (Figure, list[RenderResult])`, which renders each panel (array, title, optional sensors and monitors) through `render_surface` with the same kind and settings, in a grid of `ncols` columns sharing axis limits, with tick labels only on the outer axes.
- [x] **MAP-MULTI-002**: A small-multiples figure shall carry exactly one colorbar for all panels, added through `add_colorbar` with `clipped` true when any panel's `RenderResult` reports clipping.

## Reading a Surface

- [x] **MAP-READ-001**: Maps shall expose `read_surface(uri) -> SurfaceArrays` returning band 1, band 2, the geotransform, and the dataset tags of a surface raster at a local path or S3 URI.

## Figure Conventions

- [x] **MAP-SAVE-001**: Maps shall expose `save_figure(fig, path)`, which saves at 300 dpi with a tight bounding box.

## Settings and Tests

- [x] **MAP-CFG-001**: Maps shall define the constants `PM25_MAX = 12.0` and `SD_MAX = 3.0` and `MapSettings` (Pydantic settings) with `pm25_max` (`AQDT_MAP_PM25_MAX`, default `PM25_MAX`) and `sd_max` (`AQDT_MAP_SD_MAX`, default `SD_MAX`), each optional.
- [x] **MAP-CFG-002**: If `pm25_max` or `sd_max` is not positive, then maps shall fail with a validation error naming the setting.
- [x] **MAP-CFG-003**: Maps tests shall render with matplotlib's non-interactive `Agg` backend and require neither a display nor a network connection.
