// The dashboard front end: one manifest, one image overlay, and the hour's monitors and sensors.
// Everything shown is precomputed by `aqdt dashboard build`; this file only chooses what to draw.
// @spec DASH-WEB-001, DASH-WEB-002, DASH-WEB-003, DASH-WEB-004, DASH-WEB-005, DASH-WEB-006
// @spec DASH-WEB-007, DASH-WEB-008, DASH-WEB-009, DASH-WEB-010, DASH-WEB-011, DASH-WEB-012
// @spec DASH-WEB-014, DASH-WEB-015
(function () {
  "use strict";

  const FRAME_MS = 500;
  const STALE_AFTER_MS = 2 * 3600 * 1000; // measured from the end of the latest hour
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
  const pad = (n) => String(n).padStart(2, "0");
  const utcHHMM = (d) => `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
  const eastern = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZoneName: "short",
  });
  const dateFormat = new Intl.DateTimeFormat("en-GB", {
    timeZone: "UTC",
    weekday: "short",
    day: "numeric",
    month: "short",
  });

  function easternParts(date) {
    const parts = eastern.formatToParts(date);
    const get = (type) => (parts.find((p) => p.type === type) || {}).value || "";
    return { time: `${get("hour")}:${get("minute")}`, zone: get("timeZoneName") };
  }

  function hourLabel(iso) {
    const start = new Date(iso);
    const end = new Date(start.getTime() + HOUR_MS);
    const s = easternParts(start);
    const e = easternParts(end);
    return (
      `${dateFormat.format(start)} · ${utcHHMM(start)}–${utcHHMM(end)} UTC ` +
      `(${s.time}–${e.time} ${e.zone})`
    );
  }

  function formatValue(value) {
    const text = Number.isInteger(value) ? String(value) : value.toFixed(1);
    return text.replace("-", "−");
  }

  function escapeHtml(text) {
    return String(text).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);
  }

  const state = { manifest: null, index: 0, kind: "pm25", timer: null, preload: null };
  let map, overlay, monitorLayer, sensorLayer;

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

  function monitorLabel(monitor) {
    if (monitor.obs === null) return "n/a";
    return monitor.obs.toFixed(1) + (monitor.qc_flags.length ? "*" : "");
  }

  function drawPoints(hour) {
    monitorLayer.clearLayers();
    sensorLayer.clearLayers();
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
    for (const monitor of hour.monitors) {
      const name = monitor.name || monitor.site_id;
      const text = monitorLabel(monitor);
      L.marker([monitor.lat, monitor.lon], {
        icon: triangleIcon(text),
        keyboard: false,
        title: `${name}: ${text === "n/a" ? "no value" : text + " µg/m³"}`,
        alt: `AirNow monitor ${name}`,
      }).addTo(monitorLayer);
    }
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
  }

  function caption(hour) {
    const parts = [
      `Kriged from ${hour.n_sensors} sensors`,
      `${hour.variogram_model} variogram`,
      `range ${hour.surface_min.toFixed(1)}–${hour.surface_max.toFixed(1)} µg/m³`,
    ];
    if (hour.nugget_share !== null) {
      parts.push(`nugget ${Math.round(100 * hour.nugget_share)}% of sill`);
    }
    return parts.join(" · ");
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
        overlay = L.imageOverlay(url, hour.bounds_latlon, { opacity: 0.85, alt, interactive: false });
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
    drawLegend(hour);
    drawPoints(hour);
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
      `Latest hour ${utcHHMM(latest)} UTC, updated ${utcHHMM(generated)} UTC`;
    const ageMs = Date.now() - (latest.getTime() + HOUR_MS);
    const stale = el("stale");
    if (ageMs > STALE_AFTER_MS) {
      const hoursOld = Math.floor((Date.now() - latest.getTime()) / HOUR_MS);
      stale.textContent =
        `The latest hour shown is ${hoursOld} hours old: the twin may have stopped updating.`;
      stale.hidden = false;
    } else {
      stale.hidden = true;
    }
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
    sensorLayer = L.layerGroup().addTo(map);
    monitorLayer = L.layerGroup().addTo(map);

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
