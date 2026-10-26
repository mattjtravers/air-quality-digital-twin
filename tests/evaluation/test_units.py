"""Evaluation's pure functions against hand-computed values — matchup cells, metrics, splits, fit
covariance, kriging weights, the calibration covariance, Monte Carlo, decomposition, budget."""

from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest
from affine import Affine
from pydantic import ValidationError
from pykrige.ok import OrdinaryKriging

from aqdt.evaluation import budget, calse, decompose, matchups, metrics, montecarlo, totvar
from aqdt.evaluation.schemas import EvaluationSettings

# --- Matchup cells ----------------------------------------------------------------------------


# @spec EVAL-MATCH-002
def test_cell_index_is_half_open():
    transform = Affine(100.0, 0.0, 1000.0, 0.0, -100.0, 5000.0)  # 3 x 2 grid
    width, height = 3, 2
    assert matchups.cell_index(transform, width, height, 1000.0, 5000.0) == (0, 0)
    assert matchups.cell_index(transform, width, height, 1150.0, 4950.0) == (0, 1)
    assert matchups.cell_index(transform, width, height, 1100.0, 4900.0) == (1, 1)
    assert matchups.cell_index(transform, width, height, 1299.9, 4800.1) == (1, 2)
    # east edge x = 1300 and south edge y = 4800 are outside; so is anything beyond the box
    assert matchups.cell_index(transform, width, height, 1300.0, 4900.0) is None
    assert matchups.cell_index(transform, width, height, 1100.0, 4800.0) is None
    assert matchups.cell_index(transform, width, height, 999.0, 4900.0) is None
    assert matchups.cell_index(transform, width, height, 1100.0, 5000.1) is None


# --- Metrics ----------------------------------------------------------------------------------


def _frame(obs, surface, sd, total_sd=None, reproduced=None):
    obs, surface, sd = (np.asarray(v, dtype=float) for v in (obs, surface, sd))
    error = surface - obs
    total_sd = sd if total_sd is None else np.asarray(total_sd, dtype=float)
    z = np.where(sd > 0, error / np.where(sd > 0, sd, 1), np.nan)
    z_total = np.where(total_sd > 0, error / np.where(total_sd > 0, total_sd, 1), np.nan)
    return pd.DataFrame(
        {
            "obs": obs,
            "surface": surface,
            "error": error,
            "z": z,
            "z_total": z_total,
            "inputs_reproduced": [True] * len(obs) if reproduced is None else reproduced,
        }
    )


# @spec EVAL-METRIC-001
def test_accuracy_metrics_match_hand_computation():
    frame = _frame(obs=[2.0, 4.0, 6.0, 8.0], surface=[3.0, 3.0, 7.0, 10.0], sd=[1, 1, 1, 1])
    m = metrics.matchup_metrics(frame)
    errors = np.array([1.0, -1.0, 1.0, 2.0])
    assert m["n"] == 4
    assert m["bias"] == pytest.approx(errors.mean())
    assert m["mae"] == pytest.approx(np.abs(errors).mean())
    assert m["rmse"] == pytest.approx(np.sqrt((errors**2).mean()))
    assert m["r2"] == pytest.approx(1 - (errors**2).sum() / 20.0)
    assert m["pearson_r"] == pytest.approx(np.corrcoef([3, 3, 7, 10], [2, 4, 6, 8])[0, 1])


# @spec EVAL-METRIC-001
def test_r2_and_r_are_null_for_one_matchup_or_no_variance():
    assert np.isnan(metrics.matchup_metrics(_frame([5.0], [6.0], [1.0]))["r2"])
    flat = metrics.matchup_metrics(_frame([5.0, 5.0], [6.0, 7.0], [1.0, 1.0]))
    assert np.isnan(flat["r2"]) and np.isnan(flat["pearson_r"])
    assert metrics.matchup_metrics(_frame([], [], []))["n"] == 0


# @spec EVAL-METRIC-002
def test_coverage_counts_only_qualifying_matchups():
    frame = _frame(
        obs=[0, 0, 0, 0, 0],
        surface=[0.5, 1.5, 2.5, 1.0, 9.0],
        sd=[1, 1, 1, 0, 1],  # sd = 0: z null, not counted
        total_sd=[1, 1, 2, 1, 10],
        reproduced=[True, True, True, True, False],  # last row: not counted for total
    )
    m = metrics.matchup_metrics(frame)
    # kriging: |z| = 0.5, 1.5, 2.5, 9 -> within 1: 1/4, within 2: 2/4
    assert m["cov1_krig"] == pytest.approx(0.25) and m["cov2_krig"] == pytest.approx(0.5)
    # total: |z_total| = 0.5, 1.5, 1.25, 1.0 (row 5 excluded) -> within 1: 2/4, within 2: 4/4
    assert m["cov1_total"] == pytest.approx(0.5) and m["cov2_total"] == pytest.approx(1.0)
    none = metrics.matchup_metrics(_frame([1.0], [1.0], [0.0], total_sd=[0.0]))
    assert np.isnan(none["cov1_krig"]) and np.isnan(none["cov2_total"])


# @spec EVAL-SPLIT-002
def test_pm25_level_groups():
    assert [metrics.level_group(v) for v in (0.0, 4.99, 5.0, 9.0, 9.01)] == [
        "<5",
        "<5",
        "5–9",
        "5–9",
        ">9",
    ]


# @spec EVAL-SPLIT-003
def test_pooled_share_bands_are_labelled_tertiles_with_merged_edges():
    hours = pd.date_range("2026-09-14", periods=9, freq="h", tz=UTC)
    shares = pd.Series([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9], index=hours)
    bands = metrics.pooled_bands(shares)
    assert bands.nunique() == 3
    assert bands.iloc[0] == bands.iloc[2] != bands.iloc[3]
    assert all(label.startswith("[") or label.startswith("(") for label in bands)
    tied = metrics.pooled_bands(pd.Series([0.5] * 8 + [0.9], index=hours))
    assert 1 <= tied.nunique() < 3


# @spec EVAL-SPLIT-001, EVAL-SPLIT-002, EVAL-SPLIT-004
def test_metric_table_has_both_subsets_and_every_split():
    hours = [datetime(2026, 9, 14, 10, tzinfo=UTC), datetime(2026, 9, 15, 11, tzinfo=UTC)]
    frame = _frame(obs=[3.0, 7.0, 12.0, 4.0], surface=[4.0, 6.0, 11.0, 4.5], sd=[1, 1, 1, 1])
    frame["hour"] = [hours[0], hours[0], hours[1], hours[1]]
    frame["flagged"] = [False, True, False, False]
    bands = pd.Series(["low", "high"], index=hours)
    table = metrics.metric_table(frame, bands)
    assert list(table.columns[:3]) == ["subset", "split", "group"]
    assert set(table["subset"]) == {"unflagged", "all"}
    assert set(table["split"]) == {
        "overall", "level", "pred_level", "hour_of_day", "day", "pooled_band"
    }  # the labelled splits (monitor, spike, variogram, hour means) need their columns
    overall = table[(table["split"] == "overall")].set_index("subset")
    assert overall.loc["unflagged", "n"] == 3 and overall.loc["all", "n"] == 4
    days = table[(table["subset"] == "all") & (table["split"] == "day")]
    assert set(days["group"]) == {"2026-09-14", "2026-09-15"}
    for column in ("bias", "mae", "rmse", "r2", "pearson_r", "cov1_krig", "cov2_total"):
        assert column in table.columns


# --- Fit covariance ---------------------------------------------------------------------------


def _hours(n, start=datetime(2026, 9, 10, tzinfo=UTC)):
    return [start + timedelta(hours=k) for k in range(n)]


# @spec EVAL-CALSE-002
def test_fit_covariance_matches_the_closed_form():
    rng = np.random.default_rng(1)
    x = rng.uniform(2, 20, 50)
    y = 0.5 + 0.7 * x + rng.normal(0, 1, 50)
    fit = calse.fit_covariance(x, y, ["s"] * 50, rng.permutation(_hours(50)))
    design = np.column_stack([np.ones(50), x])
    beta, *_ = np.linalg.lstsq(design, y, rcond=None)
    residual = y - design @ beta
    sigma2 = (residual**2).sum() / 48
    expected = sigma2 * np.linalg.inv(design.T @ design)
    assert (fit.intercept, fit.slope) == pytest.approx(tuple(beta))
    assert fit.sigma2 == pytest.approx(sigma2)
    assert fit.var_a_raw == pytest.approx(expected[0, 0])
    assert fit.var_b_raw == pytest.approx(expected[1, 1])
    assert fit.cov_ab_raw == pytest.approx(expected[0, 1])


# @spec EVAL-CALSE-003
def test_lag1_autocorrelation_pairs_consecutive_hours_within_one_series():
    hours = _hours(4)
    residuals = np.array([1.0, 1.0, -1.0, -1.0])
    # pairs (1,1), (1,-1), (-1,-1): (1 - 1 + 1) / 4
    assert calse.lag1_autocorrelation(residuals, ["s"] * 4, hours) == pytest.approx(0.25)
    gap = [hours[0], hours[1], hours[3], hours[3] + timedelta(hours=1)]
    # only (1,1) and (-1,-1) are consecutive: 2 / 4
    assert calse.lag1_autocorrelation(residuals, ["s"] * 4, gap) == pytest.approx(0.5)
    two_series = ["a", "a", "b", "b"]
    assert calse.lag1_autocorrelation(residuals, two_series, hours) == pytest.approx(0.5)


# @spec EVAL-CALSE-003
def test_covariance_is_inflated_by_n_over_n_eff():
    rng = np.random.default_rng(2)
    n = 200
    x = rng.uniform(2, 20, n)
    e = np.zeros(n)
    for k in range(1, n):
        e[k] = 0.8 * e[k - 1] + rng.normal(0, 1)
    fit = calse.fit_covariance(x, 1 + 0.6 * x + e, ["s"] * n, _hours(n))
    assert 0.5 < fit.rho1 <= 0.99
    assert fit.n_eff == pytest.approx(n * (1 - fit.rho1) / (1 + fit.rho1))
    factor = n / fit.n_eff
    assert fit.var_b == pytest.approx(fit.var_b_raw * factor)
    assert fit.var_a == pytest.approx(fit.var_a_raw * factor)
    assert fit.cov_ab == pytest.approx(fit.cov_ab_raw * factor)
    assert fit.matrix() == pytest.approx(
        np.array([[fit.var_a, fit.cov_ab], [fit.cov_ab, fit.var_b]])
    )
    alternating = calse.fit_covariance(
        np.array([1.0, 2.0, 3.0, 4.0]), np.array([1.0, 3.0, 1.0, 3.0]), ["s"] * 4, _hours(4)
    )  # residuals -0.4, 1.2, -1.2, 0.4: negative lag-1 correlation, clipped to 0
    assert alternating.rho1 == 0.0 and alternating.n_eff == 4


# @spec EVAL-CALSE-004
def test_fewer_than_three_pairs_have_no_covariance():
    assert calse.fit_covariance([1.0, 2.0], [1.0, 2.0], ["s", "s"], _hours(2)) is None


# --- Kriging weights and the calibration covariance -------------------------------------------


def _points(n=25, seed=3):
    rng = np.random.default_rng(seed)
    x, y = rng.uniform(0, 50_000, n), rng.uniform(0, 40_000, n)
    z = 8 + 2 * np.sin(x / 9000) + np.cos(y / 7000) + rng.normal(0, 0.2, n)
    return x, y, z


# @spec EVAL-TOTVAR-001
@pytest.mark.parametrize("model", ["spherical", "exponential"])
def test_ok_weights_sum_to_one_and_reproduce_pykrige(model):
    x, y, z = _points()
    params = [2.0, 15_000.0, 0.3]  # partial sill, range, nugget
    tx, ty = np.array([10_000.0, 31_234.0, 45_000.0]), np.array([5_000.0, 22_222.0, 39_000.0])
    lam = totvar.ok_weights(x, y, model, params, tx, ty)
    assert lam.shape == (3, len(x))
    assert lam.sum(axis=1) == pytest.approx(np.ones(3))
    kriger = OrdinaryKriging(
        x, y, z, variogram_model=model, variogram_parameters=totvar.as_fixed(params)
    )
    expected, _ = kriger.execute("points", tx, ty, backend="vectorized")
    assert lam @ z == pytest.approx(np.ma.getdata(expected), rel=1e-6)


# @spec EVAL-TOTVAR-002
def test_sensor_covariance_shares_pooled_parameter_error_only():
    x = np.array([2.0, 4.0, 6.0, 8.0])
    c_fit = np.array([[0.04, -0.01], [-0.01, 0.01]])
    c_pool = np.array([[0.09, -0.02], [-0.02, 0.02]])
    C = np.array([c_fit, c_fit, c_pool, c_pool])
    sigma2 = np.array([1.0, 1.5, 2.0, 2.0])
    key = [None, None, "2026-09-14", "2026-09-14"]
    sigma = totvar.sensor_covariance(x, C, sigma2, key)
    v = lambda c, a, b: np.array([1, a]) @ c @ np.array([1, b])  # noqa: E731
    assert sigma[0, 0] == pytest.approx(v(c_fit, 2, 2) + 1.0)
    assert sigma[2, 3] == pytest.approx(v(c_pool, 6, 8))
    assert sigma[3, 2] == pytest.approx(sigma[2, 3])
    assert sigma[0, 1] == 0 and sigma[0, 2] == 0
    other = totvar.sensor_covariance(x, C, sigma2, [None, None, "2026-09-14", "2026-09-15"])
    assert other[2, 3] == 0


# @spec EVAL-TOTVAR-003
def test_aggregation_matrix_averages_co_located_sensors():
    sx = np.array([100.4, 100.2, 500.0])
    sy = np.array([200.1, 199.9, 600.0])
    px, py = np.array([100.0, 500.0]), np.array([200.0, 600.0])
    M = totvar.aggregation_matrix(sx, sy, px, py)
    assert M == pytest.approx(np.array([[0.5, 0.5, 0.0], [0.0, 0.0, 1.0]]))


# @spec EVAL-TOTVAR-003
def test_shared_pooled_error_survives_averaging():
    n = 10
    lam = np.full((1, n), 1 / n)
    M = np.eye(n)
    x = np.full(n, 5.0)
    C = np.repeat(np.array([[[0.1, 0.0], [0.0, 0.01]]]), n, axis=0)
    sigma2 = np.zeros(n)
    shared = totvar.cal_variance(lam, M, totvar.sensor_covariance(x, C, sigma2, ["p"] * n))
    independent = totvar.cal_variance(lam, M, totvar.sensor_covariance(x, C, sigma2, [None] * n))
    per_sensor = 0.1 + 25 * 0.01
    assert shared == pytest.approx([per_sensor])
    assert independent == pytest.approx([per_sensor / n])


# --- Monte Carlo ------------------------------------------------------------------------------


# @spec EVAL-MC-001
def test_mc_hour_selection_is_seeded_stratified_and_takes_all_when_short():
    hours = pd.date_range("2026-09-01", periods=24 * 6, freq="h", tz=UTC)
    means = pd.Series(np.linspace(1, 20, len(hours)), index=hours)
    picked = montecarlo.select_mc_hours(means, 6, seed=0)
    assert picked == montecarlo.select_mc_hours(means, 6, seed=0)
    assert len(picked) == 6 and picked == sorted(picked)
    tertile = pd.qcut(means, 3, labels=False)
    assert sorted(tertile[picked].value_counts().tolist()) == [2, 2, 2]
    assert len({h.date() for h in picked}) == 6
    assert montecarlo.select_mc_hours(means.iloc[:4], 10, seed=0) == list(means.index[:4])


# @spec EVAL-MC-002, EVAL-MC-003
def test_monte_carlo_matches_the_analytic_variance_and_shows_the_cost_of_independence():
    x, y, _ = _points(n=16, seed=5)
    n = len(x)
    corrected = np.full(n, 10.0)
    intercept, slope = np.full(n, 0.5), np.full(n, 0.8)
    pooled = np.arange(n) % 2 == 0
    C = np.where(
        pooled[:, None, None],
        np.array([[0.2, -0.01], [-0.01, 0.002]]),
        np.array([[0.1, -0.005], [-0.005, 0.001]]),
    )
    sigma2 = np.full(n, 0.5)
    key = ["p" if p else None for p in pooled]
    params = [2.0, 15_000.0, 0.3]
    tx, ty = np.array([20_000.0]), np.array([20_000.0])
    lam = totvar.ok_weights(x, y, "spherical", params, tx, ty)
    analytic = totvar.cal_variance(
        lam, np.eye(n), totvar.sensor_covariance(corrected, C, sigma2, key)
    )
    rng = np.random.default_rng(0)
    common = (x, y, corrected, intercept, slope, C, sigma2, key, "spherical", params, tx, ty)
    mc = montecarlo.propagate(*common, draws=4000, rng=rng)
    assert mc == pytest.approx(analytic, rel=0.1)
    independent = montecarlo.propagate(*common, draws=4000, rng=rng, shared_pooled=False)
    assert independent[0] < mc[0]


# --- Decomposition ----------------------------------------------------------------------------


# @spec EVAL-DECOMP-001
def test_detector_term_in_calibrated_units():
    snapshots = pd.DataFrame(
        {"hour": [0, 1, 1], "a": [10.0, 4.0, 6.0], "b": [8.0, 4.0, 2.0]}
    )  # hour 0: (2²/4)/1 = 1; hour 1: mean(0, 16/4)/2 = 1
    expected = (0.524 * 0.8) ** 2 * 1.0
    assert decompose.detector_term(0.8, snapshots) == pytest.approx(expected)


# @spec EVAL-DECOMP-001
def test_detector_term_treats_each_sensor_hour_as_its_own_pair():
    snapshots = pd.DataFrame(
        {"site_id": ["s1", "s2"], "hour": [0, 0], "a": [10.0, 6.0], "b": [8.0, 4.0]}
    )  # two sensors in one hour: two pairs of k = 1, each (2²/4)/1 = 1
    assert decompose.detector_term(1.0, snapshots) == pytest.approx(0.524**2)


# @spec EVAL-DECOMP-002
def test_split_residual_and_clamped_remainder():
    assert decompose.REFERENCE_SD == {"T640": 0.5, "BAM-1020": 1.8}
    parts = decompose.split_residual(sigma2=4.0, detector=1.0, ref_sd=0.5)
    assert parts == {"a": 1.0, "b": 0.25, "c": 2.75, "clamped": False}
    clamped = decompose.split_residual(sigma2=2.0, detector=1.0, ref_sd=1.8)
    assert clamped["c"] == 0.0 and clamped["clamped"]


# --- Error budget -----------------------------------------------------------------------------


# @spec EVAL-BUDGET-001, EVAL-BUDGET-002
def test_error_budget_lists_every_known_source():
    table = budget.error_budget({})
    assert list(table.columns) == ["source", "stage", "type", "handling", "magnitude"]
    assert len(table) >= 11
    assert set(table["type"]) <= {"random", "systematic-shared"}
    assert set(table["handling"]) <= {
        "quantified-included",
        "quantified-diagnostic",
        "listed-not-modeled",
    }
    by_source = table.set_index("source")
    assert by_source.loc[budget.POOLED_FIT, "type"] == "systematic-shared"
    assert by_source.loc[budget.POOLED_FIT, "handling"] == "quantified-included"
    assert by_source.loc[budget.BARKJOHN, "handling"] == "listed-not-modeled"
    assert by_source.loc[budget.DEPENDENCE, "handling"] == "quantified-diagnostic"
    assert (table["magnitude"] != "").all()


# --- Settings ---------------------------------------------------------------------------------


# @spec EVAL-CFG-001
def test_settings_defaults_environment_and_validation(monkeypatch):
    for name in (
        "AQDT_EVAL_MC_HOURS",
        "AQDT_EVAL_MC_DRAWS",
        "AQDT_EVAL_LOMO_HOURS",
        "AQDT_EVAL_SEED",
    ):
        monkeypatch.delenv(name, raising=False)
    s = EvaluationSettings()
    assert (s.mc_hours, s.mc_draws, s.lomo_hours, s.seed) == (24, 500, 6, 0)
    monkeypatch.setenv("AQDT_EVAL_MC_DRAWS", "50")
    monkeypatch.setenv("AQDT_EVAL_LOMO_HOURS", "0")
    assert (EvaluationSettings().mc_draws, EvaluationSettings().lomo_hours) == (50, 0)
    for field, value in (("mc_hours", 0), ("mc_draws", 0), ("lomo_hours", -1), ("seed", -1)):
        with pytest.raises(ValidationError, match=field):
            EvaluationSettings(**{field: value})
