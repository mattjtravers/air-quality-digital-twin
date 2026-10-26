"""Intervals, conditional bias, coverage diagnostics, spikes, pooling, and the further
diagnostics: pure functions against constructed data."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from aqdt.evaluation import baselines, bootstrap, diagnostics, lomo, metrics, report, spikes
from aqdt.fusion.points import HourPoints

DAY0 = datetime(2026, 10, 1, tzinfo=UTC)


def _matchups(n_days=12, per_day=20, seed=1, noise=1.0, slope=1.0):
    """Synthetic matchups: true value t, surface = t (a perfect predictor of truth), obs = t + noise
    (monitor noise). ``slope`` scales the surface's departure from the hour mean."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in range(n_days):
        for k in range(per_day):
            hour = DAY0 + timedelta(days=d, hours=k % 24)
            t = rng.uniform(2, 16)
            surface = 9 + slope * (t - 9)
            obs = t + rng.normal(0, noise)
            sd = noise
            rows.append(
                {
                    "hour": hour,
                    "site_id": f"airnow:{k % 5}",
                    "obs": obs,
                    "surface": surface,
                    "error": surface - obs,
                    "z": (surface - obs) / sd,
                    "z_total": (surface - obs) / sd,
                    "z_total_raw": (surface - obs) / sd,
                    "inputs_reproduced": True,
                    "flagged": False,
                }
            )
    return pd.DataFrame(rows)


# --- Intervals --------------------------------------------------------------------------------


# @spec EVAL-METRIC-001
def test_pearson_r2_is_the_squared_correlation_and_differs_from_skill_r2():
    frame = _matchups()
    frame["surface"] = frame["surface"] * 1.5 + 2  # biased and over-dispersed: R² < r²
    frame["error"] = frame["surface"] - frame["obs"]
    m = metrics.matchup_metrics(frame)
    assert m["pearson_r2"] == pytest.approx(m["pearson_r"] ** 2)
    assert m["r2"] < m["pearson_r2"]
    assert metrics.METRICS.index("pearson_r2") == metrics.METRICS.index("r2") + 1


# @spec EVAL-METRIC-002
def test_raw_total_coverage_is_computed_from_z_total_raw():
    frame = _matchups()
    frame["z_total_raw"] = frame["z_total"] * 3  # an uninflated, smaller variance: larger |z|
    m = metrics.matchup_metrics(frame)
    assert m["cov1_total_raw"] < m["cov1_total"]
    assert m["cov2_total_raw"] == pytest.approx((frame["z_total_raw"].abs() <= 2).mean())


# @spec EVAL-METRIC-003, EVAL-METRIC-004
def test_metric_intervals_resample_days_and_match_the_generic_block_bootstrap():
    frame = _matchups()
    ci = bootstrap.metric_intervals(frame, seed=4, n=60)
    blocks = bootstrap.days(frame["hour"])
    for name in ("bias", "rmse", "r2", "pearson_r", "cov1_krig"):
        generic = bootstrap.block_interval(
            blocks, lambda idx, name=name: metrics.matchup_metrics(frame.iloc[idx])[name], 4, 60
        )
        assert (ci[f"{name}_ci_low"], ci[f"{name}_ci_high"]) == pytest.approx(generic, rel=1e-9)
    point = metrics.matchup_metrics(frame)
    assert ci["bias_ci_low"] <= point["bias"] <= ci["bias_ci_high"]
    assert ci == bootstrap.metric_intervals(frame, seed=4, n=60)
    assert "n_ci_low" not in ci


# @spec EVAL-METRIC-003
def test_intervals_are_null_for_a_single_day():
    frame = _matchups(n_days=1)
    ci = bootstrap.metric_intervals(frame, seed=0, n=50)
    assert all(np.isnan(v) for v in ci.values())
    lo, hi = bootstrap.block_interval(bootstrap.days(frame["hour"]), lambda idx: 1.0, 0, 50)
    assert np.isnan(lo) and np.isnan(hi)


# @spec EVAL-SPLIT-002, EVAL-SPLIT-003, EVAL-SPLIT-004
def test_metric_table_adds_the_new_splits_and_interval_columns():
    frame = _matchups(n_days=3, per_day=30)
    hours = sorted(frame["hour"].unique())
    frame["hour_mean_points_band"] = "[1.00, 2.00]"
    frame["hour_mean_obs_band"] = "[1.00, 2.00]"
    frame["spike_hour"] = np.where(frame["hour"] == hours[0], "affected", "other")
    frame["spike_day"] = "other"
    frame["variogram"] = "regular"
    bands = pd.Series("[0.30, 0.40]", index=hours)
    table = metrics.metric_table(frame, bands, seed=0)
    assert set(table["split"]) == {
        "overall",
        "level",
        "pred_level",
        "hour_mean_points",
        "hour_mean_obs",
        "hour_of_day",
        "day",
        "pooled_band",
        "spike_hour",
        "spike_day",
        "variogram",
        "monitor",
    }
    pred = table[(table["subset"] == "unflagged") & (table["split"] == "pred_level")]
    assert len(pred) == 3 and pred["n"].sum() == len(frame)
    monitors = table[(table["subset"] == "all") & (table["split"] == "monitor")]
    assert set(monitors["group"]) == set(frame["site_id"])
    columns = list(table.columns)
    assert columns.index("pearson_r2") == columns.index("r2") + 1
    for name in ("bias", "rmse", "r2", "pearson_r2", "cov2_total_raw"):
        assert f"{name}_ci_low" in columns and f"{name}_ci_high" in columns
    days = table[table["split"] == "day"]
    assert days["bias_ci_low"].isna().all()
    overall = table[table["split"] == "overall"]
    assert overall["bias_ci_low"].notna().all()


# @spec EVAL-SPLIT-003
def test_tertile_bands_generalise_the_pooled_bands():
    values = pd.Series(np.arange(9, dtype=float))
    assert metrics.tertile_bands(values).equals(metrics.pooled_bands(values))
    assert metrics.tertile_bands(values).nunique() == 3


# --- Conditional bias -------------------------------------------------------------------------


# @spec EVAL-COND-001, EVAL-COND-002, EVAL-COND-003
def test_a_calibrated_surface_shows_bias_by_observed_level_but_not_by_predicted_level():
    frame = _matchups(n_days=20, per_day=40, noise=2.0)
    by_obs = frame.groupby(frame["obs"].map(metrics.level_group))["error"].mean()
    assert by_obs["<5"] > 0.3 and by_obs[">9"] < -0.3  # the mechanical pattern

    slopes = diagnostics.slopes(frame, seed=0).set_index("slope")
    lo, hi = slopes.loc["obs_on_surface", ["ci_low", "ci_high"]]
    assert lo < 1 < hi
    assert slopes.loc["surface_on_obs", "estimate"] < 1
    assert diagnostics.smoothing_verdict(lo, hi) == "not distinguishable from calibrated"

    table = diagnostics.reliability(frame, seed=0)
    assert list(table.columns) == [
        "bin", "n", "surface_mean", "obs_mean", "obs_ci_low", "obs_ci_high"
    ]
    assert len(table) == 10 and table["n"].sum() == len(frame)
    assert np.allclose(table["obs_mean"], table["surface_mean"], atol=1.0)


# @spec EVAL-COND-003
def test_smoothing_verdicts():
    smooth = _matchups(n_days=20, per_day=40, slope=0.5)
    slopes = diagnostics.slopes(smooth, seed=0).set_index("slope")
    lo, hi = slopes.loc["obs_on_surface", ["ci_low", "ci_high"]]
    assert diagnostics.smoothing_verdict(lo, hi) == "over-smoothed"
    assert diagnostics.smoothing_verdict(0.6, 0.9) == "over-dispersed"
    assert diagnostics.clean_dirty_holds(0.2, -0.1) is True
    assert diagnostics.clean_dirty_holds(-0.1, -0.5) is False
    assert diagnostics.clean_dirty_holds(0.2, 0.1) is False


# --- Coverage diagnostics ---------------------------------------------------------------------


# @spec EVAL-UNC-002
def test_inflation_factor_restores_nominal_coverage():
    rng = np.random.default_rng(0)
    z = rng.normal(0, 2.0, 200_000)  # variance understated four-fold
    k68, k95 = diagnostics.inflation_factors(z)
    assert k68 == pytest.approx(4.0, rel=0.02) and k95 == pytest.approx(4.0, rel=0.02)
    assert np.mean(np.abs(z / np.sqrt(k68)) <= 1) == pytest.approx(0.683, abs=0.002)


# @spec EVAL-UNC-002, EVAL-UNC-004
def test_coverage_diagnostics_has_one_row_per_z_kind():
    frame = _matchups()
    frame["z_total_raw"] = frame["z"] * 2
    table = diagnostics.coverage_diagnostics(frame, seed=0, shared_variance=0.5)
    assert list(table["kind"]) == ["z", "z_total_raw", "z_total", "z_total_shared"]
    for column in ("n", "cov1", "cov2", "k68", "k68_ci_low", "k68_ci_high", "k95", "mean", "sd"):
        assert column in table.columns
    row = table.set_index("kind")
    assert row.loc["z_total_raw", "k68"] == pytest.approx(4 * row.loc["z", "k68"])
    assert row.loc["z_total_shared", "cov1"] >= row.loc["z_total", "cov1"]


# @spec EVAL-UNC-003
def test_shared_offset_removes_the_independent_part():
    rng = np.random.default_rng(5)
    hours, rows = 400, []
    for h in range(hours):
        s = rng.normal(0, 1.0)  # shared, variance 1
        for r in s + rng.normal(0, 2.0, 8):  # independent, variance 4, n = 8
            rows.append({"hour": h, "r": r})
    variance, n = diagnostics.shared_offset(pd.DataFrame(rows))
    assert n == hours
    assert variance == pytest.approx(1.0, abs=0.25)
    single = pd.DataFrame({"hour": [0, 1, 1], "r": [5.0, 1.0, 1.0]})
    assert diagnostics.shared_offset(single)[1] == 1  # hours with one sensor are skipped
    flat = pd.DataFrame({"hour": [0, 0, 1, 1], "r": [1.0, -1.0, 1.0, -1.0]})
    assert diagnostics.shared_offset(flat)[0] == 0.0


# --- Leave one monitor out --------------------------------------------------------------------


# @spec EVAL-LOMO-004
def test_fitted_sensors_are_counted_by_where_they_go_without_the_monitor():
    control = pd.DataFrame(
        {
            "status": ["fitted", "fitted", "fitted", "fitted", "pooled"],
            "ref_site_id": ["X", "X", "X", "Y", None],
        }
    )
    without = pd.DataFrame(
        {
            "status": ["pooled", "fitted", None, "fitted", "pooled"],
            "ref_site_id": [None, "Z", None, "Y", None],
        }
    )
    assert lomo.fitted_moves(control, without, "X") == (1, 1, 1)


def _lomo_table():
    rows = []
    rng = np.random.default_rng(2)
    hours = pd.date_range("2026-10-01", periods=8, freq="13h", tz="UTC")
    for h in hours:
        for k, n_fit in enumerate((0, 3, 9)):
            control = rng.normal(0, 1)
            d = 0.0 if n_fit == 0 else 0.1 * n_fit
            rows.append(
                {
                    "hour": h,
                    "site_id": f"airnow:{k}",
                    "obs": 8.0,
                    "flagged": False,
                    "stored_error": control + 0.01,
                    "control_error": control,
                    "lomo_error": np.sign(control or 1) * (abs(control) + d),
                    "n_fitted_to_x": n_fit,
                }
            )
    return pd.DataFrame(rows)


# @spec EVAL-LOMO-005, EVAL-LOMO-006, EVAL-LOMO-007
def test_lomo_statistics_report_sanity_intervals_and_dose_response():
    table = _lomo_table()
    stats = lomo.lomo_statistics(table, seed=0)
    assert stats["sanity_median"] == pytest.approx(0.01)
    assert stats["sanity_max"] == pytest.approx(0.01)
    assert stats["sanity_rows"] == []
    assert stats["median_d"] == pytest.approx(np.median([0.0, 0.3, 0.9] * 8))
    assert stats["median_d_ci_low"] <= stats["median_d"] <= stats["median_d_ci_high"]
    assert stats["mean_d_ci_low"] <= stats["mean_d"] <= stats["mean_d_ci_high"]
    assert stats["share_worse"] == pytest.approx(2 / 3)
    bands = stats["bands"].set_index("band")
    assert list(bands.index) == ["0", "1–5", ">5"]
    assert bands.loc["0", "median"] == pytest.approx(0.0)
    assert bands.loc[">5", "median"] == pytest.approx(0.9)
    assert stats["dose_rises"] is True and stats["dose_separates"] is True
    assert stats["empty_bands"] == [] and stats["dose_compared"] == ("0", ">5")
    no_zero = lomo.lomo_statistics(table[table["n_fitted_to_x"] > 0], seed=0)
    assert no_zero["empty_bands"] == ["0"] and no_zero["dose_compared"] == ("1–5", ">5")
    by_monitor = lomo.lomo_by_monitor(table)
    assert list(by_monitor.columns) == ["site_id", "n", "median_d", "mean_n_fitted_to_x"]
    assert by_monitor.set_index("site_id").loc["airnow:2", "median_d"] == pytest.approx(0.9)


# @spec EVAL-LOMO-005
def test_lomo_sanity_lists_rows_far_from_the_stored_surface():
    table = _lomo_table()
    table.loc[0, "stored_error"] = table.loc[0, "control_error"] + 2.0
    stats = lomo.lomo_statistics(table, seed=0)
    assert stats["sanity_max"] == pytest.approx(2.0)
    assert stats["sanity_rows"] == [(table.loc[0, "hour"], table.loc[0, "site_id"])]


# @spec EVAL-LOMO-007
def test_dose_bands():
    assert [lomo.dose_band(n) for n in (0, 1, 5, 6, 40)] == ["0", "1–5", "1–5", ">5", ">5"]


# --- Baselines --------------------------------------------------------------------------------


# @spec EVAL-BASE-004
def test_hour_level_baseline_is_the_points_mean_and_median():
    assert baselines.hour_level(np.array([1.0, 2.0, 9.0])) == pytest.approx((4.0, 2.0))


def _baseline_frame():
    frame = _matchups(n_days=6, per_day=30)
    rng = np.random.default_rng(3)
    frame["idw"] = frame["surface"] + rng.normal(0, 1.0, len(frame))
    frame["rf"] = frame["surface"] + rng.normal(0, 0.5, len(frame))
    hourly = frame.groupby("hour")["surface"].transform("mean")
    frame["hour_mean"] = hourly
    frame["hour_median"] = frame.groupby("hour")["surface"].transform("median")
    return frame


# @spec EVAL-BASE-003
def test_baseline_table_has_splits_pearson_r2_and_intervals():
    table = baselines.baseline_table(_baseline_frame(), seed=0)
    assert list(table.columns[:4]) == ["method", "subset", "split", "group"]
    assert set(table["method"]) == {"kriging", "hour_mean", "hour_median", "idw", "rf"}
    assert set(table["split"]) == {"overall", "level", "pred_level"}
    assert {"pearson_r2", "rmse_ci_low", "rmse_ci_high"} <= set(table.columns)
    pred = table[(table["split"] == "pred_level") & (table["subset"] == "unflagged")]
    groups = {m: set(g["group"]) for m, g in pred.groupby("method")}
    assert len({frozenset(v) for v in groups.values()}) == 1  # same bands for every method


# @spec EVAL-BASE-005
def test_paired_comparison_is_negative_when_kriging_is_closer():
    frame = _baseline_frame()
    paired = baselines.paired_table(frame, seed=0)
    assert list(paired.columns) == [
        "baseline", "split", "group", "n", "mean_difference", "ci_low", "ci_high"
    ]
    assert set(paired["baseline"]) == {"hour_mean", "hour_median", "idw", "rf"}
    overall = paired[paired["split"] == "overall"].set_index("baseline")
    expected = (
        (frame["surface"] - frame["obs"]).abs() - (frame["idw"] - frame["obs"]).abs()
    ).mean()
    assert overall.loc["idw", "mean_difference"] == pytest.approx(expected)
    assert overall.loc["idw", "ci_high"] < 0  # IDW here is the surface plus noise


# --- Spikes -----------------------------------------------------------------------------------


# @spec EVAL-SPIKE-001
def test_spike_rule_needs_both_the_robust_excess_and_the_floor():
    rng = np.random.default_rng(0)
    z = rng.normal(8, 1.0, 60)
    z[3] = 150.0
    assert np.flatnonzero(spikes.spike_mask(z)).tolist() == [3]
    clean = np.full(40, 2.0) + rng.normal(0, 0.05, 40)
    clean[0] = 9.0  # far above a tiny MAD, but below the 20 µg/m³ floor
    assert not spikes.spike_mask(clean).any()
    ordinary = rng.normal(25, 4.0, 60)  # above the floor, within the robust band
    assert not spikes.spike_mask(ordinary).any()


def _points(z):
    n = len(z)
    x = np.linspace(300_000, 340_000, n)
    y = np.linspace(4_290_000, 4_330_000, n)[::-1]
    return HourPoints(x=x, y=y, z=np.asarray(z, float), n_sensors=n, n_points=n)


# @spec EVAL-SPIKE-001
def test_spike_points_name_the_sensors_at_the_point():
    hour = DAY0
    z = np.full(12, 6.0)
    z[5] = 120.0
    points = _points(z)
    sensors = pd.DataFrame(
        {
            "site_id": [f"purpleair:{k}" for k in range(12)],
            "x": points.x,
            "y": points.y,
            "pm25_calibrated": z,
        }
    )
    table = spikes.spike_points(hour, sensors, points)
    assert list(table.columns) == ["hour", "site_ids", "z", "median", "mad"]
    assert table["site_ids"].tolist() == ["purpleair:5"]
    assert table["z"].iloc[0] == pytest.approx(120.0)


# @spec EVAL-SPIKE-004
def test_spike_summary_scores_affected_hours_both_ways():
    hours = [DAY0, DAY0 + timedelta(hours=1), DAY0 + timedelta(days=1)]
    matchups = pd.DataFrame(
        {
            "hour": [hours[0], hours[0], hours[1], hours[2]],
            "site_id": ["a", "b", "a", "a"],
            "obs": 5.0,
            "surface": [9.0, 8.0, 6.0, 5.5],
            "error": [4.0, 3.0, 1.0, 0.5],
            "z": np.nan,
            "z_total": np.nan,
            "inputs_reproduced": True,
            "flagged": False,
        }
    )
    rows = pd.DataFrame({"hour": [hours[0], hours[0]], "site_id": ["a", "b"],
                         "delta": [-4.0, -2.0]})
    points = pd.DataFrame({"hour": [hours[0]]})
    summary = spikes.spike_summary(matchups, rows, points, {hours[0]})
    hour = summary["hour_table"].iloc[0]
    assert hour["bias"] == pytest.approx(3.5) and hour["bias_without"] == pytest.approx(0.5)
    assert hour["rmse_without"] == pytest.approx(np.sqrt(0.5))
    day = summary["days"].iloc[0]
    assert day["day"] == "2026-10-01"
    assert day["rmse"] == pytest.approx(np.sqrt((16 + 9 + 1) / 3))
    assert day["rmse_without"] == pytest.approx(np.sqrt((0 + 1 + 1) / 3))
    assert summary["headline_all"]["n"] == 4 and summary["headline_unaffected"]["n"] == 2
    assert summary["points"] == 1 and summary["hours"] == 1


# @spec EVAL-SPIKE-003
def test_rekrige_without_spikes_moves_the_prediction_down():
    rng = np.random.default_rng(1)
    z = 6.0 + rng.normal(0, 0.5, 25)
    z[12] = 150.0
    points = _points(z)
    mask = spikes.spike_mask(points.z)
    reduced = spikes.without(points, mask)
    assert reduced.n_points == 24 and 150.0 not in reduced.z
    monitors = pd.DataFrame(
        {"site_id": ["airnow:1"], "x": [points.x[12] + 500], "y": [points.y[12]]}
    )
    scored = pd.DataFrame(
        {"site_id": ["airnow:1"], "obs": [6.0], "flagged": [False], "error": [20.0]}
    )
    rows = spikes.rekrige(DAY0, points, mask, monitors, scored, min_points=5)
    assert list(rows.columns) == spikes.COLUMNS
    row = rows.iloc[0]
    assert row["delta"] == pytest.approx(row["without_error"] - row["control_error"])
    assert row["delta"] < 0
    assert abs(row["without_error"]) < 3


# --- Further diagnostics ----------------------------------------------------------------------


# @spec EVAL-POOL-001
def test_pooling_regression_recovers_a_planted_effect_with_day_effects():
    rng = np.random.default_rng(9)
    rows = []
    for d in range(30):
        day_shift = rng.normal(0, 3)
        for k in range(40):
            share = rng.uniform(0, 1)
            rows.append(
                {
                    "hour": DAY0 + timedelta(days=d, hours=k % 24),
                    "pooled_share_10km": share + 0.5 * (day_shift > 0),  # confounded with day
                    "error": day_shift + 1.5 * share + rng.normal(0, 0.5),
                    "flagged": False,
                }
            )
    table = diagnostics.pooling_regression(pd.DataFrame(rows), seed=0).set_index("model")
    assert list(table.index) == ["day", "day+hour_of_day"]
    for model in table.index:
        assert table.loc[model, "coefficient"] == pytest.approx(1.5, abs=0.15)
        assert table.loc[model, "ci_low"] < table.loc[model, "coefficient"]
        assert table.loc[model, "coefficient"] < table.loc[model, "ci_high"]
        assert table.loc[model, "ci_high"] - table.loc[model, "ci_low"] < 0.5
        assert table.loc[model, "n"] == 1200


# @spec EVAL-VARIO-001
def test_variogram_classes_and_table():
    assert diagnostics.variogram_class("spherical", 2.0, 1.995, 9000) == "pure_nugget"
    assert diagnostics.variogram_class("exponential", 2.0, 0.5, 800) == "short_range"
    assert diagnostics.variogram_class("spherical", 2.0, 0.5, 800) == "regular"
    assert diagnostics.variogram_class("exponential", 2.0, 0.5, 5000) == "regular"
    fits = pd.DataFrame(
        {
            "variogram_model": ["spherical", "exponential", "exponential", "spherical"],
            "sill": [2.0, 2.0, 2.0, 2.0],
            "nugget": [2.0, 0.5, 0.5, 0.5],
            "range_m": [9000.0, 800.0, 5000.0, 7000.0],
        }
    )
    table = diagnostics.variogram_table(fits)
    assert list(table.columns) == ["quantity", "name", "value"]
    lookup = table.set_index(["quantity", "name"])["value"]
    assert lookup[("class_count", "pure_nugget")] == 1
    assert lookup[("class_share", "regular")] == pytest.approx(0.5)
    assert lookup[("model_count", "exponential")] == 2
    assert lookup[("range_m_percentile", "p50")] == pytest.approx(6000.0)


# @spec EVAL-LAG-001
def test_lag_table_pairs_the_surface_with_neighbouring_hours():
    hours = pd.date_range("2026-10-01", periods=6, freq="h", tz="UTC")
    obs = pd.DataFrame(
        {
            "site_id": "airnow:1",
            "observed_at": hours,
            "pm25_raw": np.arange(6, dtype=float),
            "qc_flags": [[] for _ in hours],
        }
    )
    matchups = pd.DataFrame(
        {"hour": hours[1:5], "site_id": "airnow:1", "surface": np.arange(2, 6, dtype=float)}
    )
    table = diagnostics.lag_table(matchups, obs).set_index("lag")
    assert list(table.index) == [-1, 0, 1]
    assert table.loc[1, "rmse"] == pytest.approx(0.0)  # surface(H) == obs(H + 1)
    assert table.loc[0, "rmse"] == pytest.approx(1.0)
    assert table.loc[0, "n"] == 4


# @spec EVAL-FLAG-001
def test_flag_rates_name_both_denominators():
    matchups = pd.DataFrame({"flagged": [True, False, False, False]})
    observations = pd.DataFrame({"qc_flags": [["flatline"], [], [], ["missing_value"], []]})
    rates = diagnostics.flag_rates(matchups, observations)
    assert rates == {
        "observations_flagged": 2,
        "observations": 5,
        "matchups_flagged": 1,
        "matchups": 4,
    }


# @spec EVAL-MON-001
def test_per_monitor_table_joins_accuracy_and_availability():
    frame = _matchups(n_days=2, per_day=10)
    frame["nearest_sensor_km"] = 2.0
    frame.loc[0, "flagged"] = True
    availability = pd.DataFrame(
        {
            "site_id": [f"airnow:{k}" for k in range(5)],
            "calibration_targets": [3, 0, 1, 2, 5],
            "median_target_distance_m": [4000.0, np.nan, 1000.0, 2500.0, 8000.0],
        }
    )
    table = diagnostics.per_monitor(frame, availability)
    assert {"site_id", "n", "bias", "mae", "rmse", "pearson_r", "cov1_krig", "cov2_total",
            "flagged_share", "calibration_targets", "median_target_distance_m",
            "mean_nearest_sensor_km"} <= set(table.columns)
    row = table.set_index("site_id").loc[frame.loc[0, "site_id"]]
    assert row["flagged_share"] == pytest.approx(1 / 4)
    assert row["n"] == 3


# --- Provenance -------------------------------------------------------------------------------


# @spec EVAL-RUN-003
def test_airnow_provenance_from_object_listing():
    from aqdt.observation_store.archive import ObjectInfo

    t1 = datetime(2026, 10, 2, 3, tzinfo=UTC)
    t2 = datetime(2026, 10, 5, 4, tzinfo=UTC)
    objects = [
        ObjectInfo(key="source=airnow/date=2026-10-02/o.parquet", size=10, etag='"a"',
                   last_modified=t2),
        ObjectInfo(key="source=airnow/date=2026-10-01/o.parquet", size=12, etag='"b"',
                   last_modified=t1),
    ]
    meta = report.airnow_provenance(objects)
    assert meta["airnow_objects"] == 2
    assert meta["airnow_last_modified"] == t2.isoformat()
    assert meta["airnow_manifest_sha256"] == report.airnow_provenance(objects[::-1])[
        "airnow_manifest_sha256"
    ]
    changed = [objects[0], ObjectInfo(objects[1].key, 12, '"c"', t1)]
    assert report.airnow_provenance(changed)["airnow_manifest_sha256"] != meta[
        "airnow_manifest_sha256"
    ]
    local = [ObjectInfo("k", 3, None, None)]
    assert report.airnow_provenance(local)["airnow_last_modified"] is None
    assert report.airnow_provenance([])["airnow_objects"] == 0


# @spec EVAL-RUN-003
def test_git_state_reports_dirty_tree_and_diff_hash(tmp_path):
    import subprocess

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "a.txt").write_text("one\n")
    git("add", "a.txt")
    git("commit", "-qm", "c")
    clean = report.git_state(tmp_path)
    assert clean["git_dirty"] is False and clean["git_diff_sha256"] is None
    assert len(clean["git_commit"]) == 40
    (tmp_path / "a.txt").write_text("two\n")
    dirty = report.git_state(tmp_path)
    assert dirty["git_dirty"] is True and len(dirty["git_diff_sha256"]) == 64
    outside = report.git_state(tmp_path / "missing")
    assert outside == {"git_commit": None, "git_dirty": None, "git_diff_sha256": None}


# @spec EVAL-FIG-004
def test_map_hours_skip_degenerate_variograms_when_regular_hours_exist():
    from types import SimpleNamespace

    from aqdt.evaluation import run as run_module

    hours = pd.date_range("2026-10-01", periods=8, freq="25h", tz="UTC")

    def context(hour, nugget):
        fit = SimpleNamespace(variogram_model="spherical", sill=2.0, nugget=nugget, range_m=9000.0)
        return SimpleNamespace(hour=hour, fit=fit)

    contexts = [context(h, 2.0 if k % 2 == 0 else 0.5) for k, h in enumerate(hours)]
    matchups = pd.DataFrame({"hour": hours, "obs": np.arange(8, dtype=float)})
    chosen = run_module._select_map_hours(contexts, matchups)
    assert len(chosen) == 4
    assert all(c.fit.nugget == 0.5 for c in chosen)
    degenerate = [context(h, 2.0) for h in hours]
    assert len(run_module._select_map_hours(degenerate, matchups)) == 4
