"""AQS site-identifier normalization to the 12-digit international code.

Structure: 3-digit country + 2-digit state + 3-digit county + 4-digit site. AirNow sends the
code with and without the country prefix, occasionally with the state's leading zero lost.
"""

from __future__ import annotations

US_PREFIX = "840"
UNRESOLVED_PREFIX = "airnow:unresolved:"


# @spec AN-SITE-002, AN-SITE-008
def normalize_aqs_code(code: str | None) -> str | None:
    """One code by digit count: 12 as is; 11 → state zero restored; 9 → ``840`` prefixed;
    8 → ``840`` and state zero; anything else, or a non-digit, is unresolvable."""
    if code is None:
        return None
    code = code.strip()
    if not code.isdigit():
        return None
    match len(code):
        case 12:
            return code
        case 11:
            return code[:3] + "0" + code[3:]
        case 9:
            return US_PREFIX + code
        case 8:
            return US_PREFIX + "0" + code
        case _:
            return None


# @spec AN-SITE-001, AN-SITE-003, AN-SITE-004, AN-SITE-005
def normalize_site_id(full_aqs_code: str | None, intl_aqs_code: str | None) -> str | None:
    """The normalized code both identifiers agree on, or the one that resolves; ``None`` when
    they conflict or neither resolves."""
    full = normalize_aqs_code(full_aqs_code)
    intl = normalize_aqs_code(intl_aqs_code)
    if full is not None and intl is not None:
        return full if full == intl else None
    return full if full is not None else intl


def _first_present(*candidates: str | None) -> str:
    return next((c for c in candidates if c), "")


def unresolved_key(
    full_aqs_code: str | None, intl_aqs_code: str | None, site_name: str | None
) -> str:
    """The unresolved-site key: ``IntlAQSCode`` as received, else ``FullAQSCode``, else name."""
    return _first_present(intl_aqs_code, full_aqs_code, site_name)


def source_native_id(
    full_aqs_code: str | None, intl_aqs_code: str | None, site_name: str | None
) -> str:
    """The site's identifier as the source returned it: ``FullAQSCode``, else ``IntlAQSCode``,
    else name."""
    return _first_present(full_aqs_code, intl_aqs_code, site_name)


# @spec AN-SITE-006, AN-SITE-007
def site_id_for(
    full_aqs_code: str | None, intl_aqs_code: str | None, site_name: str | None
) -> tuple[str, bool]:
    """The canonical ``site_id`` and whether the site resolved."""
    normalized = normalize_site_id(full_aqs_code, intl_aqs_code)
    if normalized is not None:
        return f"airnow:{normalized}", True
    return UNRESOLVED_PREFIX + unresolved_key(full_aqs_code, intl_aqs_code, site_name), False
