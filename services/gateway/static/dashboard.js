// Live dashboard for the gateway API. No dependencies: plain SVG charts.
(() => {
  const ZONES = ["zone-a", "zone-b", "zone-c", "zone-d"];
  const METRIC_META = {
    pm25: { label: "PM2.5", unit: "µg/m³", digits: 1 },
    pm10: { label: "PM10", unit: "µg/m³", digits: 0 },
    aqi: { label: "AQI", unit: "", digits: 0 },
    temperature_c: { label: "Temperature", unit: "°C", digits: 1 },
    humidity: { label: "Humidity", unit: "%", digits: 1 },
    co2: { label: "CO₂", unit: "ppm", digits: 0 },
    pressure_hpa: { label: "Pressure", unit: "hPa", digits: 1 },
  };
  // AQI category bands mapped onto the sequential blue ramp.
  const AQI_BANDS = [
    { max: 50, name: "Good", step: "--seq-100" },
    { max: 100, name: "Moderate", step: "--seq-200" },
    { max: 150, name: "USG", step: "--seq-300" },
    { max: 200, name: "Unhealthy", step: "--seq-400" },
    { max: 300, name: "Very unhealthy", step: "--seq-600" },
    { max: Infinity, name: "Hazardous", step: "--seq-700" },
  ];
  const SVG = "http://www.w3.org/2000/svg";
  const $ = (id) => document.getElementById(id);
  // Color follows the entity: a zone keeps its slot whatever else is on screen.
  const zoneColor = (z) => `var(--series-${(ZONES.indexOf(z) % 4) + 1})`;
  const fmt = (v, d = 1) => (v == null || Number.isNaN(+v) ? "–" : (+v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d }));
  const timeFmt = (t) => new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const state = { metric: "pm25", hours: 1 };

  function svgEl(tag, attrs = {}, parent) {
    const n = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
    if (parent) parent.appendChild(n);
    return n;
  }

  // ---- tooltip -----------------------------------------------------------
  const tip = $("tooltip");
  function showTip(html, x, y) {
    tip.innerHTML = html;
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    let left = x + 14, top = y + 14;
    if (left + r.width > innerWidth - 8) left = x - r.width - 14;
    if (top + r.height > innerHeight - 8) top = y - r.height - 14;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }
  const hideTip = () => { tip.hidden = true; };

  // ---- scales ------------------------------------------------------------
  function niceTicks(min, max, count = 4) {
    if (min === max) { min -= 1; max += 1; }
    const span = max - min;
    const step0 = span / count;
    const mag = 10 ** Math.floor(Math.log10(step0));
    const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || 10 * mag;
    const lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = lo; v <= hi + step / 2; v += step) ticks.push(+v.toFixed(10));
    return ticks;
  }

  function emptyChart(el, h, msg) {
    el.innerHTML = "";
    const svg = svgEl("svg", { viewBox: `0 0 600 ${h}` }, el);
    svgEl("text", { x: 300, y: h / 2, "text-anchor": "middle", class: "empty" }, svg).textContent = msg;
  }

  // ---- line chart with crosshair ----------------------------------------
  function lineChart(el, series, { height = 260, digits = 1, unit = "", directLabels = true, color } = {}) {
    const names = Object.keys(series).filter((k) => series[k].length);
    if (!names.length) return emptyChart(el, height, "Waiting for data…");
    if (new Set(names.flatMap((n) => series[n].map((p) => p.t))).size < 2)
      return emptyChart(el, height, "Collecting data – the chart starts after two complete intervals");
    const W = Math.max(320, el.clientWidth || 600), H = height;
    const m = { t: 10, r: directLabels && names.length <= 4 ? 64 : 16, b: 24, l: 44 };
    const all = names.flatMap((n) => series[n]);
    const times = [...new Set(all.map((p) => p.t))].sort((a, b) => a - b);
    const t0 = times[0], t1 = times[times.length - 1] || t0 + 1;
    const vals = all.map((p) => p.v);
    const ticks = niceTicks(Math.min(...vals), Math.max(...vals));
    const y0 = ticks[0], y1 = ticks[ticks.length - 1];
    const tickDigits = Math.max(0, -Math.floor(Math.log10(ticks[1] - ticks[0] || 1)));
    const x = (t) => m.l + ((t - t0) / Math.max(1, t1 - t0)) * (W - m.l - m.r);
    const y = (v) => H - m.b - ((v - y0) / (y1 - y0)) * (H - m.t - m.b);

    el.innerHTML = "";
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, role: "img" }, el);
    const g = svgEl("g", { class: "axis" }, svg);
    for (const tv of ticks) {
      svgEl("line", { x1: m.l, x2: W - m.r, y1: y(tv), y2: y(tv), class: tv === y0 ? "baseline" : "gridline" }, g);
      svgEl("text", { x: m.l - 6, y: y(tv) + 4, "text-anchor": "end" }, g).textContent = fmt(tv, tickDigits);
    }
    // Clock-aligned time ticks so labels never repeat.
    const maxTicks = Math.max(2, Math.floor((W - m.l - m.r) / 80));
    const steps = [10, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600].map((s) => s * 1000);
    const step = steps.find((s) => (t1 - t0) / s <= maxTicks) || steps[steps.length - 1];
    const withSeconds = step < 60_000;
    for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
      svgEl("text", { x: x(t), y: H - 6, "text-anchor": "middle" }, g).textContent =
        new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", ...(withSeconds ? { second: "2-digit" } : {}) });
    }

    const labelYs = [];
    for (const n of names) {
      const pts = series[n];
      const stroke = color ? color(n) : zoneColor(n);
      svgEl("path", {
        d: pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(""),
        fill: "none", stroke, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round",
      }, svg);
      const last = pts[pts.length - 1];
      if (directLabels && names.length <= 4) labelYs.push({ n, y: y(last.v), x: x(last.t), stroke });
    }
    // Direct labels at line ends, nudged apart so they never overlap.
    labelYs.sort((a, b) => a.y - b.y);
    for (let i = 1; i < labelYs.length; i++) labelYs[i].y = Math.max(labelYs[i].y, labelYs[i - 1].y + 13);
    for (const l of labelYs) {
      svgEl("circle", { cx: l.x, cy: y(series[l.n][series[l.n].length - 1].v), r: 4, fill: l.stroke, stroke: "var(--surface-1)", "stroke-width": 2 }, svg);
      svgEl("text", { x: l.x + 8, y: l.y + 4, class: "direct-label" }, svg).textContent = l.n;
    }

    // Crosshair + tooltip.
    const cross = svgEl("line", { y1: m.t, y2: H - m.b, stroke: "var(--axis)", "stroke-width": 1, visibility: "hidden" }, svg);
    const dots = names.map((n) => svgEl("circle", { r: 4, fill: color ? color(n) : zoneColor(n), stroke: "var(--surface-1)", "stroke-width": 2, visibility: "hidden" }, svg));
    const hit = svgEl("rect", { x: m.l, y: 0, width: W - m.l - m.r, height: H, fill: "transparent" }, svg);
    const byTime = new Map(names.map((n) => [n, new Map(series[n].map((p) => [p.t, p.v]))]));
    hit.addEventListener("pointermove", (e) => {
      const rect = svg.getBoundingClientRect();
      const px = ((e.clientX - rect.left) / rect.width) * W;
      const tt = t0 + ((px - m.l) / (W - m.l - m.r)) * (t1 - t0);
      let best = times[0];
      for (const t of times) if (Math.abs(t - tt) < Math.abs(best - tt)) best = t;
      cross.setAttribute("x1", x(best)); cross.setAttribute("x2", x(best)); cross.setAttribute("visibility", "visible");
      const rows = names.map((n, i) => {
        const v = byTime.get(n).get(best);
        if (v == null) { dots[i].setAttribute("visibility", "hidden"); return ""; }
        dots[i].setAttribute("cx", x(best)); dots[i].setAttribute("cy", y(v)); dots[i].setAttribute("visibility", "visible");
        return { n, v, c: color ? color(n) : zoneColor(n) };
      }).filter(Boolean).sort((a, b) => b.v - a.v);
      showTip(`<div class="t-head">${new Date(best).toLocaleString()}</div>` +
        rows.map((r) => `<div class="t-row"><span><i style="background:${r.c}"></i>${esc(r.n)}</span><b>${fmt(r.v, digits)} ${unit}</b></div>`).join(""),
        e.clientX, e.clientY);
    });
    hit.addEventListener("pointerleave", () => {
      cross.setAttribute("visibility", "hidden"); dots.forEach((d) => d.setAttribute("visibility", "hidden")); hideTip();
    });
  }

  // ---- horizontal bars (zones) ------------------------------------------
  function zoneBars(el, groups) {
    if (!groups.length) return emptyChart(el, 180, "Waiting for data…");
    const W = Math.max(320, el.clientWidth || 600), rowH = 34, m = { t: 6, r: 56, b: 22, l: 64 };
    const H = m.t + m.b + rowH * groups.length;
    const max = Math.max(...groups.map((g) => +g.max_aqi || 0), 50);
    const ticks = niceTicks(0, max, 4);
    const x = (v) => m.l + (v / ticks[ticks.length - 1]) * (W - m.l - m.r);
    el.innerHTML = "";
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` }, el);
    const ax = svgEl("g", { class: "axis" }, svg);
    for (const tv of ticks) {
      svgEl("line", { x1: x(tv), x2: x(tv), y1: m.t, y2: H - m.b, class: tv === 0 ? "baseline" : "gridline" }, ax);
      svgEl("text", { x: x(tv), y: H - 6, "text-anchor": "middle" }, ax).textContent = tv;
    }
    groups.forEach((g, i) => {
      const yy = m.t + i * rowH + 7, bh = rowH - 14, w = Math.max(2, x(+g.aqi) - x(0));
      svgEl("text", { x: m.l - 8, y: yy + bh / 2 + 4, "text-anchor": "end", class: "direct-label" }, svg).textContent = g.sensor_group;
      // Square at the baseline, 4px rounded at the data end.
      const r = Math.min(4, w / 2);
      svgEl("path", {
        d: `M${x(0)},${yy}H${x(0) + w - r}Q${x(0) + w},${yy} ${x(0) + w},${yy + r}V${yy + bh - r}Q${x(0) + w},${yy + bh} ${x(0) + w - r},${yy + bh}H${x(0)}Z`,
        fill: zoneColor(g.sensor_group),
      }, svg);
      svgEl("line", { x1: x(+g.max_aqi), x2: x(+g.max_aqi), y1: yy - 3, y2: yy + bh + 3, stroke: "var(--text-secondary)", "stroke-width": 2, "stroke-linecap": "round" }, svg);
      svgEl("text", { x: Math.max(x(+g.max_aqi), x(0) + w) + 8, y: yy + bh / 2 + 4, class: "direct-label" }, svg).textContent = fmt(g.aqi, 0);
      const hit = svgEl("rect", { x: 0, y: m.t + i * rowH, width: W, height: rowH, fill: "transparent" }, svg);
      hit.addEventListener("pointermove", (e) => showTip(
        `<div class="t-head">${esc(g.sensor_group)} · ${g.sensors} sensors</div>` +
        `<div class="t-row"><span>Mean AQI</span><b>${fmt(g.aqi, 0)}</b></div>` +
        `<div class="t-row"><span>Worst sensor</span><b>${fmt(g.max_aqi, 0)}</b></div>` +
        `<div class="t-row"><span>PM2.5</span><b>${fmt(g.pm25, 1)} µg/m³</b></div>` +
        `<div class="t-row"><span>Temperature</span><b>${fmt(g.temperature_c, 1)} °C</b></div>` +
        `<div class="t-row"><span>Anomalous now</span><b>${g.anomalous_sensors}</b></div>`, e.clientX, e.clientY));
      hit.addEventListener("pointerleave", hideTip);
    });
  }

  // ---- sensor field grid -------------------------------------------------
  const band = (aqi) => AQI_BANDS.find((b) => aqi <= b.max);
  function sensorField(el, latest) {
    if (!latest.length) return emptyChart(el, 120, "Waiting for data…");
    const sorted = [...latest].sort((a, b) => a.sensor_group.localeCompare(b.sensor_group) || a.sensor_id.localeCompare(b.sensor_id));
    const W = Math.max(320, el.clientWidth || 600);
    const cell = sorted.length > 400 ? 12 : sorted.length > 150 ? 16 : 22, gap = 2;
    const cols = Math.max(1, Math.floor((W + gap) / (cell + gap)));
    const rows = Math.ceil(sorted.length / cols);
    const H = rows * (cell + gap);
    el.innerHTML = "";
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}` }, el);
    sorted.forEach((s, i) => {
      const cx = (i % cols) * (cell + gap), cy = Math.floor(i / cols) * (cell + gap);
      const b = band(+s.aqi || 0);
      const rect = svgEl("rect", {
        x: cx + 1, y: cy + 1, width: cell - 2, height: cell - 2, rx: 3, fill: `var(${b.step})`,
        stroke: s.is_anomaly ? "var(--text-primary)" : "none", "stroke-width": s.is_anomaly ? 2 : 0,
      }, svg);
      rect.addEventListener("pointermove", (e) => showTip(
        `<div class="t-head">${esc(s.sensor_id)} · ${esc(s.sensor_group)}</div>` +
        `<div class="t-row"><span>AQI</span><b>${fmt(s.aqi, 0)} · ${esc(s.aqi_category)}</b></div>` +
        `<div class="t-row"><span>PM2.5</span><b>${fmt(s.pm25, 1)} µg/m³</b></div>` +
        `<div class="t-row"><span>Temp</span><b>${fmt(s.temperature_c, 1)} °C</b></div>` +
        `<div class="t-row"><span>Humidity</span><b>${fmt(s.humidity, 1)} %</b></div>` +
        `<div class="t-row"><span>CO₂</span><b>${fmt(s.co2, 0)} ppm</b></div>` +
        (s.is_anomaly ? `<div class="t-row"><span>Anomaly</span><b>${esc(s.anomalies.join(", "))}</b></div>` : ""),
        e.clientX, e.clientY));
      rect.addEventListener("pointerleave", hideTip);
    });
    $("field-legend").innerHTML = `<span>Good</span><span class="bar">${AQI_BANDS.map((b) => `<i style="background:var(${b.step})" title="${b.name}"></i>`).join("")}</span><span>Hazardous</span>`;
  }

  // ---- tables ------------------------------------------------------------
  const STATUS = { warning: { c: "var(--warning)", icon: "▲", label: "Warning" }, critical: { c: "var(--critical)", icon: "■", label: "Critical" } };
  function table(el, head, body, emptyMsg) {
    el.innerHTML = `<thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead><tbody>${
      body.length ? body.join("") : `<tr><td class="none" colspan="${head.length}">${emptyMsg}</td></tr>`}</tbody>`;
  }

  // ---- tiles -------------------------------------------------------------
  // Sub-second latencies read better in milliseconds.
  const latency = (s) => (s == null ? ["–", ""] : s < 1 ? [fmt(s * 1000, 1), "ms"] : [fmt(s, 2), "s"]);
  function tiles(k) {
    const b = k.avg_aqi != null ? band(+k.avg_aqi) : null;
    const items = [
      ["Active sensors", fmt(k.active_sensors, 0), "", `${fmt(k.consumers, 0)} processor pods`],
      ["Throughput", fmt(k.readings_per_s, 1), "rec/s", "5-minute average"],
      ["Mean AQI", fmt(k.avg_aqi, 0), "", b ? `${b.name} · worst ${fmt(k.max_aqi, 0)}` : ""],
      ["Mean PM2.5", fmt(k.avg_pm25, 1), "µg/m³", ""],
      ["Mean temperature", fmt(k.avg_temperature_c, 1), "°C", `humidity ${fmt(k.avg_humidity, 0)}%`],
      ["Anomalies", fmt(k.anomalies_5m, 0), "", `${fmt(k.gap_filled_5m, 0)} gap-filled · last 5 min`],
      ["Latency p95", ...latency(k.latency_p95_s), `p50 ${latency(k.latency_p50_s).join(" ")}`],
      ["Stream backlog", fmt(k.stream_lag, 0), "", `${fmt(k.dlq_size, 0)} in dead-letter queue`],
    ];
    $("tiles").innerHTML = items.map(([l, v, u, n]) =>
      `<div class="tile"><div class="label">${l}</div><div class="value">${v}${u ? `<small>${u}</small>` : ""}</div><div class="note">${n}</div></div>`).join("");
    $("asof").textContent = `updated ${new Date(k.as_of).toLocaleTimeString()}`;
  }

  // ---- data loading ------------------------------------------------------
  const get = (url) => fetch(url).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });

  async function loadTrend() {
    const meta = METRIC_META[state.metric];
    const window = state.hours > 6 ? "15m" : "1m";
    $("trend-title").textContent = `${meta.label} by zone`;
    $("trend-hint").textContent = `${window === "1m" ? "1-minute" : "15-minute"} rolling average${meta.unit ? `, ${meta.unit}` : ""}`;
    const data = await get(`/api/series?metric=${state.metric}&window=${window}&hours=${state.hours}&by=group`);
    const series = {};
    for (const z of ZONES) series[z] = [];
    for (const row of data) (series[row.series] ||= []).push({ t: Date.parse(row.bucket), v: +row.value });
    $("trend-legend").innerHTML = Object.keys(series).filter((z) => series[z].length)
      .map((z) => `<span><i style="background:${zoneColor(z)}"></i>${z}</span>`).join("");
    lineChart($("trend"), series, { digits: meta.digits, unit: meta.unit });
  }

  async function loadPipeline() {
    const { series } = await get(`/api/pipeline?hours=${Math.min(state.hours, 6)}`);
    // Drop the current, still-filling minute so the line doesn't dip at the end.
    const done = series.filter((r) => Date.parse(r.minute) < Date.now() - 60_000);
    const one = (key) => ({ all: done.map((r) => ({ t: Date.parse(r.minute), v: +r[key] })) });
    lineChart($("throughput"), one("records_per_s"), { height: 180, unit: "rec/s", directLabels: false, color: () => "var(--series-1)" });
    const lat = { all: done.map((r) => ({ t: Date.parse(r.minute), v: +r.latency_p95_s * 1000 })) };
    lineChart($("latency"), lat, { height: 180, digits: 1, unit: "ms", directLabels: false, color: () => "var(--series-1)" });
  }

  async function loadPanels() {
    const [groups, latest, alerts, anomalies, dlq] = await Promise.all([
      get("/api/groups"), get("/api/latest"), get("/api/alerts?minutes=60&limit=200"),
      get("/api/anomalies?minutes=60&limit=100"), get("/api/dlq?limit=50"),
    ]);
    zoneBars($("zones"), groups);
    sensorField($("field"), latest);
    table($("alerts"), ["Time", "Status", "Sensor", "Zone", "Metric", "Value"], alerts.map((a) => {
      const s = STATUS[a.severity];
      return `<tr><td>${timeFmt(a.bucket)}</td><td><span class="status"><span class="dot" style="background:${s.c}"></span>${s.icon} ${s.label}</span></td>` +
        `<td>${esc(a.sensor_id)}</td><td>${esc(a.sensor_group)}</td><td>${esc(a.label)}</td><td>${fmt(a.value, 1)} ${esc(a.unit)}</td></tr>`;
    }), "No threshold breaches in the last hour");
    table($("anomalies"), ["Time", "Sensor", "Metric", "PM2.5", "AQI"], anomalies.map((a) =>
      `<tr><td>${new Date(a.ts).toLocaleTimeString()}</td><td>${esc(a.sensor_id)}</td><td>${esc(a.metric)}</td><td>${fmt(a.pm25, 1)}</td><td>${fmt(a.aqi, 0)}</td></tr>`),
    "No anomalies in the last hour");
    table($("dlq"), ["Stage", "Reason", "Payload"], dlq.map((d) =>
      `<tr><td>${esc(d.stage)}</td><td class="wrap">${esc(d.reason)}</td><td class="wrap">${esc(String(d.raw).slice(0, 80))}</td></tr>`),
    "Dead-letter queue is empty");
  }

  async function refresh() {
    try { await Promise.all([loadTrend(), loadPipeline(), loadPanels()]); }
    catch (e) { console.error("refresh failed", e); }
  }

  function connect() {
    const es = new EventSource("/api/stream?interval=2");
    es.onmessage = (e) => { $("live").textContent = "● live"; $("live").classList.add("on"); tiles(JSON.parse(e.data)); };
    es.onerror = () => { $("live").textContent = "○ reconnecting"; $("live").classList.remove("on"); };
  }

  $("metric").addEventListener("change", (e) => { state.metric = e.target.value; loadTrend(); });
  $("range").addEventListener("change", (e) => { state.hours = +e.target.value; loadTrend(); loadPipeline(); });
  let resizeTimer;
  addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(refresh, 200); });

  connect();
  refresh();
  setInterval(refresh, 10_000);
})();
