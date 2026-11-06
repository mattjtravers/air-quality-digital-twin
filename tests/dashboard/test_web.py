"""The front end — DASH-WEB, DASH-TEST. Static checks on the web assets, then a smoke test that
loads a built site in a real browser."""

from __future__ import annotations

import contextlib
import functools
import http.server
import json
import re
import shutil
import threading
from pathlib import Path

import pytest

from .conftest import H13, H14

WEB = Path(__file__).resolve().parents[2] / "src" / "aqdt" / "dashboard" / "web"
LEAFLET = "https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/"
#: a 1×1 transparent PNG, answered for every basemap tile
BLANK_TILE = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000001e221bc330000000049454e44ae426082"
)


def html() -> str:
    return (WEB / "index.html").read_text()


def script() -> str:
    return (WEB / "app.js").read_text()


# --- Static checks ----------------------------------------------------------------------------


# @spec DASH-WEB-001
def test_leaflet_is_pinned_with_integrity_and_osm_and_esri_are_attributed():
    page = html()
    for asset in ("leaflet.min.js", "leaflet.min.css"):
        tag = re.search(r"<[^>]*%s[^>]*>" % re.escape(LEAFLET + asset), page)
        assert tag, asset
        assert re.search(r'integrity="sha(384|512)-[A-Za-z0-9+/=]+"', tag.group(0)), asset
        assert 'crossorigin="anonymous"' in tag.group(0), asset
    code = script()
    assert "tile.openstreetmap.org/{z}/{x}/{y}.png" in code
    assert "openstreetmap.org/copyright" in code and "OpenStreetMap" in code
    assert "server.arcgisonline.com/ArcGIS/rest/services/Reference/" in code
    assert "World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}" in code
    assert "esri.com" in code and "Esri" in code
    assert not list(WEB.glob("package*.json")), "no build step"


# @spec DASH-WEB-002
def test_manifest_is_fetched_past_the_http_cache():
    code = script()
    fetch = r"fetch\(\s*['\"]data/manifest\.json['\"]\s*,\s*\{[^}]*"
    assert re.search(fetch + r"cache:\s*['\"]no-cache['\"]", code)
    assert "opacity: 0.85" in code


# @spec DASH-WEB-005, DASH-WEB-006
def test_playback_speed_and_eastern_time():
    code = script()
    assert re.search(r"\b500\b", code)
    assert "America/New_York" in code and "Intl.DateTimeFormat" in code


# @spec DASH-WEB-013
def test_about_panel():
    page = html()
    about = re.search(r"<details[^>]*>.*?</details>", page, re.S)
    assert about, "a collapsible About panel"
    text = about.group(0)
    for phrase in (
        "PurpleAir",
        "AirNow",
        "Barkjohn",
        "preliminary",
        "not for regulatory or health decisions",
        "https://github.com/mattjtravers/air-quality-digital-twin",
    ):
        assert phrase in text, phrase
    assert "uncertainty" in text.lower() and "standard deviation" in text.lower()


# @spec DASH-WEB-014
def test_page_is_built_for_phones_and_assistive_technology():
    page = html()
    assert '<meta name="viewport" content="width=device-width, initial-scale=1"' in page
    assert '<html lang="en">' in page
    assert ":focus-visible" in (WEB / "style.css").read_text()
    assert 'type="range"' in page and "aria-label" in page


# --- Smoke test in a browser ------------------------------------------------------------------


@pytest.fixture(scope="module")
def browser():
    sync_api = pytest.importorskip("playwright.sync_api")
    with sync_api.sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except Exception as error:  # Chromium or its system libraries are not installed
            pytest.skip(f"Playwright's Chromium is unavailable: {error}".splitlines()[0])
        yield browser
        browser.close()


@contextlib.contextmanager
def _serve(directory: Path):
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()


@pytest.fixture(scope="module")
def served(site):
    out, _ = site
    with _serve(out) as url:
        yield url


@pytest.fixture(scope="module")
def crowded(site, tmp_path_factory):
    """The built site with its manifest edited: in every hour the first three monitors stand on
    one spot, and the first two hours are restamped to an evening hour (a UTC date ahead of the
    Eastern one) and to the autumn hour that daylight saving ends in."""
    out, _ = site
    copy = tmp_path_factory.mktemp("crowded") / "site"
    shutil.copytree(out, copy)
    path = copy / "data" / "manifest.json"
    manifest = json.loads(path.read_text())
    for hour in manifest["hours"]:
        first, second, third = hour["monitors"][:3]
        second["lat"], second["lon"] = first["lat"], first["lon"] + 0.0003
        third["lat"], third["lon"] = first["lat"], first["lon"]
    manifest["hours"][0]["hour"] = "2026-09-15T01:00:00Z"
    manifest["hours"][1]["hour"] = "2026-11-01T05:00:00Z"
    path.write_text(json.dumps(manifest))
    with _serve(copy) as url:
        yield url


def _open(browser, url, *, width=1200, height=900):
    page = browser.new_page(viewport={"width": width, "height": height})
    for tiles in (r"https://[a-z.]*tile\.openstreetmap\.org/.*", r"https://server\.arcgisonline\.com/.*"):
        page.route(
            re.compile(tiles),
            lambda route: route.fulfill(status=200, content_type="image/png", body=BLANK_TILE),
        )
    errors: list[str] = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url)
    page.wait_for_function("window.dashboardReady === true", timeout=20_000)
    return page, errors


def _overlay_src(page) -> str | None:
    return page.evaluate(
        "() => { const i = document.querySelector('img.leaflet-image-layer');"
        " return i ? i.getAttribute('src') : null; }"
    )


# @spec DASH-TEST-002, DASH-WEB-002, DASH-WEB-003, DASH-WEB-005, DASH-WEB-006, DASH-WEB-007
def test_frames_load_the_slider_moves_and_the_toggle_switches(browser, served):
    page, errors = _open(browser, served)
    # opens on the latest hour (12:00), its estimate frame loaded
    assert _overlay_src(page).endswith("pm25_2026-09-14T12.png")
    assert page.evaluate("() => document.querySelector('img.leaflet-image-layer').naturalWidth") > 0
    assert page.evaluate("() => window.dashboardPreloaded") == 9
    label = page.locator("#hour-label").inner_text()
    assert label == "Mon, Sep 14 · 8:00 AM–9:00 AM EDT", label

    slider = page.locator("#slider")
    slider.focus()
    page.keyboard.press("Home")
    assert _overlay_src(page).endswith("pm25_2026-09-14T10.png")
    assert page.locator("#hour-label").inner_text() == "Mon, Sep 14 · 6:00 AM–7:00 AM EDT"

    toggle = page.locator(".kind button").all_inner_texts()
    assert toggle == ["Estimate", "Difference", "Uncertainty"]
    page.locator("#kind-diff").click()
    assert page.locator("#kind-diff").get_attribute("aria-pressed") == "true"
    assert _overlay_src(page).endswith("diff_2026-09-14T10.png")
    assert "Above / below this hour's average (µg/m³)" in page.locator("#legend").inner_text()

    page.locator("#kind-sd").click()
    assert page.locator("#kind-sd").get_attribute("aria-pressed") == "true"
    assert page.locator("#kind-pm25").get_attribute("aria-pressed") == "false"
    assert page.locator("#kind-diff").get_attribute("aria-pressed") == "false"
    assert _overlay_src(page).endswith("sd_2026-09-14T10.png")
    alt = page.locator("img.leaflet-image-layer").get_attribute("alt")
    assert "ncertainty" in alt and "6:00 AM" in alt

    page.locator("#play").click()
    page.wait_for_function(
        "() => document.querySelector('#hour-label').textContent.includes('7:00 AM–8:00 AM')",
        timeout=3_000,
    )
    page.locator("#play").click()
    assert errors == []
    page.close()


# @spec DASH-TEST-002, DASH-WEB-008, DASH-WEB-009, DASH-WEB-010
def test_gap_hours_monitors_and_sensors(browser, served):
    page, _ = _open(browser, served)
    slider = page.locator("#slider")
    slider.focus()
    page.keyboard.press("End")  # 14:00, no fit row
    assert _overlay_src(page) is None
    assert (
        "No surface:"
        in page.locator("#hour-label").inner_text() + page.locator("#status").inner_text()
    )
    page.keyboard.press("ArrowLeft")  # 13:00, too few sensors
    assert "No surface:" in page.locator("#status").inner_text()
    assert H13.hour == 13 and H14.hour == 14

    page.keyboard.press("Home")
    page.keyboard.press("ArrowRight")  # 11:00, M2 flagged
    labels = page.locator(".monitor-label").all_inner_texts()
    assert any(text.endswith("*") for text in labels), labels
    page.keyboard.press("ArrowRight")  # 12:00, M3 null
    assert "n/a" in page.locator(".monitor-label").all_inner_texts()

    fills = page.evaluate(
        "() => [...document.querySelectorAll('path.sensor-marker')].map(p =>"
        " p.getAttribute('fill') === 'none' || p.getAttribute('fill-opacity') === '0')"
    )
    assert fills and all(fills), "sensors are drawn with no visible fill"
    page.locator("path.sensor-marker").first.click(force=True)
    popup = page.locator(".leaflet-popup-content").inner_text()
    assert "purpleair:" in popup and ("µg/m³" in popup or "no calibrated value" in popup)
    assert any(s in popup for s in ("fitted", "pooled", "uncalibrated"))
    page.close()


# @spec DASH-TEST-002, DASH-WEB-004, DASH-WEB-011, DASH-WEB-012
def test_legend_freshness_and_stale_banner(browser, served):
    page, _ = _open(browser, served)
    legend = page.locator("#legend").inner_text()
    assert "PM2.5 (µg/m³)" in legend and "10 µg/m³" in legend
    freshness = page.locator("#freshness").inner_text()
    assert freshness.startswith("Latest hour 8:00 AM EDT, updated "), freshness
    assert re.fullmatch(r"Latest hour .*, updated \d{1,2}:\d{2} [AP]M E[DS]T", freshness), freshness
    # the fixture's latest hour is weeks before any real clock
    banner = page.locator("#stale")
    assert banner.is_visible() and "hours old" in banner.inner_text()
    page.close()


# @spec DASH-TEST-002, DASH-WEB-014
def test_phone_width_has_no_horizontal_scroll_and_controls_take_the_keyboard(browser, served):
    page, _ = _open(browser, served, width=360, height=740)
    assert page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    page.locator("#play").focus()
    page.keyboard.press("Enter")
    assert page.locator("#play").get_attribute("aria-pressed") == "true"
    page.keyboard.press("Enter")
    assert page.locator("#play").get_attribute("aria-pressed") == "false"
    for control in ("#play", "#slider", "#kind-pm25", "#kind-diff", "#kind-sd"):
        name = page.locator(control).evaluate(
            "e => e.getAttribute('aria-label') || e.textContent.trim()"
        )
        assert name, control
    page.close()


# @spec DASH-TEST-001
def test_dashboard_tests_need_no_network_beyond_the_smoke_test():
    here = Path(__file__).parent
    source = "\n".join(p.read_text() for p in here.glob("*.py") if p.name != Path(__file__).name)
    for module in ("httpx", "requests", "psycopg", "boto3", "urllib.request"):
        assert not re.search(rf"^\s*(import|from) {re.escape(module)}\b", source, re.M), module
    assert "s3://" not in source


# @spec DASH-TEST-003
def test_post_create_installs_chromium_without_blocking_the_codespace():
    root = Path(__file__).resolve().parents[2]
    script = (root / ".devcontainer" / "post-create.sh").read_text()
    lines = script.replace("\\\n", " ").splitlines()  # join continued lines
    (install,) = [i for i, line in enumerate(lines) if "playwright install" in line]
    assert "uv run playwright install --with-deps chromium" in lines[install]
    assert "||" in lines[install], "a failed install is reported, not fatal under set -e"
    (sync,) = [i for i, line in enumerate(lines) if "uv sync --all-groups" in line]
    assert sync < install


# @spec DASH-TEST-002, DASH-WEB-015, DASH-WEB-004
def test_caption_explains_the_hour_and_legend_marks_clipping(browser, served, site):
    import json

    out, _ = site
    manifest = json.loads((out / "data" / "manifest.json").read_text())
    latest = next(h for h in manifest["hours"] if h["hour"] == manifest["latest_hour"])
    page, _ = _open(browser, served)
    caption = page.locator("#status").inner_text()
    expected_range = f"range {latest['surface_min']:.1f}–{latest['surface_max']:.1f} µg/m³"
    assert expected_range in caption, caption
    assert f"nugget {round(100 * latest['nugget_share'])}% of sill" in caption, caption
    # the legend's arrows follow the current hour's extremes against the active scale
    page.locator("#kind-diff").click()
    low = latest["surface_min"] - latest["surface_mean"] < manifest["scales"]["diff"]["min"]
    high = latest["surface_max"] - latest["surface_mean"] > manifest["scales"]["diff"]["max"]
    assert page.locator("#legend-under").is_visible() == low
    assert page.locator("#legend-over").is_visible() == high
    page.close()


_BOXES = """() => [...document.querySelectorAll('.monitor-icon')].map(icon => {
  const box = (e) => { const r = e.getBoundingClientRect();
    return {left: r.left, right: r.right, top: r.top, bottom: r.bottom}; };
  return {label: box(icon.querySelector('.monitor-label')),
          triangle: box(icon.querySelector('svg'))};
})"""


def _overlap(a, b) -> bool:
    return min(a["right"], b["right"]) > max(a["left"], b["left"]) and min(
        a["bottom"], b["bottom"]
    ) > max(a["top"], b["top"])


def _assert_labels_clear(page):
    boxes = page.evaluate(_BOXES)
    assert len(boxes) >= 4, boxes
    for i, mine in enumerate(boxes):
        for j, other in enumerate(boxes):
            if i == j:
                continue
            assert not _overlap(mine["label"], other["triangle"]), (i, j, "label over triangle")
            if j > i:
                assert not _overlap(mine["label"], other["label"]), (i, j, "labels overlap")
    return boxes


# @spec DASH-TEST-002, DASH-WEB-016
def test_monitor_labels_on_one_spot_do_not_overlap(browser, crowded):
    page, errors = _open(browser, crowded)
    boxes = _assert_labels_clear(page)
    # a monitor with nobody near it keeps its label on the right
    lone = boxes[3]
    assert lone["label"]["left"] >= lone["triangle"]["right"], lone
    # the three stacked monitors take right, left, and below, in manifest order
    first, second, third = boxes[:3]
    assert first["label"]["left"] >= first["triangle"]["right"]
    assert second["label"]["right"] <= second["triangle"]["left"]
    assert third["label"]["top"] >= third["triangle"]["bottom"]

    page.locator(".leaflet-control-zoom-out").click()
    page.wait_for_timeout(400)  # Leaflet's zoom animation
    _assert_labels_clear(page)
    page.locator("#slider").focus()
    page.keyboard.press("ArrowLeft")  # another hour, other label widths
    _assert_labels_clear(page)
    assert errors == []
    page.close()


# @spec DASH-TEST-002, DASH-WEB-006
def test_hour_label_takes_the_eastern_date_and_names_both_zones_across_a_change(browser, crowded):
    page, _ = _open(browser, crowded)
    slider = page.locator("#slider")
    slider.focus()
    page.keyboard.press("Home")  # 01:00 UTC on the 15th is 9 PM Eastern on the 14th
    assert page.locator("#hour-label").inner_text() == "Mon, Sep 14 · 9:00 PM–10:00 PM EDT"
    page.keyboard.press("ArrowRight")  # 05:00 UTC on 1 November: 1 AM EDT, then 1 AM EST
    assert page.locator("#hour-label").inner_text() == "Sun, Nov 1 · 1:00 AM EDT–1:00 AM EST"
    assert "UTC" not in page.locator("body").inner_text()
    page.close()


# @spec DASH-TEST-002, DASH-WEB-017
def test_map_names_sit_above_the_surface_and_below_the_markers(browser, served):
    page, errors = _open(browser, served)
    panes = page.evaluate(
        """() => Object.fromEntries(['labels', 'overlay', 'marker'].map(name => {
          const pane = document.querySelector(`.leaflet-${name}-pane`);
          const style = pane && getComputedStyle(pane);
          return [name, style && {z: Number(style.zIndex), pointer: style.pointerEvents}];
        }))"""
    )
    assert panes["labels"], "a pane holds the map names"
    assert panes["overlay"]["z"] < panes["labels"]["z"] < panes["marker"]["z"], panes
    assert panes["labels"]["pointer"] == "none", panes
    tiles = page.locator(".leaflet-labels-pane img.leaflet-tile")
    assert tiles.count() > 0
    assert "World_Boundaries_and_Places" in tiles.first.get_attribute("src")
    assert "Esri" in page.locator(".leaflet-control-attribution").inner_text()
    # a point under the names layer still reaches the sensor marker drawn there
    hit = page.evaluate(
        """() => { const r = document.querySelector('path.sensor-marker').getBoundingClientRect();
          const e = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return e.closest('.leaflet-labels-pane') ? 'labels' : e.tagName; }"""
    )
    assert hit != "labels", hit
    assert errors == []
    page.close()
