/* AI-QI dashboard (Part 4) — client logic + hand-rolled SVG charts.
   Zero dependencies. Talks to the same-origin FastAPI backend. */

'use strict';

// ---- Domain constants -------------------------------------------------------

// API pollutant identifiers (must match the query params the backend expects).
const POLLUTANTS = ['PM2.5', 'PM10', 'NO2', 'SO2', 'CO', 'OZONE', 'NH3'];
const UNITS = { 'PM2.5': 'µg/m³', 'PM10': 'µg/m³', NO2: 'µg/m³', SO2: 'µg/m³', CO: 'mg/m³', OZONE: 'µg/m³', NH3: 'µg/m³' };
const LABELS = { 'PM2.5': 'PM2.5', 'PM10': 'PM10', NO2: 'NO₂', SO2: 'SO₂', CO: 'CO', OZONE: 'O₃', NH3: 'NH₃' };

// CPCB National AQI sub-index breakpoints: [concLow, concHigh, aqiLow, aqiHigh].
// Bands map to categories: Good, Satisfactory, Moderate, Poor, Very Poor, Severe.
const CATEGORIES = ['Good', 'Satisfactory', 'Moderate', 'Poor', 'Very Poor', 'Severe'];
const BREAKPOINTS = {
  'PM2.5': [[0, 30, 0, 50], [31, 60, 51, 100], [61, 90, 101, 200], [91, 120, 201, 300], [121, 250, 301, 400], [251, 500, 401, 500]],
  'PM10':  [[0, 50, 0, 50], [51, 100, 51, 100], [101, 250, 101, 200], [251, 350, 201, 300], [351, 430, 301, 400], [431, 600, 401, 500]],
  NO2:     [[0, 40, 0, 50], [41, 80, 51, 100], [81, 180, 101, 200], [181, 280, 201, 300], [281, 400, 301, 400], [401, 500, 401, 500]],
  SO2:     [[0, 40, 0, 50], [41, 80, 51, 100], [81, 380, 101, 200], [381, 800, 201, 300], [801, 1600, 301, 400], [1601, 2000, 401, 500]],
  CO:      [[0, 1, 0, 50], [1.1, 2, 51, 100], [2.1, 10, 101, 200], [10.1, 17, 201, 300], [17.1, 34, 301, 400], [34.1, 50, 401, 500]],
  OZONE:   [[0, 50, 0, 50], [51, 100, 51, 100], [101, 168, 101, 200], [169, 208, 201, 300], [209, 748, 301, 400], [749, 1000, 401, 500]],
  NH3:     [[0, 200, 0, 50], [201, 400, 51, 100], [401, 800, 101, 200], [801, 1200, 201, 300], [1201, 1800, 301, 400], [1801, 2000, 401, 500]],
};

// Mono-gold severity ramp (deep → bright): worse air = brighter, hotter gold.
const GOLD_RAMP = ['#8a6d00', '#b38f00', '#d4a900', '#ffcc00', '#ffdb4d', '#ffeb99'];
const CAT_DESC = {
  Good: 'Air quality is healthy. Minimal impact on health.',
  Satisfactory: 'Acceptable air quality; minor breathing discomfort possible for the sensitive.',
  Moderate: 'Breathing discomfort possible for people with lung or heart conditions.',
  Poor: 'Breathing discomfort on prolonged exposure. Consider limiting outdoor exertion.',
  'Very Poor': 'Respiratory illness likely on prolonged exposure. Reduce outdoor activity.',
  Severe: 'Serious health impact for all. Avoid outdoor activity.',
};

// Returns { subIndex, category, level } for a concentration, or null if unmapped.
function subIndex(pollutant, conc) {
  if (conc == null || Number.isNaN(conc)) return null;
  const bands = BREAKPOINTS[pollutant];
  if (!bands) return null;
  for (let i = 0; i < bands.length; i++) {
    const [cLo, cHi, aLo, aHi] = bands[i];
    if (conc <= cHi) {
      const c = Math.max(conc, cLo);
      const idx = Math.round(((aHi - aLo) / (cHi - cLo)) * (c - cLo) + aLo);
      return { subIndex: idx, category: CATEGORIES[i], level: i + 1 };
    }
  }
  return { subIndex: 500, category: 'Severe', level: 6 };
}

// ---- Formatting -------------------------------------------------------------

const IST = 'Asia/Kolkata';
const fmtHour = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', timeZone: IST });
const fmtDay = new Intl.DateTimeFormat('en-GB', { day: '2-digit', month: 'short', timeZone: IST });
const fmtStamp = new Intl.DateTimeFormat('en-GB', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: IST });

const num = (v, d = 1) => (v == null || Number.isNaN(v) ? '—' : Number(v).toFixed(d));

// ---- State ------------------------------------------------------------------

const state = {
  stations: [],
  stationId: null,
  pollutant: 'PM2.5',
  clean: false,
  impute: false,
  overview: [],   // cached /overview rows for the current pollutant (powers the map)
};

const $ = (sel) => document.querySelector(sel);

// ---- Networking -------------------------------------------------------------

async function api(path) {
  const res = await fetch(path, { headers: { Accept: 'application/json' } });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json();
}

// ---- SVG helpers ------------------------------------------------------------

const NS = 'http://www.w3.org/2000/svg';
function el(tag, attrs = {}, kids = []) {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  for (const c of kids) n.appendChild(c);
  return n;
}

const GOLD = '#ffcc00', GOLD_SOFT = '#ffeb99', GOLD_DEEP = '#b38f00';

// Generic state block (loading / empty / error) rendered into a chart container.
function stateBlock(container, kind, title, msg, onRetry) {
  const icons = {
    empty: '<path d="M3 3v18h18"/><path d="M18.7 8l-5.1 5.2-2.8-2.7L7 14.3"/>',
    error: '<circle cx="12" cy="12" r="9"/><path d="M12 8v4"/><path d="M12 16h.01"/>',
    loading: '<path d="M21 12a9 9 0 1 1-6.2-8.5"/>',
  };
  container.innerHTML =
    `<div class="state">
       <svg width="34" height="34" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" ${kind === 'loading' ? 'style="animation:spin 1s linear infinite"' : ''}>${icons[kind]}</svg>
       <div class="title">${title}</div>
       <div class="msg">${msg || ''}</div>
     </div>`;
  if (onRetry) {
    const b = document.createElement('button');
    b.className = 'retry'; b.textContent = 'Retry';
    b.addEventListener('click', onRetry);
    container.querySelector('.state').appendChild(b);
  }
}
// keyframes for the loading spinner (added once)
const styleTag = document.createElement('style');
styleTag.textContent = '@keyframes spin{to{transform:rotate(360deg)}}';
document.head.appendChild(styleTag);

// ---- Line chart (history + forecast share this) -----------------------------
// series: { line: [{t,v}], raw?: [{t,v}], band?: [{t,lo,hi}], now?: Date }
function lineChart(container, series, opts = {}) {
  const W = 760, H = 300;
  const pad = { t: 18, r: 16, b: 34, l: 46 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;

  const all = [];
  series.line.forEach((p) => p.v != null && all.push(p.v));
  if (series.raw) series.raw.forEach((p) => p.v != null && all.push(p.v));
  if (series.band) series.band.forEach((p) => { if (p.lo != null) all.push(p.lo); if (p.hi != null) all.push(p.hi); });

  const times = series.line.map((p) => +p.t);
  if (series.band) series.band.forEach((p) => times.push(+p.t));
  const tMin = Math.min(...times), tMax = Math.max(...times);
  let vMin = Math.min(...all), vMax = Math.max(...all);
  if (!isFinite(vMin)) { vMin = 0; vMax = 1; }
  const span = vMax - vMin || 1;
  vMin = Math.max(0, vMin - span * 0.12); vMax = vMax + span * 0.12;

  const x = (t) => pad.l + ((+t - tMin) / (tMax - tMin || 1)) * iw;
  const y = (v) => pad.t + (1 - (v - vMin) / (vMax - vMin || 1)) * ih;

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': opts.ariaLabel || 'chart' });

  // Y gridlines + labels
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) {
    const v = vMin + (i / ticks) * (vMax - vMin);
    const yy = y(v);
    svg.appendChild(el('line', { x1: pad.l, y1: yy, x2: W - pad.r, y2: yy, stroke: GOLD_DEEP, 'stroke-opacity': 0.14 }));
    svg.appendChild(el('text', { x: pad.l - 8, y: yy + 4, 'text-anchor': 'end', fill: '#7a776e', 'font-size': 11, 'font-family': 'Fira Code, monospace' },
      [document.createTextNode(num(v, v >= 100 ? 0 : 1))]));
  }

  // X labels (~5 evenly spaced)
  const xt = 5;
  for (let i = 0; i <= xt; i++) {
    const t = tMin + (i / xt) * (tMax - tMin);
    const d = new Date(t);
    const label = (tMax - tMin) > 36 * 3600e3 ? fmtDay.format(d) : fmtHour.format(d);
    svg.appendChild(el('text', { x: x(t), y: H - 12, 'text-anchor': 'middle', fill: '#7a776e', 'font-size': 11, 'font-family': 'Fira Code, monospace' },
      [document.createTextNode(label)]));
  }

  // "now" divider (forecast chart)
  if (series.now && +series.now >= tMin && +series.now <= tMax) {
    const nx = x(series.now);
    svg.appendChild(el('line', { x1: nx, y1: pad.t, x2: nx, y2: pad.t + ih, stroke: GOLD_SOFT, 'stroke-opacity': 0.4, 'stroke-dasharray': '3 4' }));
    svg.appendChild(el('text', { x: nx + 5, y: pad.t + 11, fill: GOLD_SOFT, 'font-size': 10, 'font-family': 'Fira Code, monospace', 'fill-opacity': 0.75 }, [document.createTextNode('now')]));
  }

  // Uncertainty band (area between lo and hi)
  if (series.band && series.band.length) {
    const top = series.band.map((p) => `${x(p.t)},${y(p.hi)}`);
    const bot = series.band.slice().reverse().map((p) => `${x(p.t)},${y(p.lo)}`);
    svg.appendChild(el('polygon', { points: [...top, ...bot].join(' '), fill: GOLD_DEEP, 'fill-opacity': 0.16, stroke: 'none' }));
  }

  const linePath = (pts) => {
    // Break the path at null gaps so missing data doesn't draw a false line.
    let d = '', pen = false;
    for (const p of pts) {
      if (p.v == null) { pen = false; continue; }
      d += `${pen ? 'L' : 'M'}${x(p.t)},${y(p.v)} `; pen = true;
    }
    return d.trim();
  };

  // Raw ghost line (behind), when cleaning is on
  if (series.raw && series.raw.some((p) => p.v != null)) {
    svg.appendChild(el('path', { d: linePath(series.raw), fill: 'none', stroke: '#7a776e', 'stroke-width': 1, 'stroke-opacity': 0.5, 'stroke-dasharray': '2 3' }));
  }

  // Comparison line (e.g. predicted vs actual) — dashed deep-gold, behind main.
  if (series.cmp && series.cmp.some((p) => p.v != null)) {
    svg.appendChild(el('path', { d: linePath(series.cmp), fill: 'none', stroke: GOLD_DEEP, 'stroke-width': 1.8, 'stroke-dasharray': '5 4', 'stroke-linejoin': 'round', 'stroke-linecap': 'round' }));
  }

  // Main line
  svg.appendChild(el('path', { d: linePath(series.line), fill: 'none', stroke: GOLD, 'stroke-width': 2.2, 'stroke-linejoin': 'round', 'stroke-linecap': 'round', style: 'filter:drop-shadow(0 0 6px rgba(255,204,0,0.35))' }));

  // Imputed points get a hollow marker
  series.line.forEach((p) => {
    if (p.v == null) return;
    if (p.imputed) svg.appendChild(el('circle', { cx: x(p.t), cy: y(p.v), r: 2.6, fill: '#000', stroke: GOLD_SOFT, 'stroke-width': 1.4 }));
  });

  // Hover crosshair + marker
  const cross = el('line', { y1: pad.t, y2: pad.t + ih, stroke: GOLD_SOFT, 'stroke-opacity': 0.5, 'stroke-width': 1, visibility: 'hidden' });
  const marker = el('circle', { r: 4, fill: GOLD, stroke: '#000', 'stroke-width': 2, visibility: 'hidden', style: 'filter:drop-shadow(0 0 5px rgba(255,204,0,0.6))' });
  svg.appendChild(cross); svg.appendChild(marker);

  // Combined point list for hover (line first; forecast band midpoints already in line)
  const hoverPts = series.line.filter((p) => p.v != null);
  const tip = $('#tip');
  const overlay = el('rect', { x: pad.l, y: pad.t, width: iw, height: ih, fill: 'transparent', style: 'cursor:crosshair' });
  svg.appendChild(overlay);

  function move(ev) {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    let best = null, bd = Infinity;
    for (const p of hoverPts) { const d = Math.abs(x(p.t) - px); if (d < bd) { bd = d; best = p; } }
    if (!best) return;
    cross.setAttribute('x1', x(best.t)); cross.setAttribute('x2', x(best.t)); cross.setAttribute('visibility', 'visible');
    marker.setAttribute('cx', x(best.t)); marker.setAttribute('cy', y(best.v)); marker.setAttribute('visibility', 'visible');
    const cmpRow = best.cmpV != null
      ? `<div class="r" style="color:var(--gold-deep)">pred <span>${num(best.cmpV)}</span> ${opts.unit || ''}${best.err != null ? ` · Δ${best.err >= 0 ? '+' : ''}${num(best.err)}` : ''}</div>`
      : '';
    tip.innerHTML = `<div class="t">${fmtStamp.format(new Date(best.t))} IST</div>` +
      `<div class="r"><span class="g">${num(best.v)}</span> ${opts.unit || ''}${best.imputed ? ' · imputed' : ''}${best.band ? ` <span class="t">(${num(best.band.lo)}–${num(best.band.hi)})</span>` : ''}</div>` +
      cmpRow;
    tip.style.left = ev.clientX + 'px';
    tip.style.top = (rect.top + y(best.v)) + 'px';
    tip.classList.add('on'); tip.setAttribute('aria-hidden', 'false');
  }
  function leave() { cross.setAttribute('visibility', 'hidden'); marker.setAttribute('visibility', 'hidden'); tip.classList.remove('on'); tip.setAttribute('aria-hidden', 'true'); }
  overlay.addEventListener('pointermove', move);
  overlay.addEventListener('pointerleave', leave);

  container.innerHTML = '';
  container.appendChild(svg);
}

// ---- Renders ----------------------------------------------------------------

function renderHero(reading) {
  const gv = $('#gauge-val'), arc = $('#gauge-arc'), bar = $('#hero-bar');
  const unit = UNITS[state.pollutant];
  $('#gauge-unit').textContent = unit;

  if (!reading || reading.value == null) {
    gv.textContent = '—';
    arc.setAttribute('stroke-dasharray', '0 999');
    $('#hero-cat-label').textContent = 'No data';
    $('#hero-desc').textContent = `No recent ${LABELS[state.pollutant]} reading for this station.`;
    $('#hero-stamp').textContent = '';
    bar.innerHTML = CATEGORIES.map(() => '<span></span>').join('');
    return;
  }

  gv.textContent = num(reading.value, reading.value >= 100 ? 0 : 1);
  const si = subIndex(state.pollutant, reading.value);
  const circ = 2 * Math.PI * 52;
  const frac = si ? Math.min(si.subIndex / 500, 1) : 0;
  arc.setAttribute('stroke-dasharray', `${(frac * circ).toFixed(1)} ${circ.toFixed(1)}`);

  const cat = si ? si.category : '—';
  const level = si ? si.level : 0;
  $('#hero-cat-label').textContent = si ? `${cat}` : 'Reading';
  $('#hero-desc').textContent = si ? `AQI sub-index ${si.subIndex}. ${CAT_DESC[cat]}` : 'Latest reading.';
  $('#hero-stamp').textContent = `Measured ${fmtStamp.format(new Date(reading.measured_at))} IST`;
  bar.innerHTML = CATEGORIES.map((_, i) => `<span class="${i < level ? 'on' : ''}"></span>`).join('');
}

function renderTiles(readings) {
  const box = $('#tiles');
  const byPol = Object.fromEntries(readings.map((r) => [r.pollutant, r]));
  box.innerHTML = '';
  for (const p of POLLUTANTS) {
    const r = byPol[p];
    const has = r && r.value != null;
    const card = document.createElement('button');
    card.className = 'card tile' + (has ? '' : ' null');
    card.type = 'button';
    card.style.cursor = 'pointer';
    card.setAttribute('aria-pressed', String(p === state.pollutant));
    card.innerHTML =
      `<span class="k">${LABELS[p]}</span>` +
      `<span class="v tnum">${has ? num(r.value, r.value >= 100 ? 0 : 1) : '—'}<span class="u">${UNITS[p]}</span></span>` +
      `<span class="k" style="text-transform:none;letter-spacing:0">${has && subIndex(p, r.value) ? subIndex(p, r.value).category : 'no data'}</span>`;
    if (p === state.pollutant) card.style.borderColor = 'var(--gold-deep)';
    card.addEventListener('click', () => selectPollutant(p));
    box.appendChild(card);
  }
}

// ---- Data flows -------------------------------------------------------------

async function loadLive() {
  const sid = state.stationId;
  renderHero(null);
  $('#tiles').innerHTML = Array.from({ length: 6 }, () => '<div class="card tile"><div class="skeleton" style="height:64px"></div></div>').join('');
  try {
    const readings = await api(`/stations/${sid}/live`);
    if (state.stationId !== sid) return; // stale
    renderTiles(readings);
    renderHero(readings.find((r) => r.pollutant === state.pollutant) || null);
  } catch (e) {
    $('#tiles').innerHTML = '';
    renderHero(null);
    $('#hero-desc').textContent = `Could not load live readings (${e.message}).`;
  }
}

async function loadHistory() {
  const sid = state.stationId, pol = state.pollutant;
  const c = $('#history-chart');
  stateBlock(c, 'loading', 'Loading history…', '');
  $('#history-legend').hidden = true;

  const params = new URLSearchParams({ pollutant: pol, hours: '48' });
  if (state.impute) { params.set('impute', 'true'); }
  else if (state.clean) { params.set('clean', 'true'); }

  $('#history-title').textContent = `${LABELS[pol]} · last 48h`;
  $('#history-hint').textContent = state.impute ? 'Cleaned + gap-filled from hour-of-day history'
    : state.clean ? 'Validated, de-outliered and smoothed' : 'Recent measured readings';

  try {
    const rows = await api(`/stations/${sid}/history?${params}`);
    if (state.stationId !== sid || state.pollutant !== pol) return;
    if (!rows.length) { stateBlock(c, 'empty', 'No history yet', `No ${LABELS[pol]} readings in the last 48 hours for this station.`); return; }
    const line = rows.map((r) => ({ t: new Date(r.measured_at), v: r.value, imputed: r.imputed }));
    const showRaw = (state.clean || state.impute) && rows.some((r) => r.raw != null && r.raw !== r.value);
    const raw = showRaw ? rows.map((r) => ({ t: new Date(r.measured_at), v: r.raw })) : null;
    lineChart(c, { line, raw }, { unit: UNITS[pol], ariaLabel: `${LABELS[pol]} history over the last 48 hours` });

    const lg = $('#history-legend');
    lg.hidden = false;
    lg.innerHTML = `<span><i style="background:var(--gold)"></i> ${state.clean || state.impute ? 'Cleaned' : 'Reading'}</span>` +
      (showRaw ? '<span><i style="background:#7a776e"></i> Raw</span>' : '') +
      (state.impute ? '<span><i style="background:var(--gold-soft)"></i> Imputed point</span>' : '');
  } catch (e) {
    stateBlock(c, 'error', 'Could not load history', e.message, loadHistory);
  }
}

async function loadForecast() {
  const sid = state.stationId, pol = state.pollutant;
  const c = $('#forecast-chart');
  stateBlock(c, 'loading', 'Loading forecast…', '');
  $('#forecast-legend').hidden = true;
  try {
    const rows = await api(`/stations/${sid}/forecast?pollutant=${encodeURIComponent(pol)}`);
    if (state.stationId !== sid || state.pollutant !== pol) return;
    if (!rows.length) { stateBlock(c, 'empty', 'No forecast available', `The model has not produced a ${LABELS[pol]} forecast for this station yet.`); return; }
    const line = rows.map((r) => ({ t: new Date(r.target_time), v: r.yhat, band: { lo: r.yhat_lower, hi: r.yhat_upper } }));
    const band = rows.filter((r) => r.yhat_lower != null && r.yhat_upper != null)
      .map((r) => ({ t: new Date(r.target_time), lo: r.yhat_lower, hi: r.yhat_upper }));
    lineChart(c, { line, band, now: new Date() }, { unit: UNITS[pol], ariaLabel: `${LABELS[pol]} 24-hour forecast` });
    $('#forecast-legend').hidden = false;
  } catch (e) {
    stateBlock(c, 'error', 'Could not load forecast', e.message, loadForecast);
  }
}

// Renders the map's severity legend (once).
function buildMapScale() {
  const box = $('#map-scale');
  box.innerHTML = `<b>Good</b><span class="ramp">${GOLD_RAMP.map((c) => `<i style="background:${c}"></i>`).join('')}</span><b>Severe</b>`;
}

// SVG geo-scatter of all stations, positioned by lat/long, sized/lit by AQI.
function renderMap() {
  const c = $('#map');
  const rows = state.overview;
  $('#map-title').textContent = `Delhi network · ${LABELS[state.pollutant]}`;

  if (!rows.length) { stateBlock(c, 'loading', 'Loading map…', ''); return; }
  const located = rows.filter((s) => s.latitude != null && s.longitude != null);
  if (!located.length) { stateBlock(c, 'empty', 'No station coordinates', 'Stations have no latitude/longitude to plot.'); return; }

  const W = 760, H = 420, pad = 34;
  const lats = located.map((s) => s.latitude), lons = located.map((s) => s.longitude);
  const laMin = Math.min(...lats), laMax = Math.max(...lats);
  const loMin = Math.min(...lons), loMax = Math.max(...lons);
  const px = (lon) => pad + ((lon - loMin) / (loMax - loMin || 1)) * (W - 2 * pad);
  const py = (lat) => pad + (1 - (lat - laMin) / (laMax - laMin || 1)) * (H - 2 * pad);

  const svg = el('svg', { viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `Map of ${located.length} Delhi monitoring stations by ${LABELS[state.pollutant]}` });

  // Faint framing + graticule for spatial context.
  svg.appendChild(el('rect', { x: pad, y: pad, width: W - 2 * pad, height: H - 2 * pad, fill: 'none', stroke: GOLD_DEEP, 'stroke-opacity': 0.18, rx: 8 }));
  for (let i = 1; i < 4; i++) {
    const gx = pad + (i / 4) * (W - 2 * pad), gy = pad + (i / 4) * (H - 2 * pad);
    svg.appendChild(el('line', { x1: gx, y1: pad, x2: gx, y2: H - pad, stroke: GOLD_DEEP, 'stroke-opacity': 0.07 }));
    svg.appendChild(el('line', { x1: pad, y1: gy, x2: W - pad, y2: gy, stroke: GOLD_DEEP, 'stroke-opacity': 0.07 }));
  }

  const tip = $('#tip');
  // Draw non-selected first, selected last (on top).
  const ordered = located.slice().sort((a) => (a.id === state.stationId ? 1 : -1));
  for (const s of ordered) {
    const x = px(s.longitude), y = py(s.latitude);
    const si = subIndex(state.pollutant, s.value);
    const selected = s.id === state.stationId;

    if (selected) {
      svg.appendChild(el('circle', { cx: x, cy: y, r: 13, fill: 'none', stroke: GOLD, 'stroke-width': 1.5, 'stroke-opacity': 0.9, style: 'filter:drop-shadow(0 0 6px rgba(255,204,0,0.5))' }));
    }

    let dot;
    if (si) {
      const color = GOLD_RAMP[si.level - 1];
      dot = el('circle', {
        class: 'map-dot', cx: x, cy: y, r: 7, fill: color, stroke: '#000', 'stroke-width': 1.2,
        style: `filter:drop-shadow(0 0 ${2 + si.level}px ${color})`,
      });
    } else {
      // No data: hollow grey ring.
      dot = el('circle', { class: 'map-dot', cx: x, cy: y, r: 5.5, fill: 'none', stroke: '#7a776e', 'stroke-width': 1.4 });
    }

    dot.addEventListener('click', () => { $('#station').value = String(s.id); selectStation(s.id); });
    dot.addEventListener('pointerenter', (ev) => {
      const rect = svg.getBoundingClientRect();
      tip.innerHTML = `<div class="t">${s.name}</div><div class="r">${si ? `<span class="g">${num(s.value)}</span> ${UNITS[state.pollutant]} · ${si.category}` : 'no data'}</div>`;
      tip.style.left = (rect.left + (x / W) * rect.width) + 'px';
      tip.style.top = (rect.top + (y / H) * rect.height) + 'px';
      tip.classList.add('on'); tip.setAttribute('aria-hidden', 'false');
    });
    dot.addEventListener('pointerleave', () => { tip.classList.remove('on'); tip.setAttribute('aria-hidden', 'true'); });
    svg.appendChild(dot);
  }

  c.innerHTML = '';
  c.appendChild(svg);
}

async function loadOverview() {
  const pol = state.pollutant;
  if (!state.overview.length) stateBlock($('#map'), 'loading', 'Loading map…', '');
  try {
    const rows = await api(`/overview?pollutant=${encodeURIComponent(pol)}`);
    if (state.pollutant !== pol) return; // stale
    state.overview = rows;
    renderMap();
  } catch (e) {
    if (!state.overview.length) stateBlock($('#map'), 'error', 'Could not load map', e.message, loadOverview);
  }
}

function renderMetrics(m) {
  const unit = UNITS[state.pollutant];
  const cells = [
    ['MAE', num(m.mae), unit, 'Mean absolute error — average miss, in pollutant units'],
    ['RMSE', num(m.rmse), unit, 'Root mean squared error — penalises big misses'],
    ['MAPE', m.mape != null ? num(m.mape) : '—', '%', 'Mean absolute percentage error'],
    ['Coverage', m.coverage != null ? num(m.coverage, 0) : '—', '%', 'Share of actuals that fell inside the uncertainty band'],
  ];
  $('#acc-metrics').innerHTML = cells.map(([k, v, u, t]) =>
    `<div class="acc-metric" title="${t}"><span class="k">${k}</span><span class="v tnum">${v}<span class="u">${u}</span></span></div>`).join('');
}

async function loadAccuracy() {
  const sid = state.stationId, pol = state.pollutant;
  const c = $('#acc-chart');
  stateBlock(c, 'loading', 'Scoring forecast…', '');
  $('#acc-metrics').innerHTML = ''; $('#acc-legend').hidden = true; $('#acc-badge').textContent = '';
  try {
    const res = await api(`/stations/${sid}/accuracy?pollutant=${encodeURIComponent(pol)}&hours=48`);
    if (state.stationId !== sid || state.pollutant !== pol) return;
    const m = res.metrics;
    if (!m.n) {
      stateBlock(c, 'empty', 'No scored forecasts yet', `No past ${LABELS[pol]} forecasts have matching readings here. Run the backtest job to populate.`);
      return;
    }
    renderMetrics(m);
    $('#acc-badge').textContent = `${m.n} hrs scored`;
    const line = res.points.map((p) => ({
      t: new Date(p.target_time), v: p.actual, cmpV: p.yhat,
      err: (p.yhat != null && p.actual != null) ? (p.yhat - p.actual) : null,
    }));
    const cmp = res.points.map((p) => ({ t: new Date(p.target_time), v: p.yhat }));
    const band = res.points.filter((p) => p.yhat_lower != null && p.yhat_upper != null)
      .map((p) => ({ t: new Date(p.target_time), lo: p.yhat_lower, hi: p.yhat_upper }));
    lineChart(c, { line, cmp, band }, { unit: UNITS[pol], ariaLabel: `Predicted vs actual ${LABELS[pol]} over the backtest window` });
    $('#acc-legend').hidden = false;
  } catch (e) {
    stateBlock(c, 'error', 'Could not score forecast', e.message, loadAccuracy);
  }
}

function refreshData() {
  loadLive();
  loadHistory();
  loadForecast();
  loadAccuracy();
}

// ---- Selection handlers -----------------------------------------------------

function selectPollutant(p) {
  if (state.pollutant === p) return;
  state.pollutant = p;
  document.querySelectorAll('#pollutants button').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.p === p)));
  state.overview = [];       // pollutant changed → old map data is stale
  loadOverview();
  refreshData();
}

function selectStation(id) {
  state.stationId = Number(id);
  renderMap();               // move the highlight (cheap, uses cached overview)
  refreshData();
}

// ---- Bootstrap --------------------------------------------------------------

function buildPollutantControl() {
  const box = $('#pollutants');
  box.innerHTML = '';
  for (const p of POLLUTANTS) {
    const b = document.createElement('button');
    b.type = 'button'; b.dataset.p = p; b.textContent = LABELS[p];
    b.setAttribute('aria-pressed', String(p === state.pollutant));
    b.addEventListener('click', () => selectPollutant(p));
    box.appendChild(b);
  }
}

function tickClock() {
  $('#foot-stamp').textContent = `Updated ${fmtStamp.format(new Date())} IST`;
}

// Refresh the fast-moving surfaces (live tiles + hero + map) without disturbing
// the charts or the user's toggle state. Skips work if no station is loaded.
function autoRefresh() {
  tickClock();
  if (state.stationId == null) return;
  loadLive();
  loadOverview();
}

async function init() {
  buildPollutantControl();
  buildMapScale();
  tickClock();
  setInterval(autoRefresh, 60_000);

  const sel = $('#station');
  const tg = { clean: $('#tg-clean'), impute: $('#tg-impute') };

  tg.clean.addEventListener('click', () => {
    state.clean = !state.clean;
    if (!state.clean) state.impute = false;
    syncToggles(tg); loadHistory();
  });
  tg.impute.addEventListener('click', () => {
    state.impute = !state.impute;
    if (state.impute) state.clean = true; // impute implies clean
    syncToggles(tg); loadHistory();
  });

  try {
    const stations = await api('/stations');
    state.stations = stations;
    if (!stations.length) {
      sel.innerHTML = '<option>No stations</option>'; sel.disabled = true;
      stateBlock($('#map'), 'empty', 'No stations registered', 'Run the ingest job to populate monitoring stations.');
      stateBlock($('#history-chart'), 'empty', 'No stations registered', 'Run the ingest job to populate monitoring stations.');
      stateBlock($('#forecast-chart'), 'empty', 'No stations registered', '');
      return;
    }
    sel.innerHTML = stations.map((s) => `<option value="${s.id}">${s.name}</option>`).join('');
    sel.addEventListener('change', (e) => selectStation(e.target.value));
    selectStation(stations[0].id);
    loadOverview();
  } catch (e) {
    sel.innerHTML = '<option>Unavailable</option>'; sel.disabled = true;
    stateBlock($('#map'), 'error', 'Backend unreachable', '');
    stateBlock($('#history-chart'), 'error', 'Backend unreachable', `Could not reach the API (${e.message}). Is the server running?`, () => location.reload());
    stateBlock($('#forecast-chart'), 'error', 'Backend unreachable', '');
  }
}

function syncToggles(tg) {
  tg.clean.setAttribute('aria-pressed', String(state.clean));
  tg.impute.setAttribute('aria-pressed', String(state.impute));
}

init();
