// The dashboard front end: one manifest, one image overlay, and the hour's monitors and sensors.
// Everything shown is precomputed by `aqdt dashboard build`; this file only chooses what to draw.
// @spec DASH-WEB-001, DASH-WEB-002, DASH-WEB-003, DASH-WEB-004, DASH-WEB-005, DASH-WEB-006
// @spec DASH-WEB-007, DASH-WEB-008, DASH-WEB-009, DASH-WEB-010, DASH-WEB-011
// @spec DASH-WEB-014, DASH-WEB-015, DASH-WEB-016, DASH-WEB-017, DASH-WEB-018, DASH-WEB-019
// @spec DASH-WEB-025
(function () {
  "use strict";

  const FRAME_MS = 500;
  // below this variogram range the fit found no spatial structure (dashboard LLD § Pattern note)
  const PATTERN_RANGE_KM = 1;
  const HOUR_MS = 3600 * 1000;
  // the current hour's extremes on each kind's scale, from the manifest's frame statistics
  const KINDS = {
    pm25: {
      name: "Estimate",
      alt: "Estimated PM2.5",
      extremes: (h) => [h.surface_min, h.surface_max],
    },
    diff: {
      name: "Difference",
      alt: "PM2.5 difference from this hour's average",
      extremes: (h) => [h.surface_min - h.surface_mean, h.surface_max - h.surface_mean],
    },
    sd: {
      name: "Uncertainty",
      alt: "Uncertainty (standard deviation) of PM2.5",
      extremes: (h) => [0, h.sd_max],
    },
  };
  const REASONS = {
    insufficient_points: "too few sensors reported this hour to krige a surface",
    fit_failed: "no variogram could be fitted to this hour's sensors",
    missing: "this hour has not been kriged",
    surface_missing: "this hour's surface file could not be read",
  };

  const el = (id) => document.getElementById(id);
  // every time shown is Eastern; the browser's time zone data handles daylight saving
  const eastern = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "numeric",
    minute: "2-digit",
    hour12: true,
    timeZoneName: "short",
  });
  const easternDate = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    weekday: "short",
    month: "short",
    day: "numeric",
  });

  function easternParts(date) {
    const parts = eastern.formatToParts(date);
    const get = (type) => (parts.find((p) => p.type === type) || {}).value || "";
    return {
      time: `${get("hour")}:${get("minute")} ${get("dayPeriod")}`,
      zone: get("timeZoneName"),
    };
  }

  function easternTime(date) {
    const { time, zone } = easternParts(date);
    return `${time} ${zone}`;
  }

  function hourLabel(iso) {
    const start = new Date(iso);
    const end = new Date(start.getTime() + HOUR_MS);
    const s = easternParts(start);
    const e = easternParts(end);
    const span = s.zone === e.zone
      ? `${s.time}–${e.time} ${e.zone}`
      : `${s.time} ${s.zone}–${e.time} ${e.zone}`; // the hours daylight saving begins and ends
    return `${easternDate.format(start)} · ${span}`;
  }

  function formatValue(value) {
    const text = Number.isInteger(value) ? String(value) : value.toFixed(1);
    return text.replace("-", "−");
  }

  // one decimal, always signed: +1.2 or −2.5
  function signed(value) {
    const text = Math.abs(value).toFixed(1);
    return (value < 0 && text !== "0.0" ? "−" : "+") + text;
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  const state = {
    manifest: null, index: 0, kind: "pm25", timer: null, preload: null, historySite: null,
  };
  let map, overlay, monitorLayer, sensorLayer, patternNote, historyPopup;
  let monitorMarkers = [];
  let monitorMarkerById = {};
  const LABEL_SIDES = ["right", "left", "below", "above"];

  function triangleIcon(label) {
    const svg =
      '<svg width="16" height="14" viewBox="0 0 16 14" aria-hidden="true">' +
      '<polygon points="8,1 15,13 1,13" fill="none" stroke="#111" stroke-width="2"/>' +
      '<polygon points="8,1 15,13 1,13" fill="none" stroke="#fff" stroke-width="0.8"/></svg>';
    return L.divIcon({
      className: "monitor-icon",
      html: `${svg}<span class="monitor-label">${escapeHtml(label)}</span>`,
      iconSize: [16, 14],
      iconAnchor: [8, 9],
    });
  }

  // observed, then the map's value at the monitor's cell, then map − observed: "7.0 · map 4.5 (−2.5)"
  function monitorLabel(monitor) {
    let text = monitor.obs === null
      ? "n/a"
      : monitor.obs.toFixed(1) + (monitor.qc_flags.length ? "*" : "");
    if (monitor.map_value !== null) text += ` · map ${monitor.map_value.toFixed(1)}`;
    if (monitor.difference !== null) text += ` (${signed(monitor.difference)})`;
    return text;
  }

  function overlaps(a, b) {
    return Math.min(a.right, b.right) > Math.max(a.left, b.left) &&
      Math.min(a.bottom, b.bottom) > Math.max(a.top, b.top);
  }

  // Each label, in manifest order, takes the first side of its triangle clear of the labels
  // already placed and of every other monitor's triangle, or the right when none is clear.
  function placeLabels() {
    const icons = monitorMarkers.map((m) => m.getElement()).filter(Boolean);
    const triangles = icons.map((icon) => icon.querySelector("svg").getBoundingClientRect());
    const placed = [];
    icons.forEach((icon, i) => {
      const label = icon.querySelector(".monitor-label");
      const others = triangles.filter((_, j) => j !== i);
      const clear = (box) => !placed.some((p) => overlaps(box, p)) &&
        !others.some((t) => overlaps(box, t));
      let side = LABEL_SIDES.find((candidate) => {
        label.dataset.side = candidate;
        return clear(label.getBoundingClientRect());
      });
      if (!side) side = "right";
      label.dataset.side = side;
      placed.push(label.getBoundingClientRect());
    });
  }

  // --- Monitor history popup: the monitor's observed and map values over the whole window ---

  // pad: the first and last hours sit clear of the axis and its tick labels
  const CHART = { width: 280, height: 150, left: 34, right: 8, top: 10, bottom: 22, pad: 6 };

  // the monitor's entry in every hour of the window, null where the hour does not list it
  function monitorHistory(siteId) {
    return state.manifest.hours.map(
      (hour) => hour.monitors.find((m) => m.site_id === siteId) || null
    );
  }

  function historyStats(history) {
    const diffs = history
      .filter((m) => m && m.difference !== null)
      .map((m) => m.difference);
    if (!diffs.length) return "Window: no hours with a map–monitor difference";
    const mean = diffs.reduce((a, d) => a + d, 0) / diffs.length;
    const rmse = Math.sqrt(diffs.reduce((a, d) => a + d * d, 0) / diffs.length);
    return `Last ${history.length} h: mean difference (map − observed) ${signed(mean)} µg/m³` +
      ` · RMSE ${rmse.toFixed(1)} µg/m³ · n = ${diffs.length}`;
  }

  function chartX(i, n) {
    const width = CHART.width - CHART.left - CHART.right - 2 * CHART.pad;
    return CHART.left + CHART.pad + (n > 1 ? (i * width) / (n - 1) : width / 2);
  }

  function historyChart(history, label) {
    const n = history.length;
    const { width, height, left, right, top, bottom } = CHART;
    const values = history.flatMap((m) => (m ? [m.obs, m.map_value] : []))
      .filter((v) => v !== null);
    const top_value = values.length ? Math.max(...values) : 0;
    const ymax = top_value > 0 ? top_value : 1;
    const y = (v) => top + (height - top - bottom) * (1 - v / ymax);
    const xy = (i, v) => `${chartX(i, n).toFixed(1)},${y(v).toFixed(1)}`;
    const base = height - bottom;
    const parts = [
      `<svg class="history-chart" role="img" aria-label="${escapeHtml(label)}"` +
        ` viewBox="0 0 ${width} ${height}" width="100%">`,
      `<path d="M${left},${top}V${base}H${width - right}" fill="none" stroke="#909090"/>`,
      `<text class="y-max" x="${left - 4}" y="${top + 4}" text-anchor="end">` +
        `${top_value.toFixed(1)}</text>`,
      `<text class="y-min" x="${left - 4}" y="${base + 4}" text-anchor="end">0</text>`,
      `<text class="y-label" transform="translate(9 ${(top + base) / 2}) rotate(-90)"` +
        ` text-anchor="middle">µg/m³</text>`,
      `<text class="x-start" x="${left}" y="${height - 4}">` +
        `${escapeHtml(easternDate.format(new Date(state.manifest.hours[0].hour)))}</text>`,
      `<text class="x-end" x="${width - right}" y="${height - 4}" text-anchor="end">` +
        `${escapeHtml(easternDate.format(new Date(state.manifest.hours[n - 1].hour)))}</text>`,
    ];
    // the map value as a line, broken wherever an hour has none
    let run = [];
    history.forEach((m, i) => {
      if (m && m.map_value !== null) run.push(xy(i, m.map_value));
      if ((!m || m.map_value === null || i === n - 1) && run.length) {
        parts.push(`<polyline class="map-line" points="${run.join(" ")}"/>`);
        run = [];
      }
    });
    // the observed value as dots, hollow on a flagged hour, none where there is no value
    history.forEach((m, i) => {
      if (!m || m.obs === null) return;
      const [cx, cy] = xy(i, m.obs).split(",");
      parts.push(m.qc_flags.length
        ? `<circle class="obs flagged" cx="${cx}" cy="${cy}" r="3" fill="none"/>`
        : `<circle class="obs" cx="${cx}" cy="${cy}" r="3" fill="#111111"/>`);
    });
    const x = chartX(state.index, n).toFixed(1);
    parts.push(
      `<line class="hour-marker" data-index="${state.index}" x1="${x}" x2="${x}"` +
        ` y1="${top}" y2="${base}"/>`,
      "</svg>"
    );
    return parts.join("");
  }

  function historyContent(siteId) {
    const history = monitorHistory(siteId);
    const any = history.find((m) => m);
    const name = any.name || siteId;
    const stats = historyStats(history);
    const label = `${name}: observed and map PM2.5 over the window. ${stats}`;
    return `<div class="monitor-history">` +
      `<h3 class="monitor-history-name">${escapeHtml(name)}</h3>` +
      historyChart(history, label) +
      `<p class="history-legend"><span class="legend-obs">● observed</span>` +
      ` <span class="legend-map">— map</span></p>` +
      `<p class="history-stats">${escapeHtml(stats)}</p></div>`;
  }

  // @spec DASH-WEB-020, DASH-WEB-021, DASH-WEB-022
  function openHistory(siteId) {
    const any = monitorHistory(siteId).find((m) => m);
    state.historySite = siteId;
    historyPopup.setLatLng([any.lat, any.lon]).setContent(historyContent(siteId));
    if (!historyPopup.isOpen()) historyPopup.openOn(map);
  }

  // @spec DASH-WEB-023
  // the chart describes the whole window; only its marker follows the hour shown
  function moveHistoryMarker() {
    if (!state.historySite || !historyPopup.isOpen()) return;
    const marker = historyPopup.getElement().querySelector(".hour-marker");
    const x = chartX(state.index, state.manifest.hours.length).toFixed(1);
    marker.setAttribute("x1", x);
    marker.setAttribute("x2", x);
    marker.dataset.index = String(state.index);
  }

  function drawPoints(hour) {
    monitorLayer.clearLayers();
    sensorLayer.clearLayers();
    monitorMarkers = [];
    monitorMarkerById = {};
    for (const sensor of hour.sensors) {
      const value = sensor.pm25_calibrated === null
        ? "no calibrated value"
        : `${sensor.pm25_calibrated.toFixed(1)} µg/m³ calibrated`;
      L.circleMarker([sensor.lat, sensor.lon], {
        radius: 5,
        // a transparent fill: nothing is drawn inside the outline, but the whole disc takes
        // a tap or click (an unfilled SVG path only responds on its stroke)
        fill: true,
        fillOpacity: 0,
        color: "#111",
        weight: 1.5,
        className: "sensor-marker",
      })
        .bindPopup(
          `<strong>${escapeHtml(sensor.sensor_id)}</strong><br>${escapeHtml(value)}` +
          `<br>Fit: ${escapeHtml(sensor.fit_status)}`
        )
        .addTo(sensorLayer);
    }
    // @spec DASH-WEB-024
    for (const monitor of hour.monitors) {
      const name = monitor.name || monitor.site_id;
      const text = monitorLabel(monitor);
      // keyboard: Leaflet puts the triangle in the tab order as a button; Enter and Space open
      // its popup below
      const marker = L.marker([monitor.lat, monitor.lon], {
        icon: triangleIcon(text),
        keyboard: true,
        title: `${name}: ${monitor.obs === null ? "no value" : text + " µg/m³"}`,
      })
        .on("click", () => openHistory(monitor.site_id))
        .addTo(monitorLayer);
      const icon = marker.getElement();
      icon.setAttribute("aria-label", `AirNow monitor ${name}`);
      icon.addEventListener("keydown", (event) => {
        if (event.key !== " " && event.key !== "Enter") return;
        event.preventDefault();
        openHistory(monitor.site_id);
      });
      monitorMarkers.push(marker);
      monitorMarkerById[monitor.site_id] = marker;
    }
    placeLabels();
  }

  function drawLegend(hour) {
    const scale = state.manifest.scales[state.kind];
    el("legend-title").textContent = `${KINDS[state.kind].name}: ${scale.label}`;
    el("legend-bar").style.background = `linear-gradient(to right, ${scale.stops.join(", ")})`;
    el("legend-min").textContent = formatValue(scale.min);
    el("legend-max").textContent = `${scale.min < 0 ? "+" : ""}${formatValue(scale.max)} ${scale.units}`;
    let under = false;
    let over = false;
    if (hour.status === "kriged") {
      const [low, high] = KINDS[state.kind].extremes(hour);
      under = low < scale.min;
      over = high > scale.max;
    }
    el("legend-under").hidden = !under;
    el("legend-over").hidden = !over;
    el("legend-clipped").hidden = !(under || over);
    el("legend-faded").hidden = hour.status !== "kriged";
    el("legend-uncertainty").hidden = state.kind !== "sd";
  }

  function caption(hour) {
    const parts = [
      `Kriged from ${hour.n_sensors} sensors`,
      `${hour.variogram_model} variogram`,
      `values ${hour.surface_min.toFixed(1)}–${hour.surface_max.toFixed(1)} µg/m³`,
    ];
    if (hour.range_km !== null) parts.push(`variogram range ${hour.range_km.toFixed(1)} km`);
    if (hour.nugget_share !== null) {
      parts.push(`nugget ${Math.round(100 * hour.nugget_share)}% of sill`);
    }
    return parts.join(" · ");
  }

  function monitorCheck(hour) {
    if (hour.status !== "kriged" || !hour.monitor_n) return "";
    return `Monitors: mean difference ${signed(hour.monitor_mean_difference)} µg/m³, ` +
      `n = ${hour.monitor_n}`;
  }

  function show(index) {
    const hours = state.manifest.hours;
    state.index = (index + hours.length) % hours.length;
    const hour = hours[state.index];
    el("slider").value = String(state.index);
    const label = hourLabel(hour.hour);
    el("hour-label").textContent = label;
    el("slider").setAttribute("aria-valuetext", label);

    if (hour.status === "kriged") {
      const url = hour[`${state.kind}_png`];
      const alt = `${KINDS[state.kind].alt}, ${label}`;
      if (!overlay) {
        // each frame carries its own alpha, faded where the estimate is least supported
        overlay = L.imageOverlay(url, hour.bounds_latlon, { opacity: 1, alt, interactive: false });
        overlay.addTo(map);
      } else {
        overlay.setUrl(url);
        overlay.setBounds(L.latLngBounds(hour.bounds_latlon));
        if (!map.hasLayer(overlay)) overlay.addTo(map);
      }
      overlay.getElement().setAttribute("alt", alt);
      el("status").textContent = caption(hour);
    } else {
      if (overlay && map.hasLayer(overlay)) map.removeLayer(overlay);
      el("status").textContent = `No surface: ${REASONS[hour.status] || hour.status}.`;
    }
    el("monitor-check").textContent = monitorCheck(hour);
    patternNote.hidden = !(
      hour.status === "kriged" && hour.range_km !== null && hour.range_km < PATTERN_RANGE_KM
    );
    drawLegend(hour);
    drawPoints(hour);
    moveHistoryMarker();
  }

  function setKind(kind) {
    state.kind = kind;
    for (const k of Object.keys(KINDS)) {
      el(`kind-${k}`).setAttribute("aria-pressed", String(k === kind));
    }
    show(state.index);
  }

  function preloadFrames() {
    window.dashboardPreloaded = 0;
    const urls = [];
    for (const hour of state.manifest.hours) {
      if (hour.status === "kriged") urls.push(hour.pm25_png, hour.diff_png, hour.sd_png);
    }
    return Promise.all(urls.map((url) => new Promise((resolve) => {
      const image = new Image();
      image.onload = () => { window.dashboardPreloaded += 1; resolve(); };
      image.onerror = resolve; // a missing frame shows as a broken overlay, not a stuck player
      image.src = url;
    })));
  }

  function setPlaying(playing) {
    const button = el("play");
    button.setAttribute("aria-pressed", String(playing));
    button.setAttribute("aria-label", playing ? "Pause animation" : "Play animation");
    button.textContent = playing ? "❚❚" : "▶";
    if (state.timer) {
      clearInterval(state.timer);
      state.timer = null;
    }
    if (playing) {
      state.preload.then(() => {
        if (el("play").getAttribute("aria-pressed") !== "true" || state.timer) return;
        state.timer = setInterval(() => show(state.index + 1), FRAME_MS);
      });
    }
  }

  function drawFreshness() {
    const manifest = state.manifest;
    const latest = new Date(manifest.latest_hour);
    const generated = new Date(manifest.generated_at);
    el("freshness").textContent =
      `Latest hour ${easternTime(latest)}, updated ${easternTime(generated)}`;
  }

  async function main() {
    const response = await fetch("data/manifest.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`manifest: HTTP ${response.status}`);
    state.manifest = await response.json();
    const hours = state.manifest.hours;

    map = L.map("map", { zoomSnap: 0.25 });
    L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 19,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
    }).addTo(map);
    // place and road names above the surface (overlay pane 400) and below the markers (600),
    // letting every click through to what lies beneath
    const labels = map.createPane("labels");
    labels.style.zIndex = "450";
    labels.style.pointerEvents = "none";
    L.tileLayer(
      "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/" +
        "World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}",
      {
        pane: "labels",
        maxZoom: 19,
        attribution:
          'Names &copy; <a href="https://www.esri.com">Esri</a>, HERE, Garmin, ' +
          "and the GIS user community",
      }
    ).addTo(map);
    const note = L.control({ position: "topright" });
    note.onAdd = () => {
      const div = L.DomUtil.create("div", "map-note");
      div.id = "map-note";
      div.setAttribute("role", "status");
      div.textContent = "Spatial pattern not detectable this hour; map ≈ hour average.";
      div.hidden = true;
      return div;
    };
    note.addTo(map);
    patternNote = el("map-note");
    sensorLayer = L.layerGroup().addTo(map);
    monitorLayer = L.layerGroup().addTo(map);
    map.on("zoomend", placeLabels); // what is close on screen changes with the zoom
    // the top-left padding keeps the popup clear of the zoom control when Leaflet pans to it
    historyPopup = L.popup({
      maxWidth: 280, minWidth: 200, autoPanPaddingTopLeft: [56, 8], className: "history-popup",
    });
    map.on("popupclose", (event) => {
      if (event.popup === historyPopup) state.historySite = null;
    });
    // capture, so this runs before Leaflet's own Escape handling closes the popup
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape" || !state.historySite) return;
      const marker = monitorMarkerById[state.historySite];
      map.closePopup(historyPopup);
      if (marker && marker.getElement()) marker.getElement().focus();
    }, true);

    const latestIndex = hours.findIndex((h) => h.hour === state.manifest.latest_hour);
    const framed = hours.find((h) => h.bounds_latlon) || hours[latestIndex];
    map.fitBounds(framed.bounds_latlon);

    const slider = el("slider");
    slider.max = String(hours.length - 1);
    slider.addEventListener("input", () => show(Number(slider.value)));
    el("play").addEventListener("click", () => {
      setPlaying(el("play").getAttribute("aria-pressed") !== "true");
    });
    for (const k of Object.keys(KINDS)) {
      el(`kind-${k}`).addEventListener("click", () => setKind(k));
    }

    drawFreshness();
    setInterval(drawFreshness, 60 * 1000);
    state.preload = preloadFrames();
    show(latestIndex >= 0 ? latestIndex : hours.length - 1);
    await state.preload;
    window.dashboardReady = true;
  }

  main().catch((error) => {
    el("freshness").textContent = `The map could not be loaded (${error.message}).`;
    throw error;
  });
})();
