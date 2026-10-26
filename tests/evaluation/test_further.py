"""Leave one monitor out and the baselines: pure functions against hand-computed values."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from aqdt.evaluation import baselines, lomo, montecarlo

HOUR = datetime(2026, 9, 14, 10, tzinfo=UTC)
AS_OF = datetime(2026, 9, 14, tzinfo=UTC)

# --- Leave one monitor out --------------------------------------------------------------------


# @spec EVAL-LOMO-001
def test_lomo_hours_are_selected_as_the_monte_carlo_hours_are():
    hours = pd.date_range("2026-09-01", periods=60, freq="7h", tz="UTC")
    means = pd.Series(np.linspace(2, 14, len(hours)), index=hours)
    for n in (1, 6, 59):
        assert lomo.select_lomo_hours(means, n, seed=3) == montecarlo.select_mc_hours(
            means, n, seed=3
        )
    assert lomo.select_lomo_hours(means, 100, seed=3) == sorted(hours)
    assert lomo.select_lomo_hours(means, 0, seed=3) == []


def _calibrated(rows):
    return pd.DataFrame(
        rows,
        columns=["site_id", "hour", "pm25_calibrated", "pm25_corrected_mean", "fit_as_of",
                 "fit_status"],
    )


def _fits(rows):
    return pd.DataFrame(rows, columns=["site_id", "as_of", "status", "slope", "intercept"])


# @spec EVAL-LOMO-003
def test_recalibration_uses_the_refit_of_each_rows_own_fit():
    older = AS_OF - timedelta(days=1)
    calibrated = _calibrated(
        [
            ("purpleair:1", HOUR, 9.0, 10.0, AS_OF, "fitted"),
            ("purpleair:2", HOUR, 5.0, 4.0, older, "pooled"),
            ("purpleair:3", HOUR, 1.0, 1.0, AS_OF, "pooled"),
            ("purpleair:4", HOUR, 7.0, 6.0, AS_OF, "fitted"),
            ("purpleair:5", HOUR, None, 6.0, AS_OF, "uncalibrated"),
        ]
    )
    fits = _fits(
        [
            ("purpleair:1", AS_OF, "fitted", 0.5, 1.0),
            ("purpleair:2", AS_OF, "fitted", 9.0, 9.0),  # not the row's fit_as_of
            ("purpleair:2", older, "pooled", 2.0, 0.0),
            ("purpleair:3", AS_OF, "pooled", 1.0, -3.0),  # clamps at 0
            ("purpleair:5", AS_OF, "uncalibrated", None, None),
            # purpleair:4 has no refit
        ]
    )
    out = lomo.recalibrate(calibrated, fits).set_index("site_id")
    assert out.loc["purpleair:1", "pm25_calibrated"] == pytest.approx(6.0)
    assert out.loc["purpleair:1", "fit_status"] == "fitted"
    assert out.loc["purpleair:2", "pm25_calibrated"] == pytest.approx(8.0)
    assert out.loc["purpleair:2", "fit_status"] == "pooled"
    assert out.loc["purpleair:3", "pm25_calibrated"] == 0.0
    for missing in ("purpleair:4", "purpleair:5"):
        assert pd.isna(out.loc[missing, "pm25_calibrated"])
        assert out.loc[missing, "fit_status"] == "uncalibrated"
    assert out.loc["purpleair:1", "pm25_corrected_mean"] == 10.0  # inputs carried through


# @spec EVAL-LOMO-003
def test_prediction_is_null_below_min_points_and_clamped_at_zero():
    from aqdt.fusion.points import HourPoints

    few = HourPoints(
        x=np.array([0.0, 1000.0]),
        y=np.array([0.0, 0.0]),
        z=np.array([1.0, 2.0]),
        n_sensors=2,
        n_points=2,
    )
    assert lomo.predict_at(few, 500.0, 0.0, min_points=10) is None

    rng = np.random.default_rng(0)
    n = 40
    x, y = rng.uniform(0, 40_000, n), rng.uniform(0, 40_000, n)
    z = -2.0 + 1e-4 * x + rng.normal(0, 0.3, n)  # an estimate below 0 everywhere near x = 0
    points = HourPoints(x=x, y=y, z=z, n_sensors=n, n_points=n)
    assert lomo.predict_at(points, 0.0, 20_000.0, min_points=10) == 0.0
    assert lomo.predict_at(points, 39_000.0, 20_000.0, min_points=10) > 0.0


# --- Baselines --------------------------------------------------------------------------------


# @spec EVAL-BASE-001
def test_idw_is_power_two_and_takes_coincident_points_exactly():
    px = np.array([0.0, 2.0, 0.0])
    py = np.array([0.0, 0.0, 4.0])
    pz = np.array([1.0, 3.0, 5.0])
    w = 1 / np.array([1.0, 1.0, 17.0])  # 1 / squared distance to (1, 0)
    expected = (w * pz).sum() / w.sum()
    assert baselines.idw(px, py, pz, 1.0, 0.0) == pytest.approx(expected)
    twin_x, twin_y = np.array([0.0, 0.0, 5.0]), np.array([0.0, 0.0, 5.0])
    assert baselines.idw(twin_x, twin_y, np.array([2.0, 4.0, 100.0]), 0.0, 0.0) == 3.0


def _training(n_hours=6, n_points=25, seed=1):
    rng = np.random.default_rng(seed)
    rows = []
    for h in range(n_hours):
        hour = HOUR + timedelta(hours=h)
        x, y = rng.uniform(0, 50_000, n_points), rng.uniform(0, 50_000, n_points)
        z = 5 + h + 1e-4 * x + rng.normal(0, 0.2, n_points)
        rows.append(pd.DataFrame({"hour": hour, "x": x, "y": y, "z": z}))
    return pd.concat(rows, ignore_index=True)


# @spec EVAL-BASE-002
def test_random_forest_is_seeded_with_the_specified_settings():
    points = _training()
    model = baselines.fit_forest(points, seed=4)
    params = model.get_params()
    assert (params["n_estimators"], params["min_samples_leaf"], params["random_state"]) == (
        200,
        5,
        4,
    )
    assert list(model.feature_names_in_) == baselines.FEATURES
    query = pd.DataFrame({"hour": [HOUR, HOUR + timedelta(hours=5)], "x": [1e4, 1e4],
                          "y": [2e4, 2e4]})
    first = baselines.predict_forest(model, points, query)
    again = baselines.predict_forest(baselines.fit_forest(points, seed=4), points, query)
    assert np.array_equal(first, again)
    assert first[1] > first[0]  # the hour's median point value carries the level


# @spec EVAL-BASE-002
def test_forest_features_are_location_hour_of_day_and_the_hours_median():
    points = _training(n_hours=2)
    frame = baselines.features(points, points)
    assert list(frame.columns) == baselines.FEATURES == ["x", "y", "hour_of_day", "median"]
    first = points["hour"] == HOUR
    assert (frame.loc[first, "median"] == points.loc[first, "z"].median()).all()
    assert (frame.loc[first, "hour_of_day"] == 10).all()


# @spec EVAL-BASE-003
def test_baseline_table_scores_each_method_over_both_subsets():
    m = pd.DataFrame(
        {
            "obs": [4.0, 6.0, 8.0, 10.0],
            "surface": [5.0, 6.0, 7.0, 10.0],
            "idw": [4.0, 7.0, 8.0, 12.0],
            "rf": [6.0, 6.0, 6.0, 6.0],
            "flagged": [False, False, False, True],
        }
    )
    table = baselines.baseline_table(m)
    assert list(table.columns[:2]) == ["method", "subset"]
    assert set(zip(table["method"], table["subset"])) == {
        (method, subset)
        for method in ("kriging", "idw", "rf")
        for subset in ("unflagged", "all")
    }
    row = table.set_index(["method", "subset"]).loc[("idw", "unflagged")]
    assert row["n"] == 3
    assert row["bias"] == pytest.approx(1 / 3)
    assert row["rmse"] == pytest.approx(np.sqrt(1 / 3))
    kriging = table.set_index(["method", "subset"]).loc[("kriging", "all")]
    assert kriging["mae"] == pytest.approx(0.5)
