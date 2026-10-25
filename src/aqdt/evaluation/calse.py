"""The parameter covariance of a calibration fit, rebuilt from the pairs it was fitted on, with
serial correlation of the hourly residuals accounted for."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import numpy as np
import pandas as pd

HOUR = timedelta(hours=1)
RHO_MAX = 0.99


@dataclass(frozen=True)
class FitCovariance:
    """OLS ``y = intercept + slope × x`` and the covariance of ``(intercept, slope)``; the
    ``*_raw`` values are textbook OLS, the others inflated by ``n / n_eff``."""

    n: int
    slope: float
    intercept: float
    sigma2: float  # residual variance, Σ residual² / (n − 2)
    rho1: float  # lag-1 residual autocorrelation, clipped to [0, 0.99]
    n_eff: float
    var_a_raw: float
    var_b_raw: float
    cov_ab_raw: float
    var_a: float
    var_b: float
    cov_ab: float

    def matrix(self) -> np.ndarray:
        """The corrected covariance of ``(intercept, slope)``."""
        return np.array([[self.var_a, self.cov_ab], [self.cov_ab, self.var_b]])


def lag1_autocorrelation(residuals, series, hours) -> float:
    """``Σ e_t e_{t+1} / Σ e_t²`` over pairs of consecutive hours within one series."""
    frame = pd.DataFrame(
        {
            "e": np.asarray(residuals, dtype=float),
            "s": list(series),
            "h": pd.to_datetime(list(hours), utc=True),
        }
    ).sort_values(["s", "h"], kind="stable")
    denominator = float((frame["e"] ** 2).sum())
    if denominator == 0:
        return 0.0
    same = frame["s"].to_numpy()[1:] == frame["s"].to_numpy()[:-1]
    step = (frame["h"].to_numpy()[1:] - frame["h"].to_numpy()[:-1]) == np.timedelta64(1, "h")
    e = frame["e"].to_numpy()
    return float((e[1:] * e[:-1])[same & step].sum() / denominator)


# @spec EVAL-CALSE-002, EVAL-CALSE-003, EVAL-CALSE-004
def fit_covariance(x, y, series, hours) -> FitCovariance | None:
    """The fit over the pairs and its parameter covariance; ``None`` for fewer than three pairs
    or a constant ``x``."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < 3:
        return None
    x_mean = x.mean()
    sxx = float(((x - x_mean) ** 2).sum())
    if sxx == 0:
        return None
    slope = float(((x - x_mean) * (y - y.mean())).sum() / sxx)
    intercept = float(y.mean() - slope * x_mean)
    residual = y - (intercept + slope * x)
    sigma2 = float((residual**2).sum() / (n - 2))
    var_b = sigma2 / sxx
    var_a = sigma2 * (1.0 / n + x_mean**2 / sxx)
    cov_ab = -x_mean * sigma2 / sxx
    rho1 = min(max(lag1_autocorrelation(residual, series, hours), 0.0), RHO_MAX)
    n_eff = n * (1 - rho1) / (1 + rho1)
    factor = n / n_eff
    return FitCovariance(
        n=n,
        slope=slope,
        intercept=intercept,
        sigma2=sigma2,
        rho1=rho1,
        n_eff=n_eff,
        var_a_raw=var_a,
        var_b_raw=var_b,
        cov_ab_raw=cov_ab,
        var_a=var_a * factor,
        var_b=var_b * factor,
        cov_ab=cov_ab * factor,
    )


POOLED = "pooled"  # the site_id under which an as_of's pooled fit is keyed


@dataclass(frozen=True, eq=False)
class RebuiltFits:
    """Every fit in use, rebuilt: covariances keyed ``(site_id or POOLED, as_of)``; the
    fit-uncertainty table; the stored fit rows; and each fit's pair snapshots (``hour``, ``a``,
    ``b``) for the residual decomposition."""

    covariances: dict[tuple[str, pd.Timestamp], FitCovariance | None]
    table: pd.DataFrame
    stored: pd.DataFrame
    snapshots: dict[tuple[str, pd.Timestamp], pd.DataFrame]


TABLE_COLUMNS = [
    "site_id",
    "fit_as_of",
    "status",
    "n",
    "rho1",
    "n_eff",
    "sigma2",
    "var_a",
    "var_b",
    "cov_ab",
    "var_a_raw",
    "var_b_raw",
    "cov_ab_raw",
    "slope",
    "intercept",
    "stored_n_pairs",
    "stored_slope",
    "stored_intercept",
]


def _snapshots(observations: pd.DataFrame) -> pd.DataFrame:
    """The PurpleAir snapshots hourly aggregation uses, with their channels."""
    trusted = observations[
        observations["qc_flags"].map(len).eq(0) & observations["pm25_corrected"].notna()
    ]
    return pd.DataFrame(
        {
            "site_id": trusted["site_id"].to_numpy(),
            "hour": trusted["observed_at"].dt.floor("h").to_numpy(),
            "a": trusted["pm25_channel_a"].to_numpy(dtype=float),
            "b": trusted["pm25_channel_b"].to_numpy(dtype=float),
        }
    ).dropna(subset=["a", "b"])


def _pair_snapshots(snapshots: pd.DataFrame, pairs: pd.DataFrame) -> pd.DataFrame:
    keys = pairs[["site_id", "hour"]].drop_duplicates()
    keys = keys.assign(hour=pd.to_datetime(keys["hour"], utc=True))
    snaps = snapshots.assign(hour=pd.to_datetime(snapshots["hour"], utc=True))
    return snaps.merge(keys, on=["site_id", "hour"], how="inner")[["site_id", "hour", "a", "b"]]


# @spec EVAL-CALSE-001, EVAL-CALSE-004, EVAL-CALSE-005
def rebuild_fits(
    archive_uri: str,
    fitted: set[tuple[str, pd.Timestamp]],
    pooled: set[pd.Timestamp],
    settings,
) -> RebuiltFits:
    """Rebuild the pairs of every fit in use exactly as ``fit_calibrations`` builds them —
    observations over ``[as_of − window_days, as_of)``, ``aggregate_hourly``,
    ``calibration_pairs`` — and compute each fit's covariance."""
    from aqdt.calibration.fit import calibration_pairs
    from aqdt.calibration.hourly import aggregate_hourly
    from aqdt.calibration.products import FITS
    from aqdt.observation_store.archive import read_observations, read_partitioned, read_sites
    from aqdt.observation_store.products import render_partition_value
    from aqdt.observation_store.schemas import Source

    as_ofs = sorted({a for _, a in fitted} | set(pooled))
    covariances: dict[tuple[str, pd.Timestamp], FitCovariance | None] = {}
    snapshots_by_fit: dict[tuple[str, pd.Timestamp], pd.DataFrame] = {}
    rows: list[dict] = []
    if not as_ofs:
        empty = pd.DataFrame(columns=TABLE_COLUMNS)
        return RebuiltFits(covariances, empty, pd.DataFrame(), snapshots_by_fit)
    window = timedelta(days=settings.window_days)
    span_start = (as_ofs[0] - window).to_pydatetime()
    span_end = as_ofs[-1].to_pydatetime()
    sensors_all = read_observations(archive_uri, Source.purpleair, span_start, span_end)
    monitors_all = read_observations(archive_uri, Source.airnow, span_start, span_end)
    sites = read_sites(archive_uri)
    rendered = {render_partition_value(a.to_pydatetime()) for a in as_ofs}
    stored = read_partitioned(archive_uri, FITS, as_of=lambda v: v in rendered)
    stored_by_key = {(r.site_id, pd.Timestamp(r.as_of)): r for r in stored.itertuples(index=False)}

    for as_of in as_ofs:
        start = as_of - window
        in_window = lambda frame: frame[  # noqa: E731
            (frame["observed_at"] >= start) & (frame["observed_at"] < as_of)
        ]
        sensors = in_window(sensors_all)
        built = calibration_pairs(
            aggregate_hourly(sensors), in_window(monitors_all), sites, settings
        )
        snapshots = _snapshots(sensors)
        wanted = sorted(s for s, a in fitted if a == as_of)
        jobs = [(s, built.pairs.get(s), "fitted") for s in wanted]
        if as_of in pooled:
            jobs.append((POOLED, built.pooled, "pooled"))
        for site_id, pairs, status in jobs:
            if pairs is None or pairs.empty:
                pairs = pd.DataFrame(columns=["site_id", "hour", "x", "y"])
            if "site_id" not in pairs.columns:
                pairs = pairs.assign(site_id=site_id)
            cov = fit_covariance(pairs["x"], pairs["y"], pairs["site_id"], pairs["hour"])
            covariances[(site_id, as_of)] = cov
            snapshots_by_fit[(site_id, as_of)] = _pair_snapshots(snapshots, pairs)
            if status == "pooled":
                example = next(
                    (
                        r
                        for (s, a), r in stored_by_key.items()
                        if a == as_of and r.status == "pooled"
                    ),
                    None,
                )
            else:
                example = stored_by_key.get((site_id, as_of))
            rows.append(
                {
                    "site_id": site_id,
                    "fit_as_of": as_of,
                    "status": status,
                    "n": len(pairs),
                    **{
                        k: (getattr(cov, k) if cov is not None else np.nan)
                        for k in TABLE_COLUMNS[4:15]
                    },
                    "stored_n_pairs": getattr(example, "n_pairs", np.nan),
                    "stored_slope": getattr(example, "slope", np.nan),
                    "stored_intercept": getattr(example, "intercept", np.nan),
                }
            )
    table = pd.DataFrame(rows, columns=TABLE_COLUMNS)
    return RebuiltFits(covariances, table, stored, snapshots_by_fit)
