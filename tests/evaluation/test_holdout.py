"""Development and test windows — EVAL-HOLD. A six-day synthetic archive (kriged every hour) split
into a three-day development window and a three-day test window, and the pure pieces checked
against hand-computed values."""

from __future__ import annotations

import json
import math
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from PIL import Image
from scipy.stats import norm

from aqdt.observation_store.archive import write_observations, write_sites
from aqdt.observation_store.schemas import Observation, QcFlag, Source

from .conftest import AS_OF, MIN_PAIRS, MONITORS, SENSORS, WINDOW_DAYS, _sites, truth

H = timedelta(hours=1)
DAYS = 6
DEV = (AS_OF, AS_OF + timedelta(days=3))
TEST = (AS_OF + timedelta(days=3), AS_OF + timedelta(days=DAYS))
RESOLUTION_M = 2000
#: a flagged and a null monitor reading in the test window
T_FLAGGED = (list(MONITORS)[1], TEST[0] + 30 * H)
T_NULL = (list(MONITORS)[2], TEST[0] + 40 * H)
HOLD_ENV = {
    "AQDT_CAL_WINDOW_DAYS": str(WINDOW_DAYS),
    "AQDT_CAL_MIN_PAIRS": str(MIN_PAIRS),
    "AQDT_FUS_RESOLUTION_M": str(RESOLUTION_M),
}
FIGURES = [
    "reliability_coverage",
    "spread_skill",
    "by_distance",
    "by_local_hour",
    "by_monitor",
    "total_sd_map",
    "methods_test",
    "shap_beeswarm",
    "example_timeseries",
    "example_maps",
]
LOCAL = "America/New_York"
#: SHAP points per run in these tests; the run's own sample is pinned separately
TEST_SHAP_SAMPLE = 100


def _observations() -> list[Observation]:
    rng = np.random.default_rng(23)
    out: list[Observation] = []
    hour = AS_OF - timedelta(days=WINDOW_DAYS)
    while hour < TEST[1]:
        for site_id, (lat, lon) in MONITORS.items():
            value: float | None = float(truth(lon, lat, hour) + rng.normal(0, 0.3))
            flags: list[QcFlag] = []
            if (site_id, hour) == T_FLAGGED:
                flags = [QcFlag.flatline]
            if (site_id, hour) == T_NULL:
                value, flags = None, [QcFlag.missing_value]
            out.append(
                Observation(
                    site_id=site_id,
                    source=Source.airnow,
                    observed_at=hour,
                    latitude=lat,
                    longitude=lon,
                    pm25_raw=value,
                    pm25_channel_a=None,
                    pm25_channel_b=None,
                    humidity=None,
                    pm25_corrected=None,
                    qc_flags=flags,
                    raw={"Value": value},
                )
            )
        # a day-to-day level change the forest's hour median has to carry
        level = 1.0 + 0.15 * ((hour - AS_OF) // timedelta(days=1))
        for site_id, (lat, lon, gain, offset) in SENSORS.items():
            corrected = (level * float(truth(lon, lat, hour)) - offset) / gain + rng.normal(0, 0.4)
            mean = max(corrected, 0.5) / 0.524
            diff = rng.normal(0, 0.6)
            out.append(
                Observation(
                    site_id=site_id,
                    source=Source.purpleair,
                    observed_at=hour + timedelta(minutes=30),
                    latitude=lat,
                    longitude=lon,
                    pm25_raw=mean,
                    pm25_channel_a=mean + diff / 2,
                    pm25_channel_b=mean - diff / 2,
                    humidity=50.0,
                    pm25_corrected=max(corrected, 0.0),
                    qc_flags=[],
                    raw={"sensor_index": int(site_id.split(":")[1])},
                )
            )
        hour += H
    return out


def build_long_archive(uri: str) -> str:
    from aqdt.calibration.apply import apply_calibrations
    from aqdt.calibration.fit import fit_calibrations
    from aqdt.calibration.schemas import CalibrationSettings
    from aqdt.fusion.run import krige_surfaces
    from aqdt.fusion.schemas import FusionSettings

    write_sites(_sites(), uri)
    write_observations(_observations(), uri)
    cal = CalibrationSettings(window_days=WINDOW_DAYS, min_pairs=MIN_PAIRS)
    fit_calibrations(uri, AS_OF, cal)
    apply_calibrations(uri, DEV[0], TEST[1], cal)
    krige_surfaces(uri, DEV[0], TEST[1], FusionSettings(resolution_m=RESOLUTION_M))
    return uri


def _environment(mp: pytest.MonkeyPatch) -> None:
    from aqdt.evaluation import forest

    mp.setattr(forest, "SHAP_SAMPLE", TEST_SHAP_SAMPLE)  # exact TreeSHAP is the slow step
    for name, value in HOLD_ENV.items():
        mp.setenv(name, value)
    for name in ("AQDT_BBOX", "AQDT_MAP_PM25_MAX", "AQDT_MAP_SD_MAX", "AQDT_FUS_MIN_POINTS"):
        mp.delenv(name, raising=False)


def _settings():
    from aqdt.evaluation.schemas import EvaluationSettings

    return EvaluationSettings(mc_hours=1, mc_draws=20, lomo_hours=1, seed=0)


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(scope="module")
def long_archive(tmp_path_factory) -> str:
    with pytest.MonkeyPatch.context() as mp:
        _environment(mp)
        return build_long_archive(str(tmp_path_factory.mktemp("long") / "archive"))


@pytest.fixture(scope="module")
def bundle(long_archive, tmp_path_factory):
    """One packed holdout run over the synthetic archive; tests read its outputs."""
    from aqdt.evaluation.holdout import holdout

    root = tmp_path_factory.mktemp("bundle")
    out = root / "evidence"
    before = _tree(Path(long_archive))
    with pytest.MonkeyPatch.context() as mp:
        _environment(mp)
        mp.setenv("AQDT_TEST_API_KEY", "do-not-print")
        summary = holdout(long_archive, DEV, TEST, out, _settings(), pack=True)
    return out, summary, before


def _csv(out: Path, name: str) -> pd.DataFrame:
    frame = pd.read_csv(out / name)
    if "hour" in frame:
        frame["hour"] = pd.to_datetime(frame["hour"], utc=True)
    return frame


@pytest.fixture(scope="module")
def test_matchups(bundle) -> pd.DataFrame:
    frame = _csv(bundle[0] / "test", "matchups.csv")
    frame["flagged"] = frame["flagged"].astype(bool)
    frame["inputs_reproduced"] = frame["inputs_reproduced"].astype(bool)
    return frame


@pytest.fixture(scope="module")
def scored(test_matchups) -> pd.DataFrame:
    m = test_matchups
    keep = (
        ~m["flagged"] & m["inputs_reproduced"] & m["cal_variance"].notna() & (m["total_sd"] > 0)
    )
    return m[keep].reset_index(drop=True)


@pytest.fixture(scope="module")
def sigma2_dev(bundle) -> float:
    diag = pd.read_csv(bundle[0] / "dev" / "coverage_diagnostics.csv")
    return float(diag.loc[diag["kind"] == "z_total_shared", "shared_variance"].iloc[0])


def _share(values) -> float:
    return float(np.mean(values))


# --- Entry point, windows, and the two evaluations ---------------------------------------------


# @spec EVAL-HOLD-001
def test_overlapping_or_naive_windows_fail_before_writing(long_archive, tmp_path):
    from aqdt.evaluation.holdout import holdout

    out = tmp_path / "never"
    overlapping = (DEV[0] + timedelta(days=2), TEST[1])
    with pytest.raises(ValueError, match="test window") as error:
        holdout(long_archive, DEV, overlapping, out, _settings())
    assert DEV[1].isoformat()[:13] in str(error.value)
    naive = (datetime(2026, 9, 14), datetime(2026, 9, 15))
    with pytest.raises(ValueError):
        holdout(long_archive, naive, TEST, out, _settings())
    assert not out.exists()


# @spec EVAL-HOLD-001, EVAL-HOLD-002
def test_both_windows_are_evaluated_and_the_archive_is_untouched(bundle, long_archive):
    out, summary, before = bundle
    assert summary.dev == DEV and summary.test == TEST
    assert summary.out_dir == out
    assert summary.archive == out.parent / "evidence.tgz"
    assert set(summary.rf_choice) == {"n_estimators", "min_samples_leaf", "max_features"}
    for name, (start, end) in (("dev", DEV), ("test", TEST)):
        run = json.loads((out / name / "run.json").read_text())
        assert run["window_start"] == start.isoformat()
        assert run["window_end"] == end.isoformat()
        assert run["evaluation_settings"] == _settings().model_dump()
        assert (out / name / "summary.md").exists()
    assert _tree(Path(long_archive)) == before


# @spec EVAL-HOLD-004
def test_completeness_counts_each_window(bundle, test_matchups):
    table = _csv(bundle[0], "completeness.csv").set_index("window")
    for name in ("dev", "test"):
        row = table.loc[name]
        assert row["hours"] == 72 and row["kriged_hours"] == 72 and row["no_fit_row"] == 0
        assert row["days"] == 3
        assert row["monitor_hours_possible"] == 72 * 4  # four monitors inside the grid
        assert row["monitor_hours_all"] == 72 * 4
    test = table.loc["test"]
    assert test["monitor_hours_valued"] == 72 * 4 - 1
    assert test["monitor_hours_unflagged"] == 72 * 4 - 2
    assert test["matchups"] == len(test_matchups)
    assert test["matchups_unflagged"] == int((~test_matchups["flagged"]).sum())
    assert test["scored"] == int(
        (~test_matchups["flagged"] & test_matchups["inputs_reproduced"]
         & test_matchups["cal_variance"].notna()).sum()
    )
    assert pd.isna(table.loc["dev", "scored"])


# --- Development against test, the offset out of sample -----------------------------------------


# @spec EVAL-HOLD-005
def test_dev_vs_test_copies_each_reports_overall_unflagged_row(bundle):
    out = bundle[0]
    table = _csv(out, "dev_vs_test.csv").set_index("statistic")
    for name in ("dev", "test"):
        metrics = pd.read_csv(out / name / "metrics.csv")
        row = metrics[(metrics["subset"] == "unflagged") & (metrics["split"] == "overall")].iloc[0]
        for stat in ("n", "bias", "mae", "rmse", "r2", "pearson_r", "cov1_krig", "cov2_total"):
            assert table.loc[stat, name] == pytest.approx(row[stat])
        assert table.loc["rmse", f"{name}_ci_low"] == pytest.approx(row["rmse_ci_low"])
        lomo = pd.read_csv(out / name / "lomo.csv")
        lomo = lomo[~lomo["flagged"].astype(bool)]
        expected = math.sqrt((lomo["lomo_error"].dropna() ** 2).mean())
        assert table.loc["lomo_rmse", name] == pytest.approx(expected)
        d = (lomo["lomo_error"].abs() - lomo["control_error"].abs()).dropna()
        assert table.loc["lomo_median_d", name] == pytest.approx(float(d.median()))
    assert {"stored_rmse", "control_rmse", "lomo_rmse", "lomo_median_d"} <= set(table.index)


# @spec EVAL-HOLD-003, EVAL-HOLD-006
def test_offset_is_estimated_on_dev_and_scored_on_test(bundle, scored, sigma2_dev):
    table = _csv(bundle[0], "oos_offset.csv").iloc[0]
    assert table["sigma2_shared_dev"] == pytest.approx(sigma2_dev)
    assert table["n"] == len(scored)
    z = scored["error"] / np.sqrt(scored["total_variance"] + sigma2_dev)
    assert table["cov1"] == pytest.approx(_share(z.abs() <= 1))
    assert table["cov2"] == pytest.approx(_share(z.abs() <= 2))
    assert table["cov1_ci_low"] <= table["cov1"] <= table["cov1_ci_high"]


# @spec EVAL-HOLD-003, EVAL-HOLD-007
def test_reliability_scores_four_sds_at_five_widths_on_the_scored_set(bundle, scored, sigma2_dev):
    table = _csv(bundle[0], "reliability_coverage.csv")
    assert list(table["sd_kind"].unique()) == ["kriging", "total", "total_dev_offset", "rf_trees"]
    widths = [0.6745, 1.0, 1.2816, 1.6449, 2.0]
    rf = _csv(bundle[0], "rf_dev.csv")
    merged = scored.merge(rf, on=["hour", "site_id"])
    sds = {
        "kriging": (scored["error"], scored["sd"]),
        "total": (scored["error"], scored["total_sd"]),
        "total_dev_offset": (scored["error"], np.sqrt(scored["total_variance"] + sigma2_dev)),
        "rf_trees": (merged["rf_dev"] - merged["obs"], merged["rf_sd"]),
    }
    for kind, (error, sd) in sds.items():
        rows = table[table["sd_kind"] == kind].reset_index(drop=True)
        assert rows["k"].tolist() == pytest.approx(widths)
        assert rows["nominal"].tolist() == pytest.approx([2 * norm.cdf(k) - 1 for k in widths])
        assert (rows["n"] == len(scored)).all()
        for row in rows.itertuples():
            assert row.observed == pytest.approx(_share((error.abs() / sd) <= row.k)), kind
    total_at_1 = table[(table["sd_kind"] == "total") & (table["k"] == 1.0)]["observed"].iloc[0]
    metrics = pd.read_csv(bundle[0] / "test" / "metrics.csv")
    overall = metrics[(metrics["subset"] == "unflagged") & (metrics["split"] == "overall")]
    assert total_at_1 == pytest.approx(overall["cov1_total"].iloc[0])


# --- Where and when the uncertainty is large -----------------------------------------------------


# @spec EVAL-HOLD-008
def test_spread_skill_bins_by_total_sd_quintile(bundle, scored):
    table = _csv(bundle[0], "spread_skill.csv")
    assert 1 <= len(table) <= 5 and table["n"].sum() == len(scored)
    edges = np.quantile(scored["total_sd"], [0, 0.2, 0.4, 0.6, 0.8, 1.0])
    first = scored[scored["total_sd"] <= edges[1]]
    row = table.iloc[0]
    assert row["n"] == len(first)
    assert row["mean_total_sd"] == pytest.approx(first["total_sd"].mean())
    assert row["rms_total_sd"] == pytest.approx(math.sqrt(first["total_variance"].mean()))
    assert row["rmse"] == pytest.approx(math.sqrt((first["error"] ** 2).mean()))
    assert row["cov1_total"] == pytest.approx(_share(first["z_total"].abs() <= 1))
    assert (table["mean_total_sd"].diff().dropna() > 0).all()


# @spec EVAL-HOLD-009
def test_distance_and_local_hour_tables(bundle, scored):
    out = bundle[0]
    distance = _csv(out, "by_distance.csv")
    labels = ["<0.75", "0.75–1.25", "1.25–2", "≥2"]
    assert set(distance["group"]) <= set(labels)
    assert distance["n"].sum() == len(scored)
    bins = pd.cut(scored["nearest_sensor_km"], [0, 0.75, 1.25, 2, np.inf], right=False,
                  labels=labels)
    for row in distance.itertuples():
        part = scored[bins == row.group]
        assert row.n == len(part)
        assert row.mean_sd == pytest.approx(part["sd"].mean())
        assert row.mean_total_sd == pytest.approx(part["total_sd"].mean())
        assert row.mae == pytest.approx(part["error"].abs().mean())
        assert set(row.monitors.split(";")) == set(part["site_id"])
        assert row.n_monitors == part["site_id"].nunique()

    by_hour = _csv(out, "by_local_hour.csv")
    local = scored["hour"].dt.tz_convert(LOCAL).dt.hour
    assert by_hour["group"].tolist() == sorted(local.unique())
    seven = scored[local == 7]
    row = by_hour[by_hour["group"] == 7].iloc[0]
    assert row["n"] == len(seven)
    assert row["rmse"] == pytest.approx(math.sqrt((seven["error"] ** 2).mean()))


# @spec EVAL-HOLD-010
def test_by_monitor_scores_each_monitor(bundle, scored, sigma2_dev):
    table = _csv(bundle[0], "by_monitor.csv").set_index("site_id")
    assert set(table.index) == set(scored["site_id"])
    site = scored["site_id"].iloc[0]
    part = scored[scored["site_id"] == site]
    row = table.loc[site]
    assert row["n"] == len(part)
    assert row["rmse"] == pytest.approx(math.sqrt((part["error"] ** 2).mean()))
    assert row["mean_nearest_sensor_km"] == pytest.approx(part["nearest_sensor_km"].mean())
    z = part["error"] / np.sqrt(part["total_variance"] + sigma2_dev)
    assert row["cov2_total_dev_offset"] == pytest.approx(_share(z.abs() <= 2))
    assert row["cov1_kriging"] == pytest.approx(_share(part["z"].abs() <= 1))
    for column in ("rmse_ci_low", "cov1_total_ci_high", "cov2_kriging_ci_low"):
        assert column in table


# @spec EVAL-HOLD-011
def test_typical_hour_and_the_total_sd_map(bundle, scored):
    out = bundle[0]
    table = _csv(out, "total_sd_map.csv")
    pool = scored[scored["variogram"] == "regular"]
    pool = pool if len(pool) else scored
    medians = pool.groupby("hour")["total_sd"].median()
    target = medians.median()
    gaps = (medians - target).abs()
    expected = gaps[gaps == gaps.min()].index.min()
    assert (table["hour"] == expected).all()
    assert set(table["fit_status"]) <= {"fitted", "pooled"}
    assert table["site_id"].is_unique
    stored_max = scored.loc[scored["hour"] == expected, "sd"].max()
    assert table["max_total_sd"].iloc[0] >= stored_max


# @spec EVAL-HOLD-011
def test_cell_calibration_variance_matches_the_matchups_at_the_monitors(
    bundle, long_archive, scored, monkeypatch
):
    """The per-cell calibration term, evaluated at the monitors' own coordinates, is the
    matchups' ``cal_variance`` (EVAL-TOTVAR-003)."""
    from aqdt.evaluation.holdout import hour_calibration_variance
    from aqdt.evaluation.run import _monitor_sites
    from aqdt.observation_store.archive import read_sites

    _environment(monkeypatch)
    hour = scored["hour"].iloc[0]
    rows = scored[scored["hour"] == hour].merge(
        _monitor_sites(read_sites(long_archive)), on="site_id"
    )
    values = hour_calibration_variance(
        long_archive, hour.to_pydatetime(), rows["x"].to_numpy(), rows["y"].to_numpy()
    )
    assert values == pytest.approx(rows["cal_variance"].to_numpy(), rel=1e-6)


# --- The development forest -------------------------------------------------------------------


# @spec EVAL-HOLD-012, EVAL-HOLD-013
def test_grid_search_scores_twelve_configurations_by_day_folds(bundle):
    grid = _csv(bundle[0], "rf_grid.csv")
    assert len(grid) == 12
    assert set(grid["n_estimators"]) == {200, 500}
    assert set(grid["min_samples_leaf"]) == {1, 5, 20}
    assert set(grid["max_features"]) == {1.0, 0.5}
    folds = [c for c in grid if c.startswith("fold_")]
    assert folds == ["fold_1", "fold_2", "fold_3"]  # three development days
    assert grid["mean_rmse"].to_numpy() == pytest.approx(grid[folds].mean(axis=1).to_numpy())
    assert sorted(grid["rank"]) == list(range(1, 13))
    assert grid.loc[grid["chosen"], "rank"].tolist() == [1]


# @spec EVAL-HOLD-012
def test_training_set_is_every_dev_kriging_point(long_archive, monkeypatch):
    from aqdt.evaluation.holdout import window_points
    from aqdt.fusion.products import SURFACE_FITS
    from aqdt.observation_store.archive import read_partitioned

    _environment(monkeypatch)
    points = window_points(long_archive, *DEV)
    fits = read_partitioned(long_archive, SURFACE_FITS)
    fits = fits[(fits["hour"] >= DEV[0]) & (fits["hour"] < DEV[1]) & (fits["status"] == "kriged")]
    assert list(points.columns) == ["hour", "x", "y", "z"]
    assert len(points) == int(fits["n_points"].sum())
    assert points["hour"].min() >= DEV[0] and points["hour"].max() < DEV[1]


# @spec EVAL-HOLD-013
def test_grid_search_needs_two_days():
    from aqdt.evaluation import forest

    one_day = pd.DataFrame(
        {
            "hour": pd.to_datetime(["2026-09-14T10", "2026-09-14T11"] * 10, utc=True),
            "x": np.arange(20.0),
            "y": np.arange(20.0),
            "z": np.arange(20.0),
        }
    )
    with pytest.raises(ValueError, match="two"):
        forest.grid_search(one_day, seed=0, window="[2026-09-14, 2026-09-15)")


def _small_forests(monkeypatch, forest) -> list:
    """Make every grid forest two trees, and record a weak reference to each as it is built."""
    import weakref

    built = []
    real = forest._model

    def small(config, seed):
        model = real({**config, "n_estimators": 2}, seed)
        built.append(weakref.ref(model))
        return model

    monkeypatch.setattr(forest, "_model", small)
    return built


def _two_day_points() -> pd.DataFrame:
    rng = np.random.default_rng(2)
    hours = pd.date_range("2026-09-14", periods=48, freq="h", tz="UTC")
    points = pd.DataFrame(
        {
            "hour": np.repeat(hours, 5),
            "x": rng.uniform(0, 1e4, 240),
            "y": rng.uniform(0, 1e4, 240),
        }
    )
    points["z"] = points["x"] / 1e3 + rng.normal(0, 0.5, 240)
    return points


# @spec EVAL-HOLD-024
def test_grid_search_logs_each_configuration_as_it_finishes(monkeypatch, caplog):
    import re

    from aqdt.evaluation import forest

    _small_forests(monkeypatch, forest)
    with caplog.at_level("INFO", logger="aqdt.evaluation.forest"):
        table = forest.grid_search(_two_day_points(), seed=0, window="[2026-09-14, 2026-09-16)")
    lines = [r.getMessage() for r in caplog.records if r.name == "aqdt.evaluation.forest"]
    pattern = re.compile(
        r"rf grid (\d+)/12: n_estimators=(\d+) min_samples_leaf=(\d+) "
        r"max_features=([\d.]+): mean RMSE ([\d.]+) in [\d.]+ s, peak RSS \d+ MB"
    )
    matches = [pattern.fullmatch(line) for line in lines]
    assert len(lines) == 12 and all(matches), lines
    for k, (match, config, (_, row)) in enumerate(
        zip(matches, forest.configurations(), table.iterrows()), start=1
    ):
        assert int(match[1]) == k
        assert (int(match[2]), int(match[3]), float(match[4])) == (
            config["n_estimators"],
            config["min_samples_leaf"],
            config["max_features"],
        )
        assert float(match[5]) == pytest.approx(row["mean_rmse"], abs=5e-4)


# @spec EVAL-HOLD-024
def test_grid_search_holds_one_fitted_forest_at_a_time(monkeypatch):
    import gc

    from aqdt.evaluation import forest

    built = _small_forests(monkeypatch, forest)
    real = forest._model
    live_at_each_build = []

    def watched(config, seed):
        gc.collect()
        live_at_each_build.append(sum(ref() is not None for ref in built))
        return real(config, seed)

    monkeypatch.setattr(forest, "_model", watched)
    forest.grid_search(_two_day_points(), seed=0, window="[2026-09-14, 2026-09-16)")
    assert len(live_at_each_build) == 12 * forest.n_folds(_two_day_points())
    assert max(live_at_each_build) == 0


# @spec EVAL-HOLD-014
def test_choice_breaks_ties_toward_the_simpler_forest():
    from aqdt.evaluation import forest

    table = pd.DataFrame(
        [
            {"n_estimators": 500, "min_samples_leaf": 5, "max_features": 0.5, "mean_rmse": 1.0},
            {"n_estimators": 200, "min_samples_leaf": 1, "max_features": 1.0, "mean_rmse": 1.0},
            {"n_estimators": 200, "min_samples_leaf": 5, "max_features": 1.0, "mean_rmse": 1.0},
            {"n_estimators": 200, "min_samples_leaf": 5, "max_features": 0.5, "mean_rmse": 1.0},
            {"n_estimators": 200, "min_samples_leaf": 20, "max_features": 1.0, "mean_rmse": 1.1},
        ]
    )
    assert forest.choose(table) == {
        "n_estimators": 200,
        "min_samples_leaf": 5,
        "max_features": 0.5,
    }


# @spec EVAL-HOLD-014
def test_tree_spread_is_the_population_sd_of_the_trees():
    from aqdt.evaluation import forest

    rng = np.random.default_rng(1)
    hours = pd.date_range("2026-09-14", periods=48, freq="h", tz="UTC")
    points = pd.DataFrame(
        {
            "hour": np.repeat(hours, 10),
            "x": rng.uniform(0, 1e4, 480),
            "y": rng.uniform(0, 1e4, 480),
        }
    )
    points["z"] = points["x"] / 1e3 + rng.normal(0, 0.5, 480)
    config = {"n_estimators": 20, "min_samples_leaf": 5, "max_features": 1.0}
    model = forest.fit(points, config, seed=0)
    query = points.iloc[:7][["hour", "x", "y"]]
    mean, sd = forest.predict_with_spread(model, points, query)
    from aqdt.evaluation.baselines import features

    X = features(query, points).to_numpy(dtype=np.float32)
    trees = np.stack([t.predict(X) for t in model.estimators_])
    assert mean == pytest.approx(trees.mean(axis=0))
    assert sd == pytest.approx(trees.std(axis=0, ddof=0))


# @spec EVAL-HOLD-014
def test_rf_dev_is_written_per_test_matchup(bundle, test_matchups):
    rf = _csv(bundle[0], "rf_dev.csv")
    assert len(rf) == len(test_matchups)
    assert set(zip(rf["hour"], rf["site_id"])) == set(
        zip(test_matchups["hour"], test_matchups["site_id"])
    )
    assert (rf["rf_sd"] >= 0).all()


# @spec EVAL-HOLD-015
def test_methods_table_scores_test_matchups_with_the_in_window_forest_as_reference(
    bundle, test_matchups, scored
):
    table = _csv(bundle[0], "methods_test.csv").set_index("method")
    assert list(table.index) == ["kriging", "rf_dev", "idw", "hour_mean", "rf_in_window"]
    assert "reference only" in table.loc["rf_in_window", "note"]
    assert pd.isna(table.loc["kriging", "note"]) or table.loc["kriging", "note"] == ""
    unflagged = test_matchups[~test_matchups["flagged"]]
    rmse = math.sqrt(((unflagged["surface"] - unflagged["obs"]) ** 2).mean())
    assert table.loc["kriging", "rmse"] == pytest.approx(rmse)
    idw = math.sqrt(((unflagged["idw"] - unflagged["obs"]) ** 2).mean())
    assert table.loc["idw", "rmse"] == pytest.approx(idw)
    kriging_abs = (unflagged["surface"] - unflagged["obs"]).abs()
    paired = (kriging_abs - (unflagged["idw"] - unflagged["obs"]).abs()).mean()
    assert table.loc["idw", "paired_mean"] == pytest.approx(paired)
    assert pd.isna(table.loc["kriging", "paired_mean"])
    rf = _csv(bundle[0], "rf_dev.csv")
    merged = scored.merge(rf, on=["hour", "site_id"])
    z = (merged["rf_dev"] - merged["obs"]) / merged["rf_sd"]
    assert table.loc["rf_dev", "cov2"] == pytest.approx(_share(z.abs() <= 2))
    assert pd.isna(table.loc["idw", "cov1"])


# @spec EVAL-HOLD-016
def test_shap_ranks_the_features(bundle):
    from aqdt.evaluation.baselines import FEATURES

    table = _csv(bundle[0], "shap_importance.csv")
    assert sorted(table["feature"]) == sorted(FEATURES)
    assert (table["mean_abs_shap"].diff().dropna() <= 0).all()
    sample = _csv(bundle[0], "shap_beeswarm.csv")
    assert len(sample) == TEST_SHAP_SAMPLE  # the test window holds more points than that
    for feature in FEATURES:
        assert f"shap_{feature}" in sample
        expected = sample[f"shap_{feature}"].abs().mean()
        got = table.loc[table["feature"] == feature, "mean_abs_shap"].iloc[0]
        assert got == pytest.approx(expected)


# --- Prediction examples ---------------------------------------------------------------------


def _qualifying_dates(test_matchups) -> list:
    hours = set(test_matchups["hour"])
    dates = []
    for date in sorted({h.tz_convert(LOCAL).date() for h in hours}):
        first = pd.Timestamp(date).tz_localize(LOCAL)
        nxt = pd.Timestamp(date + timedelta(days=1)).tz_localize(LOCAL)
        local_hours = pd.date_range(first, nxt, freq="h", inclusive="both")
        if all(h.tz_convert(UTC) in hours for h in local_hours):
            dates.append(date)
    return dates


# @spec EVAL-HOLD-017, EVAL-HOLD-018
def test_example_day_time_series_and_maps(bundle, test_matchups, sigma2_dev):
    out = bundle[0]
    dates = _qualifying_dates(test_matchups)
    unflagged = test_matchups[~test_matchups["flagged"]]
    local_date = unflagged["hour"].dt.tz_convert(LOCAL).dt.date
    rmse = {
        d: math.sqrt((unflagged.loc[local_date == d, "error"] ** 2).mean()) for d in dates
    }
    ordered = sorted(dates, key=lambda d: (rmse[d], d))
    expected = ordered[(len(ordered) - 1) // 2]

    series = _csv(out, "example_timeseries.csv")
    assert set(series["hour"].dt.tz_convert(LOCAL).dt.date) == {expected}
    assert series["site_id"].nunique() == 3
    row = series.iloc[0]
    half = 2 * math.sqrt(row["total_sd"] ** 2 + sigma2_dev)
    assert row["upper_oos"] == pytest.approx(row["surface"] + half)
    assert row["lower_total"] == pytest.approx(row["surface"] - 2 * row["total_sd"])
    assert row["upper_oos"] >= row["upper_total"]

    maps = _csv(out, "example_maps.csv")
    local = maps["hour"].dt.tz_convert(LOCAL)
    assert local.dt.hour.tolist() == [6, 12, 18, 0]
    assert local.dt.date.tolist()[:3] == [expected] * 3
    assert local.iloc[3].date() == expected + timedelta(days=1)


# @spec EVAL-HOLD-016
def test_the_shap_sample_is_a_thousand_points_drawn_with_the_seed():
    from aqdt.evaluation import forest

    assert forest.SHAP_SAMPLE == 1000
    rng = np.random.default_rng(2)
    hours = pd.date_range("2026-09-14", periods=24, freq="h", tz="UTC")
    points = pd.DataFrame({"hour": np.repeat(hours, 5), "x": rng.uniform(0, 1e4, 120),
                           "y": rng.uniform(0, 1e4, 120)})
    points["z"] = points["x"] / 1e3
    model = forest.fit(points, {"n_estimators": 5, "min_samples_leaf": 5, "max_features": 1.0}, 0)
    X, values = forest.shap_sample(model, points, seed=0)
    assert len(X) == len(points)  # fewer than 1000: every point
    X3, _ = forest.shap_sample(model, points, seed=0, n=30)
    again, _ = forest.shap_sample(model, points, seed=0, n=30)
    assert len(X3) == 30 and X3.equals(again)
    expected = model.predict(X) - values.sum(axis=1).to_numpy()
    assert np.allclose(expected, expected[0])  # SHAP values add up to prediction − base value


# @spec EVAL-HOLD-017
def test_example_day_rule_picks_the_lower_median():
    from aqdt.evaluation.holdout import lower_median_key

    rmse = {"2026-09-17": 2.0, "2026-09-18": 1.0, "2026-09-19": 3.0, "2026-09-20": 2.0}
    assert lower_median_key(rmse) == "2026-09-17"


# --- Reproducibility, figures, summary, pack ---------------------------------------------------


# @spec EVAL-HOLD-019
def test_run_config_records_settings_versions_and_provenance(bundle, long_archive):
    config = json.loads((bundle[0] / "run_config.json").read_text())
    assert isinstance(config["command"], list)
    assert config["dev"] == {"start": DEV[0].isoformat(), "end": DEV[1].isoformat()}
    assert config["environment"]["AQDT_TEST_API_KEY"] == "<redacted>"
    assert config["environment"]["AQDT_CAL_MIN_PAIRS"] == str(MIN_PAIRS)
    assert config["settings"]["evaluation"] == _settings().model_dump()
    assert config["settings"]["calibration"]["min_pairs"] == MIN_PAIRS
    assert config["settings"]["fusion"]["resolution_m"] == RESOLUTION_M
    assert set(config["settings"]["maps"]) == {"pm25_max", "diff_max", "sd_max"}
    assert config["forest"]["folds"] == 3 and len(config["forest"]["grid"]) == 12
    assert config["forest"]["chosen"] == bundle[1].rf_choice
    assert config["variogram"]["candidates"] == ["spherical", "exponential"]
    assert config["calibration_rules"]["correction"]["slope"] == 0.524
    assert config["seed"] == 0 and config["shap_sample"] == TEST_SHAP_SAMPLE
    assert config["reliability_half_widths"] == [0.6745, 1.0, 1.2816, 1.6449, 2.0]
    for package in ("aqdt", "numpy", "pandas", "scipy", "scikit-learn", "pykrige", "shap",
                    "matplotlib", "rasterio", "pyproj"):
        assert config["versions"][package], package
    assert config["versions"]["python"].count(".") == 2
    assert {"git_commit", "git_dirty", "git_diff_sha256"} <= set(config)
    assert config["archive_uri"] == long_archive
    assert "do-not-print" not in (bundle[0] / "run_config.json").read_text()


# @spec EVAL-HOLD-019
def test_redaction_covers_named_secrets_and_url_passwords():
    from aqdt.evaluation.holdout import redact

    values = {
        "AQDT_PURPLEAIR_API_KEY": "abc",
        "aqdt_token": "t",
        "AQDT_DB_PASSWORD": "p",
        "AQDT_ARCHIVE_URI": "s3://bucket/prefix",
        "AQDT_DSN": "postgresql://user:hunter2@host:5432/db",
        "nested": {"client_secret": "s", "resolution_m": 100},
    }
    out = redact(values)
    assert out["AQDT_PURPLEAIR_API_KEY"] == out["aqdt_token"] == "<redacted>"
    assert out["AQDT_DB_PASSWORD"] == "<redacted>"
    assert out["AQDT_ARCHIVE_URI"] == "s3://bucket/prefix"
    assert out["AQDT_DSN"] == "postgresql://user:<redacted>@host:5432/db"
    assert out["nested"] == {"client_secret": "<redacted>", "resolution_m": 100}


# @spec EVAL-HOLD-020
def test_every_holdout_figure_is_a_300_dpi_png_with_its_csv_and_a_caption(bundle):
    from aqdt.evaluation import holdout_figures

    out = bundle[0]
    captions = (out / "figures.md").read_text()
    for number, name in enumerate(FIGURES, start=1):
        png, csv = out / f"{name}.png", out / f"{name}.csv"
        assert png.exists() and csv.exists(), name
        with Image.open(png) as image:
            dpi = image.info["dpi"]
            assert round(dpi[0]) == 300, name
            assert 3.3 - 0.05 <= image.size[0] / dpi[0] <= 6.5 + 0.3, name
        assert f"**Figure {number}.** `{name}.png`" in captions
    for name, labels in holdout_figures.AXIS_LABELS.items():
        for label in labels:
            assert "(" in label and ")" in label, (name, label)


# @spec EVAL-HOLD-021
def test_summary_states_the_results_and_caveats(bundle):
    text = (bundle[0] / "summary.md").read_text()
    for phrase in (
        "Completeness",
        "Development against test",
        "σ²_shared(D)",
        "Reliability",
        "Chosen forest",
        "reference only",
        "SHAP",
        "not a predictive variance",
        "indicative",
        "trailing windows",
    ):
        assert phrase in text, phrase


# @spec EVAL-HOLD-022
def test_pack_writes_a_tgz_with_the_directory_as_its_top_entry(bundle):
    out, summary, _ = bundle
    with tarfile.open(summary.archive, "r:gz") as tar:
        names = tar.getnames()
    assert {n.split("/")[0] for n in names} == {out.name}
    assert f"{out.name}/run_config.json" in names
    assert f"{out.name}/test/matchups.csv" in names


# @spec EVAL-HOLD-023
def test_a_second_run_writes_identical_csvs(bundle, long_archive, tmp_path):
    from aqdt.evaluation.holdout import holdout

    first = bundle[0]
    with pytest.MonkeyPatch.context() as mp:
        _environment(mp)
        summary = holdout(long_archive, DEV, TEST, tmp_path / "again", _settings())
    assert summary.archive is None
    names = sorted(p.relative_to(first) for p in first.rglob("*.csv"))
    assert names == sorted(p.relative_to(tmp_path / "again") for p in
                           (tmp_path / "again").rglob("*.csv"))
    for name in names:
        assert (first / name).read_bytes() == (tmp_path / "again" / name).read_bytes(), name
