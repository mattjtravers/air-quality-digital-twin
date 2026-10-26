"""evaluate end to end over the synthetic archive — EVAL-IN, MATCH, AVAIL, CALSE, TOTVAR, MC,
DECOMP, SENS, BUDGET, FIG, RUN."""

import hashlib
import json
from datetime import timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from PIL import Image
from pydantic import ValidationError

from aqdt.calibration.products import FITS
from aqdt.evaluation import budget
from aqdt.evaluation import run as run_module
from aqdt.evaluation.run import evaluate
from aqdt.fusion.products import SURFACE_FITS
from aqdt.observation_store.archive import read_partitioned

from .conftest import (
    EVAL_WINDOW,
    FLAGGED,
    H10,
    H13,
    H14,
    INSIDE,
    KRIGED,
    NULL_VALUE,
    OUTSIDE_MONITOR,
    eval_settings,
)

FILES = [
    "matchups.csv",
    "metrics.csv",
    "availability.csv",
    "fit_uncertainty.csv",
    "montecarlo.csv",
    "lomo.csv",
    "lomo_by_monitor.csv",
    "baselines.csv",
    "baseline_paired.csv",
    "decomposition.csv",
    "variogram_sensitivity.csv",
    "variogram.csv",
    "coverage_diagnostics.csv",
    "reliability.csv",
    "conditional_bias.csv",
    "spike_points.csv",
    "spikes.csv",
    "pooling.csv",
    "per_monitor.csv",
    "lag.csv",
    "error_budget.csv",
    "figures.md",
    "summary.md",
    "run.json",
]
FIGURES = [
    "timeseries",
    "scatter",
    "coverage",
    "bias_vs_pooled",
    "decomposition",
    "mc_vs_analytic",
    "error_budget",
    "lomo_rmse",
    "baselines_rmse",
    "reliability",
    "qq_ztotal",
    "maps_pm25",
    "maps_sd",
    "maps_pm25_10",
]


def _csv(report, name):
    return pd.read_csv(report[0] / name)


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


# --- Inputs and the read-only guarantee --------------------------------------------------------


# @spec EVAL-IN-001
def test_inputs_come_from_the_archive_and_never_postgis():
    package = Path(run_module.__file__).parent
    source = "\n".join(p.read_text() for p in package.glob("*.py")).lower()
    assert "psycopg" not in source and "cursor(" not in source
    assert "purpleair.client" not in source and "airnow.client" not in source


# @spec EVAL-IN-002, EVAL-RUN-004
def test_writes_nothing_to_the_archive_and_is_deterministic(archive, report, tmp_path, cal_env):
    before = _tree(Path(archive))
    evaluate(archive, *EVAL_WINDOW, tmp_path, eval_settings())
    assert _tree(Path(archive)) == before
    for name in [f for f in FILES if f.endswith(".csv")] + [f"{f}.csv" for f in FIGURES]:
        assert (tmp_path / name).read_bytes() == (report[0] / name).read_bytes(), name


# @spec EVAL-IN-003
def test_window_bounds_are_validated(archive, tmp_path):
    naive = EVAL_WINDOW[0].replace(tzinfo=None)
    with pytest.raises((ValueError, ValidationError)):
        evaluate(archive, naive, EVAL_WINDOW[1], tmp_path, eval_settings())
    with pytest.raises(ValueError):
        evaluate(archive, H10, H10 + timedelta(minutes=30), tmp_path, eval_settings())


# @spec EVAL-IN-003
def test_window_bounds_are_converted_and_truncated(archive, tmp_path, cal_env):
    est = timezone(timedelta(hours=-5))
    start = (H10 + timedelta(minutes=20)).astimezone(est)
    summary = evaluate(archive, start, H10 + timedelta(hours=1), tmp_path, eval_settings())
    assert summary.window_start == H10
    assert set(pd.to_datetime(_csv((tmp_path,), "matchups.csv")["hour"], utc=True)) == {H10}


# --- Matchups ---------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def matchups(report):
    frame = _csv(report, "matchups.csv")
    frame["hour"] = pd.to_datetime(frame["hour"], utc=True)
    return frame


# @spec EVAL-MATCH-001
def test_one_matchup_per_valued_monitor_inside_the_grid_per_kriged_hour(matchups):
    expected = {(h, m) for h in KRIGED for m in INSIDE} - {(NULL_VALUE[1], NULL_VALUE[0])}
    assert set(zip(matchups["hour"], matchups["site_id"])) == expected
    assert OUTSIDE_MONITOR not in set(matchups["site_id"])
    assert H13 not in set(matchups["hour"]) and H14 not in set(matchups["hour"])


# @spec EVAL-MATCH-002, EVAL-MATCH-003
def test_matchup_columns_are_the_raster_cell_and_derived_errors(archive, matchups):
    from pyproj import Transformer

    from aqdt.evaluation.matchups import cell_index
    from aqdt.maps.surface import read_surface

    fits = read_partitioned(archive, SURFACE_FITS).set_index("hour")
    row = matchups.iloc[0]
    surface = read_surface(fits.loc[row["hour"], "surface_uri"])
    from .conftest import MONITORS

    lat, lon = MONITORS[row["site_id"]]
    x, y = Transformer.from_crs(4326, 26918, always_xy=True).transform(lon, lat)
    height, width = surface.pm25.shape
    r, c = cell_index(surface.transform, width, height, x, y)
    assert row["surface"] == pytest.approx(float(surface.pm25[r, c]))
    assert row["variance"] == pytest.approx(float(surface.variance[r, c]))
    assert row["sd"] == pytest.approx(np.sqrt(row["variance"]))
    assert row["error"] == pytest.approx(row["surface"] - row["obs"])
    assert row["z"] == pytest.approx(row["error"] / row["sd"])
    flagged = matchups[(matchups["site_id"] == FLAGGED[0]) & (matchups["hour"] == FLAGGED[1])]
    assert flagged["flagged"].item() and "flatline" in flagged["qc_flags"].item()
    assert matchups["flagged"].sum() == 1


# @spec EVAL-MATCH-004, EVAL-MATCH-005
def test_kriging_inputs_are_rebuilt_with_fit_status(archive, matchups):
    fits = read_partitioned(archive, SURFACE_FITS).set_index("hour")
    for hour, group in matchups.groupby("hour"):
        assert (group["n_sensors"] == fits.loc[hour, "n_sensors"]).all()
        assert group["inputs_reproduced"].all()
    assert (matchups["pooled_share"] + matchups["fitted_share"]).to_numpy() == pytest.approx(1.0)
    assert matchups["pooled_share"].between(0.3, 0.8).all()
    assert (matchups["nearest_sensor_km"] > 0).all() and (matchups["nearest_sensor_km"] < 30).all()


# @spec EVAL-TOTVAR-003
def test_total_variance_adds_the_calibration_term(matchups):
    assert (matchups["cal_variance"] > 0).all()
    assert matchups["total_variance"].to_numpy() == pytest.approx(
        (matchups["variance"] + matchups["cal_variance"]).to_numpy()
    )
    assert matchups["total_sd"].to_numpy() == pytest.approx(np.sqrt(matchups["total_variance"]))
    assert matchups["z_total"].to_numpy() == pytest.approx(
        (matchups["error"] / matchups["total_sd"]).to_numpy()
    )


# @spec EVAL-MATCH-006
def test_a_raster_that_does_not_match_its_row_is_still_scored(archive, tmp_path, cal_env):
    import shutil

    copy = tmp_path / "archive"
    shutil.copytree(archive, copy)
    fits = read_partitioned(str(copy), SURFACE_FITS).set_index("hour")
    raster = Path(fits.loc[H10, "surface_uri"].replace(archive, str(copy)))
    assert raster.exists()
    fits_dir = copy / "fusion" / "surface_fits"
    for parquet in fits_dir.rglob("*.parquet"):
        frame = pd.read_parquet(parquet)
        frame["surface_uri"] = frame["surface_uri"].str.replace(archive, str(copy), regex=False)
        frame.loc[frame["hour"] == H10, "surface_sha256"] = hashlib.sha256(b"other").hexdigest()
        frame.to_parquet(parquet)
    out = tmp_path / "out"
    evaluate(str(copy), H10, H10 + timedelta(hours=1), out, eval_settings())
    assert len(_csv((out,), "matchups.csv")) == 4
    assert "sha256" in (out / "summary.md").read_text().lower()


# --- Availability -----------------------------------------------------------------------------


# @spec EVAL-AVAIL-001, EVAL-AVAIL-003
def test_summary_reports_hours_days_gaps_and_non_kriged_hours(report):
    out, summary = report
    assert summary.kriged_hours == 3 and summary.days == 1
    text = (out / "summary.md").read_text()
    assert "2026-09-14T13" in text and "insufficient_points" in text
    assert "2026-09-14T14" in text  # no surface_fits row: a gap


# @spec EVAL-AVAIL-002
def test_availability_counts_per_monitor(report):
    table = _csv(report, "availability.csv").set_index("site_id")
    assert not table.loc[OUTSIDE_MONITOR, "inside_grid"]
    assert table.loc[OUTSIDE_MONITOR, "matchups"] == 0
    assert table.loc[NULL_VALUE[0], "null_value"] == 1
    assert table.loc[FLAGGED[0], "flagged"] == 1
    assert table.loc[INSIDE[0], "matchups"] == 3
    assert (table.loc[INSIDE, "calibration_targets"] >= 0).all()
    assert table["calibration_targets"].sum() > 0
    assert "median_target_distance_m" in table.columns
    assert "nan_cell" in table.columns


# @spec EVAL-AVAIL-004
def test_a_window_with_no_kriged_hour_still_writes_a_report(archive, tmp_path, cal_env):
    summary = evaluate(archive, H13, H14 + timedelta(hours=1), tmp_path, eval_settings())
    assert summary.kriged_hours == 0
    assert len(_csv((tmp_path,), "matchups.csv")) == 0
    assert "no hour was kriged" in (tmp_path / "summary.md").read_text().lower()
    assert not (tmp_path / "maps_pm25.png").exists()
    for name in ("lomo.csv", "baselines.csv", "baseline_paired.csv", "spikes.csv",
                 "spike_points.csv", "pooling.csv", "reliability.csv", "per_monitor.csv"):
        assert len(_csv((tmp_path,), name)) == 0, name
    for name in FILES:
        assert (tmp_path / name).exists(), name
    assert not (tmp_path / "maps_pm25_10.png").exists()


# --- Calibration uncertainty ------------------------------------------------------------------


# @spec EVAL-CALSE-001, EVAL-CALSE-005
def test_fit_uncertainty_rebuilds_the_stored_fits(archive, report):
    table = _csv(report, "fit_uncertainty.csv")
    stored = read_partitioned(archive, FITS)
    assert set(table["status"]) == {"fitted", "pooled"}
    assert (table["status"] == "pooled").sum() == 1  # one pooled fit per fit_as_of
    for column in (
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
        "stored_n_pairs",
        "stored_slope",
        "stored_intercept",
        "slope",
        "intercept",
    ):
        assert column in table.columns, column
    assert table["slope"].to_numpy() == pytest.approx(table["stored_slope"].to_numpy(), rel=1e-9)
    assert table["intercept"].to_numpy() == pytest.approx(
        table["stored_intercept"].to_numpy(), rel=1e-9, abs=1e-12
    )
    assert (table["n"] == table["stored_n_pairs"]).all()
    fitted_sites = set(stored.loc[stored["status"] == "fitted", "site_id"])
    assert set(table.loc[table["status"] == "fitted", "site_id"]) == fitted_sites


# @spec EVAL-CALSE-003
def test_corrected_covariance_is_at_least_the_uncorrected(report):
    table = _csv(report, "fit_uncertainty.csv")
    assert (table["var_b"] >= table["var_b_raw"] - 1e-15).all()
    assert table["rho1"].between(0, 0.99).all()


# @spec EVAL-MC-001, EVAL-MC-003
def test_monte_carlo_reports_analytic_and_both_propagations(report):
    table = _csv(report, "montecarlo.csv")
    assert table["hour"].nunique() == 2
    for column in ("analytic", "monte_carlo", "monte_carlo_independent"):
        assert (table[column] > 0).all()
    ratio = table["monte_carlo"] / table["analytic"]
    assert ratio.median() == pytest.approx(1.0, abs=0.5)


# @spec EVAL-DECOMP-001, EVAL-DECOMP-002, EVAL-DECOMP-003
def test_decomposition_shares_by_status_and_bound(report):
    table = _csv(report, "decomposition.csv")
    assert set(table["group"]) == {"overall", "fitted", "pooled"}
    assert set(table["reference"]) == {"T640", "BAM-1020"}
    shares = table[["share_a", "share_b", "share_c"]].sum(axis=1)
    assert (shares <= 1 + 1e-9).all()
    assert (table[["share_a", "share_b", "share_c"]] >= 0).all().all()
    assert "clamped" in table.columns


# @spec EVAL-SENS-001
def test_variogram_sensitivity_per_kriged_hour(report):
    table = _csv(report, "variogram_sensitivity.csv")
    assert len(table) == 3
    assert {"hour", "selected", "alternative", "rms_difference"} <= set(table.columns)
    assert (table["selected"] != table["alternative"]).all()


# --- Error budget, figures, summary -----------------------------------------------------------


# @spec EVAL-BUDGET-001, EVAL-BUDGET-003
def test_error_budget_is_written_with_run_magnitudes(report):
    table = _csv(report, "error_budget.csv")
    assert len(table) >= 11
    quantified = table[table["handling"] != "listed-not-modeled"]
    assert quantified["magnitude"].str.contains("µg/m³").all()


# @spec EVAL-FIG-001, EVAL-FIG-002
def test_every_figure_is_a_300_dpi_png_with_its_csv(report):
    out = report[0]
    for name in FIGURES:
        png, csv = out / f"{name}.png", out / f"{name}.csv"
        assert png.exists() and csv.exists(), name
        with Image.open(png) as image:
            dpi = image.info["dpi"]
            assert round(dpi[0]) == 300, name
            width_in = image.size[0] / dpi[0]
            assert 3.3 - 0.05 <= width_in <= 6.5 + 0.3, (name, width_in)


# @spec EVAL-FIG-002, EVAL-FIG-003
def test_figures_label_axes_with_units():
    from aqdt.evaluation import figures

    for name, labels in figures.AXIS_LABELS.items():
        for label in labels:
            assert "(" in label and ")" in label, (name, label)


# @spec EVAL-FIG-004, EVAL-FIG-005
def test_maps_and_clipping_are_reported(report):
    out = report[0]
    hours = _csv(report, "maps_pm25.csv")
    assert hours["hour"].nunique() == 3  # fewer than four kriged hours: all of them
    text = (out / "summary.md").read_text()
    assert "pm25_max" in text and "sd_max" in text


# @spec EVAL-RUN-001, EVAL-RUN-002, EVAL-BASE-006, EVAL-SPLIT-005
def test_report_files_and_summary_contents(report):
    out, summary = report
    for name in FILES:
        assert (out / name).exists(), name
    assert set(summary.headline) >= {"n", "bias", "rmse", "cov2_krig", "cov2_total"}
    text = (out / "summary.md").read_text().lower()
    for phrase in (
        "preliminary",
        "weather",
        "calibration targets",
        "nugget",
        "variogram uncertainty",
        "unflagged",
        "monte carlo",
        "leave one monitor out",
        "baselines",
        "idw",
        "random forest",
        "r² (squared correlation)",
        "r² (skill, 1 − sse/ss_obs)",
        "observed-conditioned",
        "spatial hold-out",
        "no baseline is built from a monitor value",
    ):
        assert phrase in text, phrase


# @spec EVAL-RUN-005, EVAL-FLAG-001, EVAL-UNC-004, EVAL-SPLIT-005
def test_summary_opens_with_the_plain_answers(report):
    text = (report[0] / "summary.md").read_text()
    head = text.split("## ")[1].lower()
    assert head.startswith("plain answers")
    for phrase in ("baselines", "leave one monitor out", "predicted level", "inflation",
                   "spike", "pooled"):
        assert phrase in head, phrase
    lower = text.lower()
    assert "flagged share of airnow observations" in lower and "flagged share of matchups" in lower
    assert "λᵀ m σ_s mᵀ λ" in lower and "n / n_eff" in lower


# @spec EVAL-RUN-003
def test_run_json_records_provenance(report, archive):
    meta = json.loads((report[0] / "run.json").read_text())
    assert meta["archive_uri"] == archive
    assert meta["seed"] == 0
    assert {"window_start", "window_end", "evaluation_settings", "map_settings", "version"} <= set(
        meta
    )
    for key in ("git_commit", "git_dirty", "git_diff_sha256", "matchups_sha256", "airnow_objects",
                "airnow_last_modified", "airnow_manifest_sha256"):
        assert key in meta, key
    digest = hashlib.sha256((report[0] / "matchups.csv").read_bytes()).hexdigest()
    assert meta["matchups_sha256"] == digest
    assert meta["airnow_objects"] > 0
    assert meta["airnow_last_modified"] is None  # a local archive reports no modification time
    assert len(meta["airnow_manifest_sha256"]) == 64


# --- Leave one monitor out --------------------------------------------------------------------

LOMO_COLUMNS = [
    "hour",
    "site_id",
    "obs",
    "flagged",
    "stored_error",
    "control_error",
    "lomo_error",
    "n_fitted_to_x",
    "n_to_pooled",
    "n_to_other_monitor",
    "n_uncalibrated",
    "n_changed",
    "nearest_sensor_km",
    "pooled_share",
]


@pytest.fixture(scope="module")
def lomo_rows(report):
    frame = _csv(report, "lomo.csv")
    frame["hour"] = pd.to_datetime(frame["hour"], utc=True)
    return frame


# @spec EVAL-LOMO-001, EVAL-LOMO-002, EVAL-LOMO-004
def test_lomo_has_one_row_per_selected_hour_and_matchup_monitor(lomo_rows, matchups):
    assert list(lomo_rows.columns) == LOMO_COLUMNS
    assert lomo_rows["hour"].nunique() == 2  # lomo_hours = 2 in eval_settings
    expected = matchups[matchups["hour"].isin(set(lomo_rows["hour"]))]
    assert sorted(zip(lomo_rows["hour"], lomo_rows["site_id"])) == sorted(
        zip(expected["hour"], expected["site_id"])
    )
    merged = lomo_rows.merge(matchups, on=["hour", "site_id"], suffixes=("", "_m"))
    assert merged["stored_error"].to_numpy() == pytest.approx(merged["error"].to_numpy())
    assert (merged["flagged"] == merged["flagged_m"]).all()


# @spec EVAL-LOMO-002, EVAL-LOMO-003
def test_the_control_reproduces_the_stored_surface(lomo_rows):
    # unchanged archive: the control refit is the stored fit; only point vs cell and the
    # variogram refit separate the control from the stored surface
    assert lomo_rows["control_error"].notna().all()
    gap = (lomo_rows["control_error"] - lomo_rows["stored_error"]).abs()
    assert gap.max() < 1.0


# @spec EVAL-LOMO-002, EVAL-LOMO-004
def test_removing_a_monitor_moves_the_sensors_fitted_to_it(archive, lomo_rows):
    fits = read_partitioned(archive, FITS)
    fitted = fits[fits["status"] == "fitted"]
    for row in lomo_rows.itertuples():
        assert row.n_fitted_to_x == (fitted["ref_site_id"] == row.site_id).sum()
        assert row.n_changed >= row.n_fitted_to_x
        if row.n_fitted_to_x == 0:
            assert row.lomo_error == pytest.approx(row.control_error)
    assert lomo_rows["n_fitted_to_x"].sum() > 0
    moved = lomo_rows[lomo_rows["n_fitted_to_x"] > 0]
    assert (moved["lomo_error"] != moved["control_error"]).any()
    went = lomo_rows["n_to_pooled"] + lomo_rows["n_to_other_monitor"] + lomo_rows["n_uncalibrated"]
    assert (went == lomo_rows["n_fitted_to_x"]).all()


# @spec EVAL-LOMO-004, EVAL-LOMO-007
def test_lomo_rows_carry_the_matchup_context_and_a_per_monitor_table(report, lomo_rows, matchups):
    merged = lomo_rows.merge(matchups, on=["hour", "site_id"], suffixes=("", "_m"))
    assert merged["nearest_sensor_km"].to_numpy() == pytest.approx(
        merged["nearest_sensor_km_m"].to_numpy()
    )
    assert merged["pooled_share"].to_numpy() == pytest.approx(merged["pooled_share_m"].to_numpy())
    by_monitor = _csv(report, "lomo_by_monitor.csv")
    assert list(by_monitor.columns) == ["site_id", "n", "median_d", "mean_n_fitted_to_x"]
    assert set(by_monitor["site_id"]) <= set(lomo_rows["site_id"])


# @spec EVAL-LOMO-001, EVAL-BUDGET-003
def test_lomo_hours_zero_skips_it_and_leaves_its_budget_row_unquantified(
    archive, tmp_path, cal_env
):
    evaluate(archive, *EVAL_WINDOW, tmp_path, eval_settings(lomo_hours=0))
    table = _csv((tmp_path,), "lomo.csv")
    assert list(table.columns) == LOMO_COLUMNS and len(table) == 0
    budget_rows = _csv((tmp_path,), "error_budget.csv").set_index("source")
    assert budget_rows.loc[budget.DEPENDENCE, "magnitude"] == "not quantified"


# @spec EVAL-BUDGET-002, EVAL-BUDGET-003, EVAL-LOMO-004
def test_the_dependence_row_carries_the_lomo_median(report):
    budget_rows = _csv(report, "error_budget.csv").set_index("source")
    row = budget_rows.loc[budget.DEPENDENCE]
    assert row["handling"] == "quantified-diagnostic"
    assert "µg/m³" in row["magnitude"] and "lomo.csv" in row["magnitude"]


# --- Baselines --------------------------------------------------------------------------------


# @spec EVAL-BASE-001, EVAL-BASE-002, EVAL-BASE-003
def test_baselines_are_matchup_columns_and_a_comparison_table(report, matchups):
    for column in ("idw", "rf", "hour_mean", "hour_median"):
        assert matchups[column].notna().all(), column
    assert ((matchups["idw"] - matchups["obs"]).abs() < 10).all()
    per_hour = matchups.groupby("hour")[["hour_mean", "points_mean"]].nunique()
    assert (per_hour == 1).all().all()
    assert matchups["hour_mean"].to_numpy() == pytest.approx(matchups["points_mean"].to_numpy())
    table = _csv(report, "baselines.csv")
    overall_rows = table[table["split"] == "overall"].set_index(["method", "subset"])
    assert set(overall_rows.index) == {
        (m, s)
        for m in ("kriging", "hour_mean", "hour_median", "idw", "rf")
        for s in ("unflagged", "all")
    }
    headline = _csv(report, "metrics.csv")
    overall = headline[(headline["subset"] == "unflagged") & (headline["split"] == "overall")]
    kriging = overall_rows.loc[("kriging", "unflagged")]
    assert kriging["n"] == overall["n"].iloc[0]
    assert kriging["rmse"] == pytest.approx(overall["rmse"].iloc[0])
    assert overall_rows.loc[("idw", "all"), "n"] == len(matchups)
    paired = _csv(report, "baseline_paired.csv")
    assert set(paired["baseline"]) == {"hour_mean", "hour_median", "idw", "rf"}


# --- Tightened evaluation -----------------------------------------------------------------------


# @spec EVAL-MATCH-007, EVAL-UNC-001
def test_matchups_carry_points_mean_pooled_share_10km_and_raw_calibration_variance(matchups):
    for column in ("points_mean", "pooled_share_10km", "cal_variance_raw", "z_total_raw"):
        assert column in matchups.columns, column
    assert matchups["points_mean"].notna().all()
    share = matchups["pooled_share_10km"].dropna()
    assert ((share >= 0) & (share <= 1)).all()
    both = matchups.dropna(subset=["cal_variance", "cal_variance_raw"])
    assert len(both) and (both["cal_variance_raw"] <= both["cal_variance"] + 1e-12).all()
    expected = both["error"] / np.sqrt(both["variance"] + both["cal_variance_raw"])
    assert both["z_total_raw"].to_numpy() == pytest.approx(expected.to_numpy())


# @spec EVAL-METRIC-003, EVAL-SPLIT-002, EVAL-SPLIT-004
def test_metric_table_has_pearson_r2_new_splits_and_interval_columns(report):
    table = _csv(report, "metrics.csv")
    columns = list(table.columns)
    assert columns.index("pearson_r2") == columns.index("r2") + 1
    assert "rmse_ci_low" in columns and "cov2_total_raw_ci_high" in columns
    assert {"pred_level", "hour_mean_points", "hour_mean_obs", "spike_hour", "spike_day",
            "variogram", "monitor"} <= set(table["split"])
    assert table["rmse_ci_low"].isna().all()  # the synthetic window is a single day


# @spec EVAL-COND-001, EVAL-COND-002, EVAL-UNC-002, EVAL-UNC-003, EVAL-UNC-004
def test_conditional_bias_and_coverage_diagnostics_are_written(report):
    reliability = _csv(report, "reliability.csv")
    assert reliability["n"].sum() == (~_csv(report, "matchups.csv")["flagged"]).sum()
    slopes = _csv(report, "conditional_bias.csv")
    assert set(slopes["slope"]) == {"obs_on_surface", "surface_on_obs"}
    coverage = _csv(report, "coverage_diagnostics.csv")
    assert list(coverage["kind"]) == ["z", "z_total_raw", "z_total", "z_total_shared"]
    assert "shared_variance" in coverage.columns


# @spec EVAL-SPIKE-001, EVAL-SPIKE-002, EVAL-SPIKE-003
def test_a_window_without_spikes_writes_empty_spike_tables(report, matchups):
    points = _csv(report, "spike_points.csv")
    assert list(points.columns) == ["hour", "site_ids", "z", "median", "mad"] and len(points) == 0
    rows = _csv(report, "spikes.csv")
    assert len(rows) == 0 and "delta" in rows.columns
    assert set(matchups["spike_hour"]) == {"other"} and set(matchups["spike_day"]) == {"other"}


# @spec EVAL-POOL-001, EVAL-MON-001, EVAL-VARIO-001, EVAL-LAG-001, EVAL-IN-004
def test_further_diagnostics_are_written(report, matchups):
    pooling = _csv(report, "pooling.csv")
    assert list(pooling["model"]) == ["day", "day+hour_of_day"]
    per_monitor = _csv(report, "per_monitor.csv")
    assert set(per_monitor["site_id"]) == set(INSIDE)
    variogram = _csv(report, "variogram.csv")
    counts = variogram[variogram["quantity"] == "class_count"]["value"].sum()
    assert counts == len(KRIGED)
    lag = _csv(report, "lag.csv").set_index("lag")
    assert list(lag.index) == [-1, 0, 1]
    assert lag.loc[0, "n"] == (~matchups["flagged"]).sum()
    # the reads extend an hour past the window, but matchups stay inside it
    assert set(matchups["hour"]) <= set(KRIGED)
    assert lag.loc[-1, "n"] > 0


# @spec EVAL-FIG-006, EVAL-FIG-007
def test_figures_md_numbers_a_caption_for_every_figure(report):
    text = (report[0] / "figures.md").read_text()
    for k, name in enumerate(FIGURES, start=1):
        assert f"Figure {k}" in text and f"{name}.png" in text, name
