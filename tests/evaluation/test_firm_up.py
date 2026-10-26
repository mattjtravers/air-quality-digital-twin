"""Common offset and within-hour anomaly, pooled-share leverage, the split-day shared offset,
spike tiers, lag differences, and the headline intervals: pure functions against constructed
data."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from aqdt.evaluation import anomaly, diagnostics, report, spikes

DAY0 = datetime(2026, 10, 1, tzinfo=UTC)


def _hourly(n_days=10, hours_per_day=6, monitors=6, seed=3, offset_sd=2.0, anomaly_sd=1.0):
    """Matchups whose error is an hour-wide offset plus a per-monitor anomaly. ``surface`` gets the
    offset only (it places the within-hour pattern perfectly); ``idw`` gets both; ``rf`` the
    anomaly only; ``hour_mean`` is the monitors' hour mean plus the offset."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        for h in range(hours_per_day):
            hour = DAY0 + timedelta(days=d, hours=h)
            offset = rng.normal(0, offset_sd)
            obs = rng.uniform(3, 12, monitors)
            noise = rng.normal(0, anomaly_sd, monitors)
            noise -= noise.mean()  # an anomaly sums to 0 within the hour
            for k in range(monitors):
                rows.append(
                    {
                        "hour": hour,
                        "site_id": f"airnow:{k}",
                        "obs": obs[k],
                        "flagged": False,
                        "surface": obs[k] + offset,
                        "idw": obs[k] + offset + noise[k],
                        "rf": obs[k] + noise[k],
                        "hour_mean": obs.mean() + offset,
                    }
                )
    frame = pd.DataFrame(rows)
    frame["error"] = frame["surface"] - frame["obs"]
    return frame


# --- Common offset and within-hour anomaly ----------------------------------------------------


# @spec EVAL-ANOM-001
def test_offset_and_anomaly_shares_split_the_mse_exactly():
    frame = _hourly()
    table = anomaly.anomaly_table(frame, seed=0).set_index("method")
    assert list(table.index) == ["kriging", "idw", "rf", "hour_mean"]
    for method in table.index:
        assert table.loc[method, "offset_share"] + table.loc[method, "anomaly_share"] == (
            pytest.approx(1.0)
        )
        assert table.loc[method, "offset_share_ci_low"] <= table.loc[method, "offset_share"]
    # kriging's error is the offset alone; the forest's the anomaly alone
    assert table.loc["kriging", "offset_share"] == pytest.approx(1.0)
    assert table.loc["rf", "anomaly_share"] == pytest.approx(1.0)
    e = frame["idw"] - frame["obs"]
    offset = e.groupby(frame["hour"]).transform("mean")
    assert table.loc["idw", "offset_share"] == pytest.approx((offset**2).sum() / (e**2).sum())


# @spec EVAL-ANOM-001
def test_hours_with_fewer_than_five_unflagged_matchups_are_left_out():
    frame = _hourly(monitors=6)
    first = frame["hour"] == frame["hour"].min()
    frame.loc[first & frame["site_id"].isin(["airnow:0", "airnow:1"]), "flagged"] = True
    table = anomaly.anomaly_table(frame, seed=0).set_index("method")
    hours = frame["hour"].nunique()
    assert table.loc["kriging", "hours"] == hours - 1
    assert table.loc["kriging", "n"] == 6 * (hours - 1)


# @spec EVAL-ANOM-002
def test_anomaly_and_hourly_mean_correlations():
    frame = _hourly()
    table = anomaly.anomaly_table(frame, seed=0).set_index("method")
    # kriging places the within-hour pattern exactly
    assert table.loc["kriging", "anomaly_r"] == pytest.approx(1.0)
    assert table.loc["kriging", "anomaly_rmse"] == pytest.approx(0.0, abs=1e-9)
    # the hourly mean has no within-hour pattern: no correlation, and its anomaly RMSE is the
    # monitors' own within-hour spread
    assert np.isnan(table.loc["hour_mean", "anomaly_r"])
    spread = frame["obs"] - frame.groupby("hour")["obs"].transform("mean")
    assert table.loc["hour_mean", "anomaly_rmse"] == pytest.approx(np.sqrt((spread**2).mean()))
    # the forest carries no offset, so its hourly means track the monitors' exactly
    assert table.loc["rf", "hourly_r"] == pytest.approx(1.0)
    assert table.loc["kriging", "hourly_r"] < table.loc["rf", "hourly_r"]


# @spec EVAL-ANOM-003
def test_reductions_against_the_hourly_mean():
    frame = _hourly()
    table = anomaly.anomaly_table(frame, seed=0).set_index("method")
    k = table.loc["kriging"]
    assert k["anomaly_reduction_ci_low"] > 0  # kriging gains within the hour
    assert k["offset_reduction"] == pytest.approx(0.0, abs=1e-9)  # same offset as hour_mean
    assert np.isnan(table.loc["hour_mean", "anomaly_reduction"])
    for column in ("n", "hours", "offset_reduction_ci_high", "anomaly_r_ci_low",
                   "hourly_r_ci_high", "anomaly_rmse_ci_low"):
        assert column in table.columns


# @spec EVAL-ANOM-005
def test_variance_explained_splits_the_monitors_variance_by_hour():
    frame = _hourly()
    table = anomaly.anomaly_table(frame, seed=0).set_index("method")
    obs = frame["obs"]
    hour_mean = obs.groupby(frame["hour"]).transform("mean")
    across = ((hour_mean - obs.mean()) ** 2).mean()
    within = ((obs - hour_mean) ** 2).mean()
    k = table.loc["kriging"]
    assert k["v_across"] == pytest.approx(across) and k["v_within"] == pytest.approx(within)
    assert k["across_share"] == pytest.approx(across / (across + within))
    # kriging places the within-hour pattern exactly; the hourly mean places none of it
    assert k["within_explained"] == pytest.approx(1.0)
    assert table.loc["hour_mean", "within_explained"] == pytest.approx(0.0, abs=1e-9)
    # the forest carries no offset, so it explains every bit of the across-hour variance
    assert table.loc["rf", "across_explained"] == pytest.approx(1.0)
    e = frame["surface"] - obs
    offset = e.groupby(frame["hour"]).transform("mean")
    assert k["across_explained"] == pytest.approx(1 - (offset**2).mean() / across)
    for column in ("across_explained_ci_low", "within_explained_ci_high", "across_share_ci_low"):
        assert column in table.columns


# @spec EVAL-ANOM-004
def test_skill_source_compares_the_variance_explained_in_each_part():
    assert anomaly.skill_source(7.7, 0.68, 3.66, 0.16) == "the hour's level"
    assert anomaly.skill_source(1.0, 0.5, 3.0, 0.5) == "the within-hour pattern"


# @spec EVAL-ANOM-004
def test_anomaly_answer_puts_the_variance_table_beside_the_verdict():
    table = anomaly.anomaly_table(_hourly(), seed=0)
    text = report.anomaly_answer(table)
    lower = text.lower()
    assert "where does kriging's skill come from" in lower
    for phrase in ("offset", "anomaly", "idw", "random forest", "hourly mean",
                   "mainly from the within-hour pattern", "variance explained"):
        assert phrase in lower, phrase
    t = table.set_index("method")
    header = next(line for line in text.splitlines() if "| Method" in line)
    assert _pct_text(t.loc["kriging", "across_share"]) in header
    assert _pct_text(t.loc["kriging", "within_share"]) in header
    rows = [line for line in text.splitlines() if line.strip().startswith("| ")]
    assert len(rows) == 1 + 4  # the header and one row per method


# @spec EVAL-ANOM-004
def test_anomaly_answer_without_qualifying_hours():
    frame = _hourly(monitors=4)
    table = anomaly.anomaly_table(frame, seed=0)
    assert (table["n"] == 0).all()
    assert "at least 5" in report.anomaly_answer(table)


def _pct_text(value: float) -> str:
    return f"{100 * value:.1f}%"


# --- Pooling ----------------------------------------------------------------------------------


def _pooled(seed=4):
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(20):
        day_shift = rng.normal(0, 2)
        for k in range(30):
            share = 0.0 if k % 2 else rng.uniform(0, 0.08)
            rows.append(
                {
                    "hour": DAY0 + timedelta(days=d, hours=k % 24),
                    "pooled_share_10km": share,
                    "error": day_shift + 3.0 * share + rng.normal(0, 0.05),
                    "flagged": False,
                }
            )
    return pd.DataFrame(rows)


# @spec EVAL-POOL-001, EVAL-POOL-002
def test_pooling_table_is_long_with_distribution_and_raw_difference():
    frame = _pooled()
    table = diagnostics.pooling_regression(frame, seed=0)
    assert list(table.columns) == ["quantity", "estimate", "ci_low", "ci_high", "n"]
    t = table.set_index("quantity")
    assert list(t.index) == ["day", "day+hour_of_day", "raw_high_minus_low", "share_mean",
                             "share_p25", "share_p50", "share_p75", "share_zero"]
    share = frame["pooled_share_10km"]
    assert t.loc["share_mean", "estimate"] == pytest.approx(share.mean())
    assert t.loc["share_p75", "estimate"] == pytest.approx(share.quantile(0.75))
    assert t.loc["share_zero", "estimate"] == pytest.approx(0.5)
    high = share > share.median()
    raw = frame.loc[high, "error"].mean() - frame.loc[~high, "error"].mean()
    assert t.loc["raw_high_minus_low", "estimate"] == pytest.approx(raw)
    assert t.loc["raw_high_minus_low", "ci_low"] < raw < t.loc["raw_high_minus_low", "ci_high"]
    assert t.loc["day", "estimate"] == pytest.approx(3.0, abs=0.6)


# @spec EVAL-POOL-003
def test_pooling_answer_states_sign_size_and_leverage_without_calling_it_a_driver():
    table = pd.DataFrame(
        [
            ("day", 2.99, 1.43, 4.97, 4663),
            ("day+hour_of_day", 2.81, 1.31, 4.68, 4663),
            ("raw_high_minus_low", -0.31, -0.70, 0.05, 4663),
            ("share_mean", 0.030, np.nan, np.nan, 4663),
            ("share_p25", 0.0, np.nan, np.nan, 4663),
            ("share_p50", 0.017, np.nan, np.nan, 4663),
            ("share_p75", 0.047, np.nan, np.nan, 4663),
            ("share_zero", 0.40, np.nan, np.nan, 4663),
        ],
        columns=["quantity", "estimate", "ci_low", "ci_high", "n"],
    )
    text = report.pooling_answer(table)
    lower = text.lower()
    assert "driver" not in lower and "cause" not in lower
    assert "0.30" in text  # 2.99 per unit fraction is 0.30 per 10 percentage points
    assert "per 10 percentage points" in lower
    assert "opposite" in lower  # raw negative, within-day positive
    assert "leverage is low" in lower
    assert "40.0%" in text
    assert "0.14" in text  # 2.99 × (0.047 − 0) across the interquartile range


# --- Split-day shared offset ------------------------------------------------------------------


# @spec EVAL-UNC-005, EVAL-UNC-002
def test_shared_offset_is_estimated_on_one_half_of_the_days_and_scored_on_the_other():
    rng = np.random.default_rng(5)
    residuals, matchups = [], []
    for d in range(10):
        for h in range(12):
            hour = DAY0 + timedelta(days=d, hours=h)
            odd = (DAY0 + timedelta(days=d)).day % 2 == 1
            s = rng.normal(0, 2.0) if odd else 0.0
            for _ in range(4):
                residuals.append({"hour": hour, "r": s})
            for k in range(3):
                error = rng.normal(0, 1.0)
                matchups.append({"hour": hour, "site_id": f"airnow:{k}", "error": error,
                                 "z": error, "z_total": error, "z_total_raw": error,
                                 "inputs_reproduced": True, "flagged": False})
    frame = pd.DataFrame(matchups)
    table = diagnostics.coverage_diagnostics(
        frame, seed=0, shared_variance=1.0, residuals=pd.DataFrame(residuals)
    ).set_index("kind")
    assert list(table.index)[-2:] == ["z_total_shared_odd_on_even", "z_total_shared_even_on_odd"]
    odd_on_even, even_on_odd = (table.loc["z_total_shared_odd_on_even"],
                                table.loc["z_total_shared_even_on_odd"])
    assert odd_on_even["shared_variance"] == pytest.approx(4.0, rel=0.35)
    assert even_on_odd["shared_variance"] == 0.0
    assert odd_on_even["shared_hours"] == 60 and even_on_odd["shared_hours"] == 60
    is_odd = pd.to_datetime(frame["hour"]).dt.day % 2 == 1
    assert odd_on_even["n"] == (~is_odd).sum() and even_on_odd["n"] == is_odd.sum()
    # σ² from the even days is 0, so the odd days score as z_total alone
    assert even_on_odd["cov1"] == pytest.approx(np.mean(np.abs(frame.loc[is_odd, "error"]) <= 1))
    assert odd_on_even["cov1"] > np.mean(np.abs(frame.loc[~is_odd, "error"]) <= 1)
    for kind in table.index:
        assert table.loc[kind, "cov1_ci_low"] <= table.loc[kind, "cov1"]
        assert table.loc[kind, "cov2"] <= table.loc[kind, "cov2_ci_high"]


# --- Spike tiers ------------------------------------------------------------------------------


# @spec EVAL-SPIKE-005
def test_spike_tier_boundaries():
    assert [spikes.tier(z) for z in (20.1, 50.0, 50.1, 100.0, 100.5)] == [
        "20–50", "20–50", "50–100", "50–100", ">100"
    ]


# @spec EVAL-SPIKE-005
def test_tier_summary_scores_each_tier_with_only_its_points_removed():
    h1, h2, h3 = (DAY0 + timedelta(days=d) for d in range(3))
    points = pd.DataFrame(
        {"hour": [h1, h2, h3], "site_ids": ["a", "b", "c"], "z": [25.0, 150.0, 30.0],
         "median": 5.0, "mad": 1.0}
    )
    matchups = pd.DataFrame(
        {"hour": [h1, h1, h2, h2, h3], "site_id": ["m1", "m2", "m1", "m2", "m1"],
         "error": [1.0, 3.0, 8.0, 6.0, 2.0], "flagged": [False, False, False, True, False]}
    )
    rows = pd.DataFrame(
        {"tier": ["20–50", "20–50", ">100", ">100", "20–50"],
         "hour": [h1, h1, h2, h2, h3], "site_id": ["m1", "m2", "m1", "m2", "m1"],
         "delta": [-1.0, -1.0, -6.0, -5.0, -0.5]}
    )
    table = spikes.tier_summary(matchups, rows, points, seed=0).set_index("tier")
    assert list(table.index) == ["20–50", "50–100", ">100"]
    low = table.loc["20–50"]
    assert (low["points"], low["hours"], low["n"]) == (2, 2, 3)
    assert low["bias"] == pytest.approx(2.0) and low["bias_without"] == pytest.approx(1.1667, 1e-3)
    assert low["change"] == pytest.approx(low["bias_without"] - low["bias"])
    high = table.loc[">100"]
    assert (high["points"], high["hours"], high["n"]) == (1, 1, 1)  # the flagged row is out
    assert high["change"] == pytest.approx(-6.0)
    assert table.loc["50–100", "points"] == 0 and table.loc["50–100", "n"] == 0


# --- Timing alignment -------------------------------------------------------------------------


# @spec EVAL-LAG-002
def test_lag_difference_points_to_a_planted_one_hour_shift():
    rng = np.random.default_rng(6)
    obs_rows, matchups = [], []
    truth = {}
    for d in range(8):
        for h in range(24):
            hour = DAY0 + timedelta(days=d, hours=h)
            for k in range(4):
                truth[(k, hour)] = rng.uniform(2, 15)
    for (k, hour), value in truth.items():
        obs_rows.append({"site_id": f"airnow:{k}", "observed_at": hour, "pm25_raw": value,
                         "qc_flags": []})
        later = truth.get((k, hour + timedelta(hours=1)))
        if later is not None:  # the surface at H reads the monitor's H + 1
            matchups.append({"hour": hour, "site_id": f"airnow:{k}", "surface": later})
    table = diagnostics.lag_table(pd.DataFrame(matchups), pd.DataFrame(obs_rows), seed=0)
    t = table.set_index("lag")
    assert t.loc[1, "rmse"] == pytest.approx(0.0)
    assert t.loc[1, "rmse_difference_ci_high"] < 0
    assert np.isnan(t.loc[0, "rmse_difference"])
    assert np.isfinite(t.loc[-1, "rmse_difference"])


# @spec EVAL-LAG-002
def test_lag_lines_state_the_timestamp_convention():
    lag = pd.DataFrame(
        {"lag": [-1, 0, 1], "n": [10, 10, 10], "pearson_r": [0.6, 0.7, 0.71],
         "rmse": [2.5, 2.39, 2.36], "rmse_difference": [0.1, np.nan, -0.03],
         "rmse_difference_ci_low": [0.05, np.nan, -0.08],
         "rmse_difference_ci_high": [0.2, np.nan, 0.02]}
    )
    text = report.lag_lines(lag)
    assert "[H, H + 1 h)" in text and "no shift" in text
    assert "no lag fits better than lag 0" in text.lower()
    assert "misalignment" not in text.lower()
    lag.loc[2, "rmse_difference_ci_high"] = -0.01
    text = report.lag_lines(lag).lower()
    assert "lag +1 h fits better by 0.03" in text and "1.3% of the lag-0 rmse" in text
    assert "not a labelling error" in text and "misalignment" not in text


# --- Headline intervals -----------------------------------------------------------------------


# @spec EVAL-RUN-006
def test_headline_rows_carry_the_metric_table_intervals():
    metrics = pd.DataFrame(
        [
            {"subset": "unflagged", "split": "overall", "group": "all", "bias_ci_low": -0.5,
             "bias_ci_high": 0.3, "rmse_ci_low": 2.1, "rmse_ci_high": 2.7},
            {"subset": "all", "split": "overall", "group": "all", "bias_ci_low": -0.3,
             "bias_ci_high": 0.5, "rmse_ci_low": 2.1, "rmse_ci_high": 2.8},
        ]
    )
    headline = {"n": 10, "bias": -0.08, "mae": 1.76, "rmse": 2.39, "r2": 0.5, "pearson_r": 0.7,
                "pearson_r2": 0.49, "cov1_krig": 0.4, "cov2_krig": 0.66, "cov1_total": 0.46,
                "cov2_total": 0.76}
    unflagged, everything = report.headline_rows(metrics, headline, dict(headline))
    assert unflagged["rmse_ci_low"] == 2.1 and everything["bias_ci_high"] == 0.5
    row = report._metrics_row("unflagged", unflagged)
    assert "-0.08 [-0.50, 0.30]" in row and "2.39 [2.10, 2.70]" in row
