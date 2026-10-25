"""The one renderer for surface maps: fixed, perceptually uniform colour scales, with sensors and
monitors overlaid. Evaluation's figures and the dashboard both draw maps through it."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import Normalize

from aqdt.fusion.grid import Grid
from aqdt.maps.settings import MapSettings

DPI = 300
_SCALES = {
    # kind: (colormap, settings attribute holding the upper limit, colorbar label)
    "pm25": ("viridis", "pm25_max", "PM2.5 (µg/m³)"),
    "sd": ("magma", "sd_max", "Standard deviation (µg/m³)"),
}
SENSOR_STYLE = {"marker": "o", "s": 9, "facecolor": "white", "edgecolor": "black", "zorder": 3}
MONITOR_STYLE = {"marker": "^", "s": 42, "facecolor": "#f4511e", "edgecolor": "white", "zorder": 4}
Points = tuple[np.ndarray, np.ndarray]


@dataclass(frozen=True, eq=False)
class RenderResult:
    mappable: Any
    clipped: bool  # some finite cell exceeds the upper limit
    clipped_fraction: float  # of finite cells
    max: float  # the array's finite maximum (NaN for an all-NaN array)


@dataclass(frozen=True, eq=False)
class Panel:
    array: np.ndarray
    title: str
    sensors: Points | None = None
    monitors: Points | None = None


def _scale(kind: str, settings: MapSettings) -> tuple[str, float, str]:
    if kind not in _SCALES:
        raise ValueError(f"kind must be one of {sorted(_SCALES)}, got {kind!r}")
    cmap, attribute, label = _SCALES[kind]
    return cmap, float(getattr(settings, attribute)), label


# @spec MAP-RENDER-001, MAP-RENDER-002, MAP-RENDER-003, MAP-SCALE-001, MAP-SCALE-002
# @spec MAP-SCALE-003, MAP-CLIP-002, MAP-MARK-001, MAP-MARK-002
def render_surface(
    ax,
    array: np.ndarray,
    grid: Grid,
    *,
    kind: str,
    sensors: Points | None = None,
    monitors: Points | None = None,
    settings: MapSettings | None = None,
) -> RenderResult:
    """Draw one surface on its kind's fixed scale; cells above the limit take the top colour."""
    cmap, top, _ = _scale(kind, settings or MapSettings())
    res = grid.resolution_m
    extent = [
        grid.x_min / 1000,
        (grid.x_min + grid.width * res) / 1000,
        (grid.y_max - grid.height * res) / 1000,
        grid.y_max / 1000,
    ]
    data = np.ma.masked_invalid(np.asarray(array, dtype=float))
    image = ax.imshow(
        data,
        extent=extent,
        origin="upper",
        cmap=cmap,
        norm=Normalize(vmin=0.0, vmax=top, clip=False),
        interpolation="nearest",
    )
    finite = np.asarray(array, dtype=float)
    finite = finite[np.isfinite(finite)]
    above = finite > top
    if sensors is not None:
        ax.scatter(
            sensors[0] / 1000,
            sensors[1] / 1000,
            linewidths=0.5,
            label="PurpleAir sensors",
            **SENSOR_STYLE,
        )
    if monitors is not None:
        ax.scatter(
            monitors[0] / 1000,
            monitors[1] / 1000,
            linewidths=0.7,
            label="AirNow monitors",
            **MONITOR_STYLE,
        )
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_xlabel("Easting (km, EPSG:26918)")
    ax.set_ylabel("Northing (km)")
    ax.set_aspect(1.0)
    return RenderResult(
        mappable=image,
        clipped=bool(above.any()),
        clipped_fraction=float(above.mean()) if finite.size else 0.0,
        max=float(finite.max()) if finite.size else math.nan,
    )


# @spec MAP-CLIP-001
def add_colorbar(fig, mappable, axes, *, kind: str, clipped: bool):
    """The kind's colorbar, with an arrow at the top end exactly when values were clipped."""
    _, _, label = _SCALES[kind]
    colorbar = fig.colorbar(mappable, ax=axes, extend="max" if clipped else "neither")
    colorbar.set_label(label)
    return colorbar


# @spec MAP-MULTI-001, MAP-MULTI-002
def small_multiples(
    panels: Sequence[Panel],
    grid: Grid,
    *,
    kind: str,
    settings: MapSettings | None = None,
    ncols: int = 2,
    width_in: float = 6.5,
):
    """Several surfaces on one fixed scale, sharing axes limits and one colorbar."""
    settings = settings or MapSettings()
    nrows = max(1, math.ceil(len(panels) / ncols))
    aspect = (grid.height / grid.width) if grid.width else 1.0
    panel_in = (width_in - 1.0) / ncols  # less the shared colorbar and its label
    height_in = nrows * (panel_in * aspect + 0.45) + 0.7  # panel titles; axis labels and legend
    fig, axes = plt.subplots(
        nrows,
        ncols,
        figsize=(width_in, height_in),
        sharex=True,
        sharey=True,
        squeeze=False,
        layout="constrained",
    )
    results: list[RenderResult] = []
    for ax, panel in zip(axes.flat, panels):
        results.append(
            render_surface(
                ax,
                panel.array,
                grid,
                kind=kind,
                sensors=panel.sensors,
                monitors=panel.monitors,
                settings=settings,
            )
        )
        ax.set_title(panel.title, fontsize=8)
    for ax in list(axes.flat)[len(panels) :]:
        ax.set_visible(False)
    for ax in axes.flat:
        ax.label_outer()
        ax.tick_params(labelsize=7)
        ax.xaxis.label.set_size(8)
        ax.yaxis.label.set_size(8)
    if results:
        add_colorbar(
            fig,
            results[0].mappable,
            axes[:, :].ravel().tolist(),
            kind=kind,
            clipped=any(r.clipped for r in results),
        )
    return fig, results


# @spec MAP-SAVE-001
def save_figure(fig, path: str | Path) -> None:
    """Save for print: 300 dpi, tight bounding box."""
    fig.savefig(path, dpi=DPI, bbox_inches="tight")
