"""Sensor-to-monitor matching — CAL-MATCH, CAL-IN-004."""

from pathlib import Path

import pandas as pd
import pytest
from pyproj import Geod

from aqdt.calibration.matching import match_references
from aqdt.observation_store.archive import read_sites

from .conftest import M1, M2, SENSORS

GEOD = Geod(ellps="WGS84")


def great_circle(a, b):
    return GEOD.inv(a[1], a[0], b[1], b[0])[2]


@pytest.fixture
def sites(archive_uri):
    return read_sites(archive_uri)


# @spec CAL-IN-004
# @spec CAL-MATCH-001
def test_every_sensor_is_matched_and_monitors_are_not(sites):
    matches = match_references(sites, eligible_reference_ids={M1[0]}, max_distance_m=10_000)
    assert set(matches["site_id"]) == set(SENSORS)
    assert M1[0] not in set(matches["site_id"]) and M2[0] not in set(matches["site_id"])
    assert "purpleair:5" in set(matches["site_id"])  # silent sensor still gets a match row


# @spec CAL-MATCH-002
def test_only_eligible_monitors_are_candidates(sites):
    with_m2 = match_references(sites, eligible_reference_ids={M1[0], M2[0]}, max_distance_m=10_000)
    without_m2 = match_references(sites, eligible_reference_ids={M1[0]}, max_distance_m=10_000)
    s4_with = with_m2.set_index("site_id").loc["purpleair:4"]
    s4_without = without_m2.set_index("site_id").loc["purpleair:4"]
    assert s4_with["ref_site_id"] == M2[0]
    assert s4_with["distance_m"] == pytest.approx(555.1, rel=1e-3)
    assert s4_without["ref_site_id"] == M1[0]
    assert s4_without["distance_m"] == pytest.approx(7861.7, rel=1e-3)


# @spec CAL-MATCH-003
def test_nearest_eligible_monitor_by_projected_distance(sites):
    matches = match_references(sites, eligible_reference_ids={M1[0], M2[0]}, max_distance_m=50_000)
    by_site = matches.set_index("site_id")
    for site_id, (lat, lon) in SENSORS.items():
        d1, d2 = great_circle((lat, lon), M1[1:]), great_circle((lat, lon), M2[1:])
        expected_ref, expected_d = (M1[0], d1) if d1 <= d2 else (M2[0], d2)
        assert by_site.loc[site_id, "ref_site_id"] == expected_ref, site_id
        assert by_site.loc[site_id, "distance_m"] == pytest.approx(expected_d, rel=1e-3), site_id
    assert by_site.loc["purpleair:1", "distance_m"] == pytest.approx(555.1, rel=1e-3)


# @spec CAL-MATCH-004
def test_no_eligible_monitor_within_range_means_no_match(sites):
    matches = match_references(sites, eligible_reference_ids={M1[0]}, max_distance_m=10_000)
    s3 = matches.set_index("site_id").loc["purpleair:3"]
    assert pd.isna(s3["ref_site_id"])
    assert pd.isna(s3["distance_m"])
    # bring the radius up and S3 matches M1 at ~44 km
    wide = match_references(sites, eligible_reference_ids={M1[0]}, max_distance_m=50_000)
    assert wide.set_index("site_id").loc["purpleair:3", "ref_site_id"] == M1[0]
    # no eligible monitors at all: nothing matches
    none = match_references(sites, eligible_reference_ids=set(), max_distance_m=10_000)
    assert none["ref_site_id"].isna().all()


# @spec CAL-MATCH-005
def test_matching_needs_no_database():
    package = Path(__file__).resolve().parents[2] / "src" / "aqdt" / "calibration"
    source = "\n".join(p.read_text() for p in package.rglob("*.py"))
    assert "psycopg" not in source
    assert "ST_DWithin" not in source and "ST_Distance" not in source
