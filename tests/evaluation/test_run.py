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
    "decomposition.csv",
    "variogram_sensitivity.csv",
    "error_budget.csv",
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
    "maps_pm25",
    "maps_sd",
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


# @spec EVAL-RUN-001, EVAL-RUN-002
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
    ):
        assert phrase in text, phrase


# @spec EVAL-RUN-003
def test_run_json_records_provenance(report, archive):
    meta = json.loads((report[0] / "run.json").read_text())
    assert meta["archive_uri"] == archive
    assert meta["seed"] == 0
    assert {"window_start", "window_end", "evaluation_settings", "map_settings", "version"} <= set(
        meta
    )
    assert "git_commit" in meta
