"""The report's figures: each a 300 dpi PNG sized for one column (3.5 in) or full width
(6.5 in), with the data it draws written beside it as a CSV of the same name."""

from __future__ import annotations

import textwrap
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from aqdt.maps.render import save_figure  # noqa: E402

COLUMN_IN = 3.5
FULL_IN = 6.5
AXIS_LABELS = {
    "timeseries": ["Hour (UTC)", "Bias (µg/m³)", "RMSE (µg/m³)"],
    "scatter": ["AirNow PM2.5 (µg/m³)", "Surface PM2.5 (µg/m³)"],
    "coverage": ["Matchups within band (%)"],
    "bias_vs_pooled": ["Pooled share of kriging inputs (%)", "Hourly bias (µg/m³)"],
    "decomposition": ["Share of calibration residual variance (%)"],
    "mc_vs_analytic": [
        "Analytic calibration variance ((µg/m³)²)",
        "Monte Carlo calibration variance ((µg/m³)²)",
    ],
    "lomo_rmse": ["RMSE at the monitor (µg/m³)"],
    "baselines_rmse": ["RMSE (µg/m³)", "Bias (µg/m³)"],
    "reliability": ["Surface PM2.5, decile mean (µg/m³)", "AirNow PM2.5, decile mean (µg/m³)"],
    "qq_ztotal": ["Standard-normal quantile (SD)", "z_total quantile (SD)"],
}
# Numbered in this order in figures.md (EVAL-FIG-007).
CAPTIONS = {
    "timeseries": "Hourly bias and RMSE of the surface at the monitors (unflagged, µg/m³), "
    "coloured by date, with the interquartile range of the hour's errors.",
    "scatter": "Surface against AirNow PM2.5 at every unflagged matchup (µg/m³), with the 1:1 "
    "line and R² (skill).",
    "coverage": "Share of unflagged matchups within ±1 and ±2 SD with the kriging SD and the "
    "total SD (%), against the Gaussian nominal.",
    "bias_vs_pooled": "Hourly bias (unflagged, µg/m³) against the hour's pooled share of "
    "kriging inputs (%), with a least-squares line.",
    "decomposition": "Shares of the calibration residual variance from detector noise, "
    "reference error, and the remainder (%), by fit status and reference bound.",
    "mc_vs_analytic": "Monte Carlo against analytic calibration variance at the sampled "
    "matchups ((µg/m³)²), with the 1:1 line.",
    "error_budget": "The error budget: every known error source, where it enters, its type, "
    "how it is handled, and its magnitude.",
    "lomo_rmse": "RMSE at each monitor of the stored surface, the control re-krige, and the "
    "re-krige without the monitor in calibration (unflagged LOMO rows, µg/m³).",
    "baselines_rmse": "RMSE and bias of kriging and each baseline over the unflagged matchups "
    "(µg/m³), with 95% day-bootstrap intervals.",
    "reliability": "Mean AirNow PM2.5 per decile of the surface (unflagged, µg/m³) with 95% "
    "day-bootstrap intervals and bin counts, against the 1:1 line.",
    "qq_ztotal": "Quantiles of z_total (unflagged, SD units) against standard-normal "
    "quantiles, with the 1:1 line.",
    "maps_pm25": "Surface PM2.5 at four hours with regular variograms (µg/m³), on one fixed "
    "scale, sensors and monitors overlaid.",
    "maps_sd": "Surface standard deviation at the same four hours (µg/m³), on one fixed scale.",
    "maps_pm25_10": "Surface PM2.5 at the same four hours on a 0–10 µg/m³ scale, so clean-air "
    "hours use more of the colour range.",
}
plt.rcParams.update(
    {
        "font.size": 8,
        "axes.titlesize": 8,
        "axes.labelsize": 8,
        "legend.fontsize": 7,
        "xtick.labelsize": 7,
        "ytick.labelsize": 7,
    }
)


# @spec EVAL-FIG-001, EVAL-FIG-002
def _write(fig, data: pd.DataFrame, out: Path, name: str) -> None:
    data.to_csv(out / f"{name}.csv", index=False)
    save_figure(fig, out / f"{name}.png")
    plt.close(fig)


# @spec EVAL-FIG-003
def timeseries(matchups: pd.DataFrame, out: Path) -> None:
    """Per-hour bias and RMSE (unflagged), coloured by date, with the matchup errors' IQR."""
    frame = matchups[~matchups["flagged"]]
    per_hour = (
        frame.groupby("hour")["error"]
        .agg(
            bias="mean",
            rmse=lambda e: float(np.sqrt((e**2).mean())),
            q25=lambda e: e.quantile(0.25),
            q75=lambda e: e.quantile(0.75),
            n="size",
        )
        .reset_index()
    )
    hours = pd.to_datetime(per_hour["hour"], utc=True).dt.tz_localize(None)
    colour = mdates.date2num(hours.dt.floor("D"))
    fig, (top, bottom) = plt.subplots(
        2, 1, figsize=(FULL_IN, 4.2), sharex=True, layout="constrained"
    )
    top.fill_between(
        hours, per_hour["q25"], per_hour["q75"], color="0.85", lw=0, label="IQR of monitor errors"
    )
    top.axhline(0, color="0.4", lw=0.6)
    top.plot(hours, per_hour["bias"], color="0.6", lw=0.4)
    points = top.scatter(hours, per_hour["bias"], c=colour, cmap="cividis", s=5, zorder=3)
    bottom.plot(hours, per_hour["rmse"], color="0.6", lw=0.4)
    bottom.scatter(hours, per_hour["rmse"], c=colour, cmap="cividis", s=5, zorder=3)
    top.set_ylabel(AXIS_LABELS["timeseries"][1])
    bottom.set_ylabel(AXIS_LABELS["timeseries"][2])
    bottom.set_xlabel(AXIS_LABELS["timeseries"][0])
    bottom.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    top.legend(loc="upper left", frameon=False)
    bar = fig.colorbar(points, ax=[top, bottom], pad=0.01, aspect=30)
    bar.ax.yaxis.set_major_formatter(mdates.DateFormatter("%m-%d"))
    bar.set_label("Date (UTC)")
    _write(fig, per_hour, out, "timeseries")


# @spec EVAL-FIG-003
def scatter(matchups: pd.DataFrame, r2: float, out: Path) -> None:
    """Surface against monitor, all hours (unflagged), with the 1:1 line and R²."""
    frame = matchups[~matchups["flagged"]][["hour", "site_id", "obs", "surface"]]
    fig, ax = plt.subplots(figsize=(COLUMN_IN, COLUMN_IN), layout="constrained")
    ax.scatter(frame["obs"], frame["surface"], s=3, alpha=0.35, color="#3b528b", lw=0)
    top = float(np.nanmax([frame["obs"].max(), frame["surface"].max(), 1.0])) * 1.05
    low = min(0.0, float(np.nanmin([frame["obs"].min(), frame["surface"].min(), 0.0])))
    ax.plot([low, top], [low, top], color="0.2", lw=0.8, ls="--", label="1:1")
    ax.set_xlim(low, top)
    ax.set_ylim(low, top)
    ax.set_aspect(1.0)
    ax.set_xlabel(AXIS_LABELS["scatter"][0])
    ax.set_ylabel(AXIS_LABELS["scatter"][1])
    ax.text(0.04, 0.95, f"R² = {r2:.2f}\nn = {len(frame)}", transform=ax.transAxes, va="top")
    ax.legend(loc="lower right", frameon=False)
    _write(fig, frame, out, "scatter")


# @spec EVAL-FIG-003
def coverage(headline: dict, out: Path) -> None:
    """±1 SD and ±2 SD coverage with kriging SD and total SD, against the Gaussian nominal."""
    data = pd.DataFrame(
        {
            "band": ["±1 SD", "±2 SD"],
            "kriging_sd": [headline["cov1_krig"], headline["cov2_krig"]],
            "total_sd": [headline["cov1_total"], headline["cov2_total"]],
            "nominal": [0.683, 0.954],
        }
    )
    fig, ax = plt.subplots(figsize=(COLUMN_IN, 2.8), layout="constrained")
    x = np.arange(2)
    ax.bar(x - 0.18, 100 * data["kriging_sd"], 0.34, label="Kriging SD", color="#5ec962")
    ax.bar(x + 0.18, 100 * data["total_sd"], 0.34, label="Total SD", color="#3b528b")
    for k, nominal in enumerate(data["nominal"]):
        ax.hlines(100 * nominal, k - 0.4, k + 0.4, color="0.2", ls="--", lw=0.8)
    ax.set_xticks(x, data["band"])
    ax.set_ylim(0, 105)
    ax.set_ylabel(AXIS_LABELS["coverage"][0])
    ax.legend(loc="upper left", frameon=False, title="dashed: Gaussian nominal", title_fontsize=6)
    _write(fig, data, out, "coverage")


# @spec EVAL-FIG-003
def bias_vs_pooled(matchups: pd.DataFrame, out: Path) -> None:
    """Per-hour bias (unflagged) against the hour's pooled share, with a least-squares line."""
    frame = matchups[~matchups["flagged"]]
    per_hour = frame.groupby("hour").agg(
        bias=("error", "mean"), pooled_share=("pooled_share", "first")
    )
    per_hour = per_hour.reset_index()
    fig, ax = plt.subplots(figsize=(COLUMN_IN, 2.8), layout="constrained")
    share = 100 * per_hour["pooled_share"]
    ax.scatter(share, per_hour["bias"], s=5, color="#3b528b", alpha=0.6, lw=0)
    if share.nunique() > 1:
        slope, intercept = np.polyfit(share, per_hour["bias"], 1)
        xs = np.array([share.min(), share.max()])
        ax.plot(
            xs,
            intercept + slope * xs,
            color="#f4511e",
            lw=1,
            label=f"slope {slope:.3f} µg/m³ per %",
        )
        ax.legend(loc="best", frameon=False)
    ax.axhline(0, color="0.4", lw=0.6)
    ax.set_xlabel(AXIS_LABELS["bias_vs_pooled"][0])
    ax.set_ylabel(AXIS_LABELS["bias_vs_pooled"][1])
    _write(fig, per_hour, out, "bias_vs_pooled")


def decomposition(table: pd.DataFrame, out: Path) -> None:
    """Stacked shares of detector noise, reference error, and remainder."""
    fig, ax = plt.subplots(figsize=(FULL_IN, 2.8), layout="constrained")
    labels = [f"{g}\n{r}" for g, r in zip(table["group"], table["reference"])]
    x = np.arange(len(table))
    bottom = np.zeros(len(table))
    for column, label, colour in (
        ("share_a", "(a) detector noise", "#fde725"),
        ("share_b", "(b) reference error", "#21918c"),
        ("share_c", "(c) representativeness / model", "#440154"),
    ):
        values = 100 * table[column].fillna(0).to_numpy()
        ax.bar(x, values, 0.6, bottom=bottom, label=label, color=colour)
        bottom += values
    ax.set_xticks(x, labels)
    ax.set_ylim(0, 105)
    ax.set_ylabel(AXIS_LABELS["decomposition"][0])
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0), frameon=False)
    _write(fig, table, out, "decomposition")


def mc_vs_analytic(table: pd.DataFrame, out: Path) -> None:
    """Monte Carlo against analytic calibration variance, shared and independent pooled error."""
    fig, ax = plt.subplots(figsize=(COLUMN_IN, COLUMN_IN), layout="constrained")
    ax.scatter(
        table["analytic"], table["monte_carlo"], s=8, color="#3b528b", label="Pooled error shared"
    )
    ax.scatter(
        table["analytic"],
        table["monte_carlo_independent"],
        s=8,
        marker="x",
        color="#f4511e",
        lw=0.6,
        label="Pooled error independent",
    )
    values = table[["analytic", "monte_carlo", "monte_carlo_independent"]].to_numpy().ravel()
    values = values[np.isfinite(values) & (values > 0)]
    if values.size:
        lo, hi = values.min() / 1.5, values.max() * 1.5
        ax.plot([lo, hi], [lo, hi], color="0.2", lw=0.8, ls="--", label="1:1")
        ax.set_xscale("log")
        ax.set_yscale("log")
        ax.set_xlim(lo, hi)
        ax.set_ylim(lo, hi)
    ax.set_xlabel(AXIS_LABELS["mc_vs_analytic"][0])
    ax.set_ylabel(AXIS_LABELS["mc_vs_analytic"][1])
    ax.legend(loc="upper left", frameon=False)
    _write(fig, table, out, "mc_vs_analytic")


def error_budget(table: pd.DataFrame, out: Path) -> None:
    """The error budget as a figure-ready table."""
    widths = {"source": 30, "stage": 12, "type": 11, "handling": 13, "magnitude": 34}
    cells = [[textwrap.fill(str(row[c]), widths[c]) for c in widths] for _, row in table.iterrows()]
    lines = [max(cell.count("\n") + 1 for cell in row) for row in cells]
    units = [1.6] + [n + 0.6 for n in lines]  # header, then each row: text lines plus padding
    fig, ax = plt.subplots(figsize=(FULL_IN, 0.12 * sum(units) + 0.1))
    fig.subplots_adjust(left=0.01, right=0.99, top=0.99, bottom=0.01)
    ax.axis("off")
    total = sum(widths.values())
    rendered = ax.table(
        cellText=cells,
        colLabels=[c.capitalize() for c in widths],
        colWidths=[w / total for w in widths.values()],
        cellLoc="left",
        bbox=[0, 0, 1, 1],
    )
    rendered.auto_set_font_size(False)
    rendered.set_fontsize(6)
    for (row, _), cell in rendered.get_celld().items():
        cell.set_height(units[row] / sum(units))
        cell.set_linewidth(0.4)
        if row == 0:
            cell.set_text_props(weight="bold")
    _write(fig, table, out, "error_budget")


# @spec EVAL-FIG-006
def lomo_rmse(table: pd.DataFrame, out: Path) -> None:
    """Per monitor: RMSE of the stored, control, and without-monitor errors (unflagged rows)."""
    rows = table[~table["flagged"].astype(bool)]
    data = (
        rows.groupby("site_id")[["stored_error", "control_error", "lomo_error"]]
        .agg(lambda e: float(np.sqrt((e.dropna() ** 2).mean())) if e.notna().any() else np.nan)
        .reset_index()
    )
    fig, ax = plt.subplots(figsize=(FULL_IN, 2.8), layout="constrained")
    y = np.arange(len(data))
    for k, (column, label, colour) in enumerate(
        (
            ("stored_error", "Stored surface", "#5ec962"),
            ("control_error", "Control re-krige", "#21918c"),
            ("lomo_error", "Without the monitor", "#3b528b"),
        )
    ):
        ax.barh(y + (k - 1) * 0.27, data[column], 0.26, label=label, color=colour)
    ax.set_yticks(y, [s.split(":")[-1] for s in data["site_id"]])
    ax.invert_yaxis()
    ax.set_xlabel(AXIS_LABELS["lomo_rmse"][0])
    ax.legend(loc="lower right", frameon=False)
    _write(fig, data, out, "lomo_rmse")


# @spec EVAL-FIG-006
def baselines_rmse(table: pd.DataFrame, out: Path) -> None:
    """RMSE and bias of each method, unflagged and overall, with day-bootstrap intervals."""
    data = table[(table["subset"] == "unflagged") & (table["split"] == "overall")].reset_index(
        drop=True
    )
    fig, axes = plt.subplots(1, 2, figsize=(FULL_IN, 2.6), layout="constrained")
    x = np.arange(len(data))
    labels = AXIS_LABELS["baselines_rmse"]
    for ax, metric, label in zip(axes, ("rmse", "bias"), labels, strict=True):
        value = data[metric].to_numpy(dtype=float)
        lo = value - data[f"{metric}_ci_low"].to_numpy(dtype=float)
        hi = data[f"{metric}_ci_high"].to_numpy(dtype=float) - value
        errors = np.vstack([np.nan_to_num(lo), np.nan_to_num(hi)])
        ax.bar(x, value, 0.6, yerr=errors, capsize=2, color="#3b528b")
        ax.set_xticks(x, [m.replace("_", " ") for m in data["method"]], rotation=30, ha="right")
        ax.set_ylabel(label)
        ax.axhline(0, color="0.3", lw=0.6)
    _write(fig, data, out, "baselines_rmse")


# @spec EVAL-FIG-006
def reliability(table: pd.DataFrame, out: Path) -> None:
    """Mean obs per surface decile with intervals, the 1:1 line, and each bin's count."""
    fig, ax = plt.subplots(figsize=(COLUMN_IN, COLUMN_IN), layout="constrained")
    x = table["surface_mean"].to_numpy(dtype=float)
    y = table["obs_mean"].to_numpy(dtype=float)
    lo = np.nan_to_num(y - table["obs_ci_low"].to_numpy(dtype=float))
    hi = np.nan_to_num(table["obs_ci_high"].to_numpy(dtype=float) - y)
    ax.errorbar(x, y, yerr=np.vstack([lo, hi]), fmt="o", ms=3, capsize=2, color="#3b528b")
    top = float(np.nanmax(np.concatenate([x, y + hi]))) * 1.08 if len(x) else 1.0
    ax.plot([0, top], [0, top], color="0.3", lw=0.8, ls="--", label="1:1")
    for xi, yi, n in zip(x, y, table["n"], strict=True):
        ax.annotate(f"{int(n)}", (xi, yi), textcoords="offset points", xytext=(3, -8), fontsize=5)
    ax.set_xlim(0, top)
    ax.set_ylim(0, top)
    ax.set_aspect(1.0)
    ax.set_xlabel(AXIS_LABELS["reliability"][0])
    ax.set_ylabel(AXIS_LABELS["reliability"][1])
    ax.legend(loc="upper left", frameon=False)
    _write(fig, table, out, "reliability")


# @spec EVAL-FIG-006
def qq_ztotal(z, out: Path) -> None:
    """Sorted z_total against standard-normal quantiles, with the 1:1 line."""
    from statistics import NormalDist

    values = np.sort(np.asarray(z, dtype=float)[np.isfinite(z)])
    probabilities = (np.arange(1, len(values) + 1) - 0.5) / max(len(values), 1)
    theory = np.array([NormalDist().inv_cdf(p) for p in probabilities])
    data = pd.DataFrame({"normal_quantile": theory, "z_total": values})
    fig, ax = plt.subplots(figsize=(COLUMN_IN, COLUMN_IN), layout="constrained")
    ax.plot(theory, values, ".", ms=2, color="#3b528b")
    edge = float(np.nanmax(np.abs(np.concatenate([theory, values])))) if len(values) else 3.0
    ax.plot([-edge, edge], [-edge, edge], color="0.3", lw=0.8, ls="--", label="1:1")
    ax.set_xlabel(AXIS_LABELS["qq_ztotal"][0])
    ax.set_ylabel(AXIS_LABELS["qq_ztotal"][1])
    ax.legend(loc="upper left", frameon=False)
    _write(fig, data, out, "qq_ztotal")


# @spec EVAL-FIG-007
def write_captions(out: Path, written: list[str]) -> None:
    """``figures.md``: every figure written, numbered in the report's order, with a caption."""
    lines = ["# Figures", ""]
    number = 0
    for name, caption in CAPTIONS.items():
        if name not in written:
            continue
        number += 1
        lines.append(f"**Figure {number}.** `{name}.png` — {caption}")
        lines.append("")
    (out / "figures.md").write_text("\n".join(lines))
