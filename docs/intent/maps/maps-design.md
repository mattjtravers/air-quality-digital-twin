---
parent: high-level-design
prefix: MAP
---

# Maps

## Context and Design Philosophy

The twin's hourly surfaces are looked at in more than one place: evaluation's report figures and
the dashboard's frames. A surface map is only comparable to another if both use the same
colour scale, so the scale cannot be chosen per hour from the hour's own range — a clean hour
would then look as alarming as a smoky one. Maps owns the one renderer every map figure goes
through, with fixed colour limits set in configuration and colormaps chosen so equal steps in
concentration look like equal steps in colour.

Principles:

1. **One renderer.** Every map of a surface, estimate or uncertainty, is coloured by the same
   scale definition, whether drawn as a figure or as a bare image; a consumer chooses what to draw,
   not how it looks.
2. **Fixed scales.** Colour limits are configuration, the same for every hour. A value beyond the
   limit is clipped to the top colour and the colorbar says so; the scale never stretches to fit.
3. **Perceptually uniform, continuous.** Sequential colormaps whose lightness rises evenly, drawn
   as a smooth gradient, readable in greyscale and by viewers with common colour-vision
   deficiencies.
4. **Readable overlays.** Sensors and monitors are drawn with markers that stay distinguishable
   anywhere on either colormap.

Maps renders; it reads no archive and writes no files of its own. Animation is not part of it.

## Rendering

`render_surface(ax, array, grid, *, kind, sensors=None, monitors=None, settings=None) ->
RenderResult` draws one surface into a matplotlib `Axes`:

- `array` — a `(height, width)` array on fusion's grid (row 0 northernmost); NaN cells are drawn
  transparent.
- `grid` — fusion's `Grid`, which places the image: extent `[x_min, x_min + width·res, y_max −
  height·res, y_max]` in EPSG:26918, shown in kilometres.
- `kind` — `pm25` (the estimate, µg/m³), `diff` (the estimate minus the hour's mean, µg/m³; the
  caller subtracts the mean), or `sd` (the standard deviation, µg/m³; the caller takes the square
  root of a variance band).
- `sensors`, `monitors` — optional `(x, y)` arrays in EPSG:26918 metres.

| `kind` | Colormap | Limits | Colorbar label |
|---|---|---|---|
| `pm25` | `viridis` | `0` to `settings.pm25_max` | `PM2.5 (µg/m³)` |
| `diff` | `RdBu_r` | `−settings.diff_max` to `+settings.diff_max` | `Above / below this hour's average (µg/m³)` |
| `sd` | `magma` | `0` to `settings.sd_max` | `Standard deviation (µg/m³)` |

`diff` shows where the surface sits above (red) or below (blue) its own hourly mean, centred on
white at zero. An hour whose estimate barely moves on the absolute scale still shows its spatial
pattern here, because the limits are a few µg/m³ either side of zero rather than the whole range
of concentrations.

The image is drawn with continuous (unbinned) normalisation and bilinear-free nearest-cell
sampling, so each cell shows its own value and the gradient comes from the colormap, not from
levels. Axes are labelled `Easting (km, EPSG:26918)` and `Northing (km)`, with equal aspect.

`RenderResult` carries the matplotlib mappable (for a colorbar), whether any finite cell fell
outside the kind's limits, the fraction of finite cells that did, and the array's maximum.

**Colorbars.** `add_colorbar(fig, mappable, ax_or_axes, *, kind, clipped)` labels the bar for the
kind and, when `clipped` is true, draws it with arrows at the ends values can pass: the top end
for `pm25` and `sd` (`extend="max"`), both ends for `diff` (`extend="both"`). Without clipping
the bar ends flat.

**Markers.** Sensors: small circles, white fill, thin black edge. Monitors: larger upward
triangles, red-orange fill, white edge. Both carry legend entries when drawn. White-with-black and
red-orange-with-white each keep contrast against both ends of `viridis` and `magma`.

## Bare images

The dashboard overlays surfaces on a basemap and draws its own legend, so it needs the colours
without the figure.

`colorize(array, *, kind, settings=None) -> ndarray` maps a 2-D array to `(height, width, 4)`
RGBA `uint8` pixels with the kind's colormap and limits, the same scale `render_surface` uses:
values at or below the lower limit take the bottom colour, values above the upper limit the top
colour, and
NaN cells are fully transparent (alpha 0). Finite cells are opaque; the consumer sets any
overlay opacity.

`scale(kind, settings=None) -> (cmap, min, max, label)` names the kind's colormap, its lower and
upper limits, and its colorbar label, so a consumer describing the scale states it from the same table.
`colour_stops(kind, n=11, settings=None) -> list[str]` returns `n` colours evenly spaced from the
kind's lower limit to its upper limit as `#rrggbb` strings, so a legend drawn outside matplotlib uses the
same colours as the pixels.

## Small multiples

`small_multiples(panels, grid, *, kind, settings=None, ncols=2, width_in=6.5) -> (Figure,
list[RenderResult])` draws several surfaces on one fixed scale: one `render_surface` per panel
(each panel an array, a title, optional sensors and monitors), a shared colorbar for the whole
figure labelled for the kind, with the clipping arrow when any panel clipped. Panels share axes
limits; only the outer axes carry tick labels.

## Reading a surface

`read_surface(uri) -> SurfaceArrays` opens a surface raster (local path or S3 URI) and returns band
1, band 2, the geotransform, and the tags — what a renderer needs, with no other project code.

## Figure conventions

Figures meant for print are saved at 300 dpi with a tight bounding box, sized for 3.3 in (one
column) or 6.5 in (full width). These are the conventions every evaluation figure follows too;
`save_figure(fig, path)` applies them.

## Settings

`MapSettings` (Pydantic settings from the environment):

| Setting | Variable | Default |
|---|---|---|
| `pm25_max` | `AQDT_MAP_PM25_MAX` | 10.0 µg/m³ |
| `diff_max` | `AQDT_MAP_DIFF_MAX` | 3.0 µg/m³ (the `diff` scale runs from `−diff_max` to `+diff_max`) |
| `sd_max` | `AQDT_MAP_SD_MAX` | 3.0 µg/m³ |

All optional; a non-positive value fails validation naming the setting. The defaults are named
constants in the module (`PM25_MAX`, `DIFF_MAX`, `SD_MAX`), overridden by the environment in the same way the
bounding box's `DC_METRO` is by `AQDT_BBOX`.

## Package Layout

```
src/aqdt/maps/
  settings.py   # MapSettings, PM25_MAX, DIFF_MAX, SD_MAX
  render.py     # render_surface, add_colorbar, small_multiples, save_figure, colorize,
                #   colour_stops
  surface.py    # read_surface
tests/maps/
```

Dependency: `matplotlib`, in the `analysis` dependency group.

## Decisions

| Decision | Chosen | Rationale |
|----------|--------|-----------|
| PM2.5 colormap | `viridis` | Perceptually uniform and monotonic in lightness, legible in greyscale and for common colour-vision deficiencies; its light top end puts attention on the high concentrations. |
| Uncertainty colormap | `magma` | Also perceptually uniform, and visibly different from `viridis`, so an uncertainty map is never mistaken for a concentration map. |
| Colour limits | Fixed from settings, 0–10 µg/m³ for PM2.5 | Hours are comparable only on a common scale. 10 µg/m³ spans the clean-to-moderate range the D.C. network reports most hours and sits just above the 9 µg/m³ annual standard, so ordinary hours use most of the ramp and anything above the standard reads in the top colours. |
| Difference colormap | `RdBu_r`, symmetric fixed limits (±3 µg/m³) | A diverging map centred on zero shows where an hour sits above or below its own mean; red–blue stays distinguishable for the common colour-vision deficiencies, and white at zero keeps "no difference" neutral. Symmetric fixed limits keep hours comparable with each other, as on the other scales. |
| Out-of-range values | Clipped to the end colour, colorbar arrow at that end, clipping reported | The scale stays fixed and the reader is told, rather than the scale silently stretching. |
| Rendering library | matplotlib | The standard for publication figures in the scientific Python ecosystem; its colormaps colour both the report figures and the dashboard's bare images. |
| Bare-image colouring | `colorize` and `colour_stops` beside `render_surface`, sharing its scale table | A frame with no axes still has to read on the same scale as a figure; one scale table means the two cannot diverge. |
| Coordinates shown | EPSG:26918 kilometres | The grid's native CRS; no resampling, and distances read directly off the axes. |

## Open Questions & Future Decisions

1. A basemap layer (county lines, the Potomac and Anacostia) under the surface in report figures;
   the dashboard draws its own basemap.
2. Whether the dashboard needs an AQI-category view beside the continuous scale.

## References

- `docs/high-level-design.md` — Maps; Dashboard.
- `docs/intent/fusion/fusion-design.md` — the grid and the surface raster.
- Crameri, F., Shephard, G. E., and Heron, P. J. (2020). The misuse of colour in science
  communication. *Nature Communications* 11, 5444.
- matplotlib documentation — perceptually uniform sequential colormaps.
