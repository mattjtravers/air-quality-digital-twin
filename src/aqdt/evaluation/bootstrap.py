"""Block bootstraps: resampling whole UTC days (or LOMO hours) so dependent matchups stay
together, and the day-bootstrap intervals of every metric-table row."""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import pandas as pd

N_RESAMPLES = 1000
INTERVAL_METRICS = [
    "bias",
    "mae",
    "rmse",
    "r2",
    "pearson_r",
    "pearson_r2",
    "cov1_krig",
    "cov2_krig",
    "cov1_total",
    "cov2_total",
    "cov1_total_raw",
    "cov2_total_raw",
]
_Z_KINDS = {"krig": "z", "total": "z_total", "total_raw": "z_total_raw"}


def days(hours) -> np.ndarray:
    """The UTC date of each hour, as ``YYYY-MM-DD`` labels."""
    return pd.to_datetime(pd.Series(hours), utc=True).dt.strftime("%Y-%m-%d").to_numpy()


def resample_counts(n_blocks: int, n: int, seed: int) -> np.ndarray:
    """``n`` × ``n_blocks`` draw counts: each row draws ``n_blocks`` blocks with replacement."""
    rng = np.random.default_rng(seed)
    return rng.multinomial(n_blocks, np.full(n_blocks, 1.0 / n_blocks), size=n)


def _percentiles(values: np.ndarray) -> tuple[float, float]:
    values = np.asarray(values, dtype=float)
    values = values[np.isfinite(values)]
    if not len(values):
        return (np.nan, np.nan)
    lo, hi = np.percentile(values, [2.5, 97.5])
    return (float(lo), float(hi))


# @spec EVAL-METRIC-004
def block_interval(
    blocks, stat: Callable[[np.ndarray], float], seed: int, n: int = N_RESAMPLES
) -> tuple[float, float]:
    """The 95% percentile interval of ``stat`` (a function of row positions) over ``n`` block
    resamples of ``blocks`` (one label per row); null with fewer than two blocks."""
    blocks = np.asarray(blocks)
    labels, inverse = np.unique(blocks, return_inverse=True)
    if len(labels) < 2:
        return (np.nan, np.nan)
    rows = [np.flatnonzero(inverse == b) for b in range(len(labels))]
    values = []
    for counts in resample_counts(len(labels), n, seed):
        idx = np.concatenate([np.tile(rows[b], c) for b, c in enumerate(counts) if c])
        values.append(stat(idx))
    return _percentiles(np.array(values, dtype=float))


def _day_sums(frame: pd.DataFrame, labels: np.ndarray, inverse: np.ndarray) -> np.ndarray:
    obs = frame["obs"].to_numpy(dtype=float)
    surface = frame["surface"].to_numpy(dtype=float)
    error = frame["error"].to_numpy(dtype=float)
    reproduced = frame["inputs_reproduced"].to_numpy(dtype=bool)
    columns = [np.ones_like(obs), error, np.abs(error), error**2, obs, obs**2, surface,
               surface**2, surface * obs]
    for kind, name in _Z_KINDS.items():
        z = frame[name].to_numpy(dtype=float) if name in frame else np.full(len(frame), np.nan)
        valid = np.isfinite(z) & (reproduced if kind != "krig" else True)
        az = np.where(valid, np.abs(np.nan_to_num(z)), np.inf)
        columns += [valid.astype(float), (az <= 1).astype(float), (az <= 2).astype(float)]
    data = np.column_stack(columns)
    out = np.zeros((len(labels), data.shape[1]))
    np.add.at(out, inverse, data)
    return out


def _from_sums(t: np.ndarray) -> dict[str, np.ndarray]:
    n, se, sae, se2, so, so2, ss, ss2, sso = (t[:, k] for k in range(9))
    with np.errstate(divide="ignore", invalid="ignore"):
        out = {"bias": se / n, "mae": sae / n, "rmse": np.sqrt(se2 / n)}
        ss_obs = so2 - so**2 / n
        ss_surface = ss2 - ss**2 / n
        ok = (n >= 2) & (ss_obs > 0)
        out["r2"] = np.where(ok, 1.0 - se2 / ss_obs, np.nan)
        r = (sso - ss * so / n) / np.sqrt(ss_obs * ss_surface)
        r = np.where(ok & (ss_surface > 0), r, np.nan)
        out["pearson_r"], out["pearson_r2"] = r, r**2
        for k, kind in enumerate(_Z_KINDS):
            valid, c1, c2 = (t[:, 9 + 3 * k + j] for j in range(3))
            out[f"cov1_{kind}"] = np.where(valid > 0, c1 / valid, np.nan)
            out[f"cov2_{kind}"] = np.where(valid > 0, c2 / valid, np.nan)
    return out


# @spec EVAL-METRIC-003
def metric_intervals(frame: pd.DataFrame, seed: int, n: int = N_RESAMPLES) -> dict[str, float]:
    """``<metric>_ci_low`` / ``_ci_high`` of every interval metric over ``n`` day resamples of
    ``frame``'s matchups, computed from per-day sums; null when the rows span fewer than two
    days."""
    out = {f"{m}_ci_{side}": np.nan for m in INTERVAL_METRICS for side in ("low", "high")}
    if len(frame) == 0:
        return out
    labels, inverse = np.unique(days(frame["hour"]), return_inverse=True)
    if len(labels) < 2:
        return out
    totals = resample_counts(len(labels), n, seed) @ _day_sums(frame, labels, inverse)
    values = _from_sums(totals)
    for name in INTERVAL_METRICS:
        out[f"{name}_ci_low"], out[f"{name}_ci_high"] = _percentiles(values[name])
    return out


def sums_interval(
    blocks, data, fn: Callable[[np.ndarray], np.ndarray], seed: int, n: int = N_RESAMPLES
) -> tuple[float, float]:
    """``block_interval`` of a statistic computable from column sums: ``data`` is rows ×
    columns, and ``fn`` maps resamples × column totals to one value per resample."""
    blocks = np.asarray(blocks)
    labels, inverse = np.unique(blocks, return_inverse=True)
    if len(labels) < 2:
        return (np.nan, np.nan)
    data = np.asarray(data, dtype=float).reshape(len(blocks), -1)
    sums = np.zeros((len(labels), data.shape[1]))
    np.add.at(sums, inverse, data)
    with np.errstate(divide="ignore", invalid="ignore"):
        return _percentiles(fn(resample_counts(len(labels), n, seed) @ sums))


def mean_interval(blocks, values, seed: int, n: int = N_RESAMPLES) -> tuple[float, float]:
    """``block_interval`` of the mean of ``values``, computed from per-block sums."""
    values = np.asarray(values, dtype=float)
    data = np.column_stack([np.ones_like(values), values])
    return sums_interval(blocks, data, lambda t: t[:, 1] / t[:, 0], seed, n)
