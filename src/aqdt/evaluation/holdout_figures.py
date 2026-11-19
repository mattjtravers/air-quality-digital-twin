"""The holdout run's figures: each a 300 dpi PNG sized for one column or full width, with the
data it draws written beside it as a CSV of the same name."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from aqdt.evaluation.figures import COLUMN_IN, FULL_IN  # noqa: E402
from aqdt.maps.render import Panel, save_figure, small_multiples  # noqa: E402
from aqdt.maps.settings import MapSettings  # noqa: E402

LOCAL_TZ = "America/New_York"
#: categorical slots in fixed order (validated: CVD-separated; labels and CSVs carry identity)
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, MUTED, GRID = "#1f1f1e", "#6b6a63", "#d9d8d2"
#: steps 200 and 100 of the series-1 ramp: the outer band, and the lighter inner band on top
BAND_OUTER, BAND_INNER = "#9ec5f4", "#cde2fb"
SD_KINDS = {
    "kriging": "kriging only",
    "total": "total",
    "total_dev_offset": "total + D-offset",
    "rf_trees": "forest tree spread",
}
AXIS_LABELS = {
    "reliability_coverage": ["Nominal central coverage (%)", "Observed coverage (%)"],
    "spread_skill": ["Predicted total SD (µg/m³)", "RMSE at the monitors (µg/m³)"],
    "by_distance": ["Distance to the nearest sensor (km)", "SD or |error| (µg/m³)"],
    "by_local_hour": ["Hour of day (America/New_York, h)", "SD or |error| (µg/m³)"],
    "by_monitor": ["RMSE (µg/m³)", "Coverage (%)"],
    "methods_test": ["RMSE (µg/m³)", "Bias (µg/m³)"],
    "shap_beeswarm": ["SHAP value (µg/m³)"],
    "example_timeseries": ["Local time (America/New_York, h)", "PM2.5 (µg/m³)"],
}
# Numbered in this order in figures.md (EVAL-HOLD-020).
CAPTIONS = {
    "reliability_coverage": "Observed against nominal central coverage on T's scored set (%), "
    "for the kriging, total, total + D-offset, and forest tree-spread SDs, with 95% "
    "day-bootstrap intervals and the 1:1 line.",
    "spread_skill": "RMSE at the monitors (µg/m³, with 95% intervals) against the mean and RMS "
    "predicted total SD in each total-SD quintile of T's scored set, with the 1:1 line.",
    "by_distance": "Mean kriging SD, mean total SD, and mean |error| (µg/m³, with 95% "
    "intervals) by distance from the monitor to its nearest sensor on T's scored set; each "
    "bin holds only the few monitors counted on its label.",
    "by_local_hour": "Mean kriging SD, mean total SD, and mean |error| (µg/m³, with 95% "
    "intervals) by local hour of day on T's scored set.",
    "by_monitor": "RMSE (µg/m³, with 95% intervals) and ±1 and ±2 SD coverage (%) with the "
    "kriging, total, and total + D-offset SDs at each monitor on T's scored set.",
    "total_sd_map": "Total SD (kriging plus calibration, µg/m³) at every cell for the typical "
    "T hour, with the hour's sensors marked by fit status and the monitors.",
    "methods_test": "RMSE and bias (µg/m³, with 95% day-bootstrap intervals) of kriging, the "
    "development-trained forest, IDW, the hour mean, and the in-window forest (reference "
    "only) on T's unflagged matchups.",
    "shap_beeswarm": "SHAP values (µg/m³) of the development-trained forest for each feature "
    "on a seeded sample of T's kriging points, coloured by feature value.",
    "example_timeseries": "One T day at three monitors: AirNow PM2.5, the surface, the "
    "out-of-sample ±2 √(total variance + σ²_shared(D)) band, and the lighter ±2 total SD band "
    "(µg/m³).",
    "example_maps": "Surface PM2.5 (µg/m³) on the example day at 06:00, 12:00, and 18:00 and "
    "the next 00:00 local time, on a fixed 0–10 µg/m³ scale, sensors and monitors overlaid.",
}


def _style(ax) -> None:
    ax.grid(True, color=GRID, linewidth=0.5)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(MUTED)
    ax.tick_params(colors=INK, labelsize=7)


def _write(fig, data: pd.DataFrame, out: Path, name: str) -> None:
    data.to_csv(out / f"{name}.csv", index=False)
    save_figure(fig, out / f"{name}.png")
    plt.close(fig)


def _errorbars(lo, value, hi) -> np.ndarray:
    lo, value, hi = (np.asarray(v, dtype=float) for v in (lo, value, hi))
    return np.vstack([np.nan_to_num(value - lo), np.nan_to_num(hi - value)])


# @spec EVAL-HOLD-007
def reliability_coverage(table: pd.DataFrame, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(COLUMN_IN, COLUMN_IN), layout="constrained")
    ax.plot([40, 100], [40, 100], color=MUTED, linewidth=1, linestyle="--", label="1:1")
    markers = ["o", "s", "D", "^"]
    for k, (kind, label) in enumerate(SD_KINDS.items()):
        rows = table[table["sd_kind"] == kind]
        ax.errorbar(
            100 * rows["nominal"],
            100 * rows["observed"],
            yerr=100 * _errorbars(rows["ci_low"], rows["observed"], rows["ci_high"]),
            color=SERIES[k],
            marker=markers[k],
            markersize=4,
            linewidth=1.5,
            capsize=2,
            label=label,
        )
    ax.set_xlim(40, 100)
    ax.set_ylim(0, 100)
    ax.set_xlabel(AXIS_LABELS["reliability_coverage"][0])
    ax.set_ylabel(AXIS_LABELS["reliability_coverage"][1])
    _style(ax)
    ax.legend(frameon=False, fontsize=7, loc="upper left")
    _write(fig, table, out, "reliability_coverage")


# @spec EVAL-HOLD-008
def spread_skill(table: pd.DataFrame, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(COLUMN_IN, COLUMN_IN), layout="constrained")
    top = float(np.nanmax(table[["rmse_ci_high", "rms_total_sd", "rmse"]].to_numpy())) * 1.1
    ax.plot([0, top], [0, top], color=MUTED, linewidth=1, linestyle="--", label="1:1")
    err = _errorbars(table["rmse_ci_low"], table["rmse"], table["rmse_ci_high"])
    ax.errorbar(table["rms_total_sd"], table["rmse"], yerr=err, color=SERIES[0], marker="o",
                markersize=5, linewidth=1.5, capsize=2, label="against RMS total SD")
    ax.errorbar(table["mean_total_sd"], table["rmse"], yerr=err, color=SERIES[1], marker="s",
                markersize=5, linewidth=1.5, capsize=2, label="against mean total SD")
    ax.set_xlim(0, top)
    ax.set_ylim(0, top)
    ax.set_xlabel(AXIS_LABELS["spread_skill"][0])
    ax.set_ylabel(AXIS_LABELS["spread_skill"][1])
    _style(ax)
    ax.legend(frameon=False, fontsize=7, loc="upper left")
    _write(fig, table, out, "spread_skill")


# @spec EVAL-HOLD-009
def by_group(table: pd.DataFrame, out: Path, name: str) -> None:
    width = FULL_IN if name == "by_local_hour" else COLUMN_IN
    fig, ax = plt.subplots(figsize=(width, 2.6), layout="constrained")
    x = np.arange(len(table))
    ax.plot(x, table["mean_sd"], color=SERIES[0], marker="o", markersize=4, linewidth=1.5,
            label="mean kriging SD")
    ax.plot(x, table["mean_total_sd"], color=SERIES[1], marker="s", markersize=4, linewidth=1.5,
            label="mean total SD")
    ax.errorbar(x, table["mae"], yerr=_errorbars(table["mae_ci_low"], table["mae"],
                                                 table["mae_ci_high"]),
                color=SERIES[2], marker="D", markersize=4, linewidth=1.5, capsize=2,
                label="mean |error|")
    ax.set_xticks(x)
    if name == "by_distance":
        ax.set_xticklabels([f"{g}\n({m} mon.)" for g, m in zip(table["group"],
                                                               table["n_monitors"])])
    else:
        ax.set_xticklabels([str(g) for g in table["group"]])
    ax.set_ylim(bottom=0)
    ax.set_xlabel(AXIS_LABELS[name][0])
    ax.set_ylabel(AXIS_LABELS[name][1])
    _style(ax)
    ax.legend(frameon=False, fontsize=7, ncols=3, loc="lower center", bbox_to_anchor=(0.5, 1.0))
    _write(fig, table, out, name)


# @spec EVAL-HOLD-010
def by_monitor(table: pd.DataFrame, out: Path) -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(FULL_IN, 3.2), sharey=True,
                                      layout="constrained")
    y = np.arange(len(table))
    names = table["name"].astype(str).tolist()
    left.errorbar(table["rmse"], y, xerr=_errorbars(table["rmse_ci_low"], table["rmse"],
                                                    table["rmse_ci_high"]),
                  color=SERIES[0], marker="o", markersize=4, linestyle="none", capsize=2)
    left.set_yticks(y)
    left.set_yticklabels(names)
    left.invert_yaxis()
    left.set_xlim(left=0)
    left.set_xlabel(AXIS_LABELS["by_monitor"][0])
    kinds = [("kriging", "kriging"), ("total", "total"), ("total_dev_offset", "total + D-offset")]
    offsets = [-0.22, 0.0, 0.22]
    for k, ((kind, label), dy) in enumerate(zip(kinds, offsets)):
        right.scatter(100 * table[f"cov1_{kind}"], y + dy, color=SERIES[k + 1], marker="o",
                      s=14, label=f"±1 SD, {label}", zorder=3, clip_on=False)
        right.scatter(100 * table[f"cov2_{kind}"], y + dy, facecolor="white",
                      edgecolor=SERIES[k + 1], marker="s", s=14, label=f"±2 SD, {label}",
                      zorder=3, clip_on=False)
    for nominal in (68.3, 95.4):
        right.axvline(nominal, color=MUTED, linewidth=1, linestyle="--")
    right.set_xlim(0, 100)
    right.set_xlabel(AXIS_LABELS["by_monitor"][1])
    for ax in (left, right):
        _style(ax)
    handles, labels = right.get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncols=3, frameon=False, fontsize=6)
    _write(fig, table, out, "by_monitor")


# @spec EVAL-HOLD-011
def total_sd_map(hour, total_sd, grid, sensors, monitors, settings: MapSettings, out: Path):
    """The typical hour's total SD on Maps' ``sd`` scale, sensors marked by fit status; returns
    the hour-level numbers written beside the sensors in the CSV."""
    local = pd.Timestamp(hour).tz_convert(LOCAL_TZ)
    panel = Panel(
        array=total_sd,
        title=f"{pd.Timestamp(hour):%Y-%m-%d %H:00} UTC ({local:%H:%M %Z}) · total SD",
        sensors=None,
        monitors=(monitors["x"].to_numpy(float), monitors["y"].to_numpy(float)),
    )
    fig, results = small_multiples([panel], grid, kind="sd", settings=settings, ncols=1,
                                   width_in=COLUMN_IN + 1.0)
    ax = fig.axes[0]
    for status, style in (
        ("fitted", {"marker": "o", "s": 14, "facecolor": "black", "edgecolor": "white"}),
        ("pooled", {"marker": "s", "s": 14, "facecolor": "white", "edgecolor": "black"}),
    ):
        part = sensors[sensors["fit_status"] == status]
        ax.scatter(part["x"] / 1000, part["y"] / 1000, linewidths=0.6, zorder=3,
                   label=f"sensor, {status} ({len(part)})", **style)
    handles, labels = ax.get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncols=2, frameon=False, fontsize=6)
    finite = np.isfinite(total_sd)
    info = {
        "hour": pd.Timestamp(hour),
        "finite_cells": int(finite.sum()),
        "max_total_sd": float(np.nanmax(total_sd)),
        "frac_above_sd_max": float((total_sd[finite] > settings.sd_max).mean()),
        "sd_max": settings.sd_max,
    }
    data = sensors[["site_id", "x", "y", "fit_status"]].assign(**info)
    data = data[[*info, "site_id", "x", "y", "fit_status"]]
    _write(fig, data, out, "total_sd_map")
    return info


# @spec EVAL-HOLD-015
def methods_test(table: pd.DataFrame, out: Path) -> None:
    fig, (left, right) = plt.subplots(1, 2, figsize=(FULL_IN, 2.4), sharey=True,
                                      layout="constrained")
    y = np.arange(len(table))
    labels = [m + (" (reference only)" if m == "rf_in_window" else "") for m in table["method"]]
    for ax, metric, xlabel in ((left, "rmse", AXIS_LABELS["methods_test"][0]),
                               (right, "bias", AXIS_LABELS["methods_test"][1])):
        ax.errorbar(table[metric], y,
                    xerr=_errorbars(table[f"{metric}_ci_low"], table[metric],
                                    table[f"{metric}_ci_high"]),
                    color=SERIES[0], marker="o", markersize=4, linestyle="none", capsize=2)
        ax.set_xlabel(xlabel)
        _style(ax)
    right.axvline(0, color=MUTED, linewidth=1)
    left.set_xlim(left=0)
    left.set_yticks(y)
    left.set_yticklabels(labels)
    left.invert_yaxis()
    _write(fig, table, out, "methods_test")


# @spec EVAL-HOLD-016
def shap_beeswarm(X: pd.DataFrame, values: pd.DataFrame, out: Path) -> None:
    import shap

    shap.summary_plot(values.to_numpy(), X, show=False, plot_size=(FULL_IN, 3.0),
                      color_bar_label="Feature value")
    fig = plt.gcf()
    for ax in fig.axes:
        ax.tick_params(labelsize=7)
        ax.yaxis.label.set_size(8)
    fig.axes[0].set_xlabel(AXIS_LABELS["shap_beeswarm"][0], fontsize=8)
    fig.set_size_inches(FULL_IN, 3.0)
    _write(fig, pd.concat([X, values], axis=1), out, "shap_beeswarm")


# @spec EVAL-HOLD-018
def example_timeseries(series: pd.DataFrame, out: Path) -> None:
    monitors = list(dict.fromkeys(series["site_id"]))
    fig, axes = plt.subplots(len(monitors), 1, figsize=(FULL_IN, 1.9 * len(monitors) + 0.5),
                             sharex=True, squeeze=False, layout="constrained")
    for ax, site_id in zip(axes[:, 0], monitors):
        part = series[series["site_id"] == site_id]
        t = pd.to_datetime(part["hour"], utc=True).dt.tz_convert(LOCAL_TZ)
        ax.fill_between(t, part["lower_oos"], part["upper_oos"], color=BAND_OUTER, linewidth=0,
                        label="±2 √(total var. + σ²_shared(D)), out of sample")
        ax.fill_between(t, part["lower_total"], part["upper_total"], color=BAND_INNER,
                        linewidth=0, label="±2 total SD")
        ax.plot(t, part["surface"], color=SERIES[0], linewidth=2, label="surface")
        good = ~part["flagged"].astype(bool)
        ax.scatter(t[good], part.loc[good, "obs"], color=SERIES[1], s=12, zorder=3,
                   label="AirNow")
        if (~good).any():
            ax.scatter(t[~good], part.loc[~good, "obs"], facecolor="white", edgecolor=SERIES[1],
                       s=12, zorder=3, label="AirNow, flagged")
        ax.set_title(f"{part['name'].iloc[0]} ({site_id})", fontsize=8, loc="left")
        ax.set_ylabel(AXIS_LABELS["example_timeseries"][1])
        ax.set_ylim(bottom=0)
        _style(ax)
    axes[-1, 0].xaxis.set_major_formatter(
        matplotlib.dates.DateFormatter("%H:%M", tz=LOCAL_TZ)
    )
    axes[-1, 0].set_xlabel(AXIS_LABELS["example_timeseries"][0])
    handles, labels = axes[0, 0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncols=3, frameon=False, fontsize=7)
    _write(fig, series, out, "example_timeseries")


# @spec EVAL-HOLD-018
def example_maps(hours, surfaces, grid, points: pd.DataFrame, monitors: pd.DataFrame, out: Path):
    settings = MapSettings(pm25_max=10.0, sd_max=MapSettings().sd_max)
    panels = []
    for hour, surface in zip(hours, surfaces):
        local = pd.Timestamp(hour).tz_convert(LOCAL_TZ)
        p = points[points["hour"] == hour]
        panels.append(
            Panel(
                array=surface.pm25,
                title=f"{local:%Y-%m-%d %H:%M %Z}",
                sensors=(p["x"].to_numpy(float), p["y"].to_numpy(float)),
                monitors=(monitors["x"].to_numpy(float), monitors["y"].to_numpy(float)),
            )
        )
    fig, results = small_multiples(panels, grid, kind="pm25", settings=settings)
    handles, labels = fig.axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="outside lower center", ncols=2, frameon=False)
    data = pd.DataFrame(
        {
            "hour": list(hours),
            "local_time": [pd.Timestamp(h).tz_convert(LOCAL_TZ).strftime("%Y-%m-%d %H:%M %Z")
                           for h in hours],
            "max": [r.max for r in results],
            "clipped_fraction": [r.clipped_fraction for r in results],
        }
    )
    _write(fig, data, out, "example_maps")


# @spec EVAL-HOLD-020
def write_captions(out: Path, written: list[str]) -> None:
    lines = ["# Figures", ""]
    number = 0
    for name, caption in CAPTIONS.items():
        if name not in written:
            continue
        number += 1
        lines += [f"**Figure {number}.** `{name}.png` — {caption}", ""]
    (out / "figures.md").write_text("\n".join(lines))
