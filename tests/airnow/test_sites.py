"""Site-identifier normalization — AN-SITE."""

import pytest

from aqdt.airnow.sites import normalize_aqs_code, normalize_site_id, site_id_for


# @spec AN-SITE-002
@pytest.mark.parametrize(
    "code, expected",
    [
        ("840110010043", "840110010043"),  # 12: as is
        ("84011001004", "840011001004"),  # 11: lost leading zero in the state
        ("110010043", "840110010043"),  # 9: full AQS code
        ("24031300", "840024031300"),  # 8: full code with lost leading zero
        ("1100100431", None),  # 10 digits
        ("1234567", None),  # 7 digits
        ("1100100430000", None),  # 13 digits
        ("11001004A", None),  # non-digit
        ("", None),
        (None, None),
    ],
)
def test_single_code_normalization_by_digit_count(code, expected):
    assert normalize_aqs_code(code) == expected


# @spec AN-SITE-008
def test_any_country_prefix_is_accepted_on_twelve_digits():
    assert normalize_aqs_code("124110010043") == "124110010043"
    assert normalize_aqs_code("110010043") == "840110010043"
    assert normalize_aqs_code("24031300") == "840024031300"


# @spec AN-SITE-001
# @spec AN-SITE-003
def test_both_codes_agreeing_resolve():
    assert normalize_site_id("110010043", "840110010043") == "840110010043"
    assert normalize_site_id("24031300", "84024031300") == "840024031300"


# @spec AN-SITE-004
def test_one_code_present_or_resolvable_is_used():
    assert normalize_site_id("110010045", None) == "840110010045"
    assert normalize_site_id(None, "840110010041") == "840110010041"
    assert normalize_site_id("garbage", "840110010041") == "840110010041"
    assert normalize_site_id("110010045", "garbage") == "840110010045"


# @spec AN-SITE-005
def test_conflicting_or_unresolvable_codes_are_unresolved():
    assert normalize_site_id("110010043", "840510130020") is None
    assert normalize_site_id("ABC", "XYZ") is None
    assert normalize_site_id(None, None) is None


# @spec AN-SITE-006
def test_resolved_site_id_is_namespaced_twelve_digits():
    site_id, resolved = site_id_for("110010043", "840110010043", "McMillan")
    assert (site_id, resolved) == ("airnow:840110010043", True)


# @spec AN-SITE-007
@pytest.mark.parametrize(
    "full, intl, name, expected",
    [
        ("110010043", "840510130020", "Conflict", "airnow:unresolved:840510130020"),
        ("ABC", "XYZ", "Garbage", "airnow:unresolved:XYZ"),
        ("ABC", None, "Garbage", "airnow:unresolved:ABC"),
        (None, None, "Nameless codes", "airnow:unresolved:Nameless codes"),
    ],
)
def test_unresolved_site_id_uses_intl_then_full_then_name(full, intl, name, expected):
    site_id, resolved = site_id_for(full, intl, name)
    assert (site_id, resolved) == (expected, False)
