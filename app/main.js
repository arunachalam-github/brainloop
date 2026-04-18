// ─────────────────────────────────────────────────────────────────────
// Brainloop — single-page vanilla-JS frontend for the Tauri shell.
// Talks to Rust commands: row_count, today_summary, daemon_status.
// ─────────────────────────────────────────────────────────────────────

const TABS = ['today', 'chat', 'settings'];

// ─── helpers ─────────────────────────────────────────────────────────
const WEEKDAY_SHORT = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const WEEKDAY_LONG  = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
const MONTH_SHORT   = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const MONTH_LONG    = ['January','February','March','April','May','June','July','August','September','October','November','December'];

function escapeHtml(str) {
  return String(str ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

// "2026-04-18" → Date at local midnight. Avoids timezone shifts.
function parseLocalDate(ymd) {
  if (!ymd) return new Date();
  const [y, m, d] = ymd.split('-').map(Number);
  return new Date(y, (m || 1) - 1, d || 1);
}

function shortDateLabel(date) {
  // e.g. "Fri Apr 18"
  return `${WEEKDAY_SHORT[date.getDay()]} ${MONTH_SHORT[date.getMonth()]} ${date.getDate()}`;
}

function longDateLabel(date) {
  // e.g. "Friday, April 18 2026"
  return `${WEEKDAY_LONG[date.getDay()]}, ${MONTH_LONG[date.getMonth()]} ${date.getDate()} ${date.getFullYear()}`;
}

function subtitleLine(date, switches, focusMin) {
  const weekday = WEEKDAY_LONG[date.getDay()].toLowerCase();
  const month = MONTH_LONG[date.getMonth()].toLowerCase();
  const focus = focusMin != null ? `${Math.floor(focusMin/60)}h${String(focusMin%60).padStart(2,'0')}` : null;
  const parts = [`${weekday}, ${month} ${date.getDate()}`];
  if (switches != null) parts.push(`${switches} switches`);
  if (focus)            parts.push(`${focus} focus`);
  return parts.join(' · ');
}

// Split "monkey" out of the headline so CSS can italicise it terracotta.
function renderHeadline(text) {
  const parts = String(text || '').split(/(monkey)/i);
  return parts.map(p => /^monkey$/i.test(p) ? `<em>${escapeHtml(p)}</em>` : escapeHtml(p)).join('');
}

function timeFromSecs(s) {
  if (s == null) return '';
  const m = Math.floor(s / 60);
  if (m < 90) return `${m} min`;
  const h = Math.floor(m / 60), rem = m % 60;
  return `${h}h ${String(rem).padStart(2,'0')}`;
}

// ─── Tauri-safe invoke wrapper ───────────────────────────────────────
// `window.__TAURI__` is injected asynchronously — it's NOT guaranteed to be
// present at DOMContentLoaded. Poll briefly for the bridge so our first
// invoke doesn't fail in a race condition. In a plain file:// browser
// preview the bridge will never arrive and we bail cleanly.
async function waitForTauriBridge(timeoutMs = 3000) {
  const start = performance.now();
  while (performance.now() - start < timeoutMs) {
    const t = window.__TAURI__;
    if (t && t.core && typeof t.core.invoke === 'function') return t;
    await new Promise(r => setTimeout(r, 50));
  }
  return null;
}

async function invokeCmd(name, args) {
  const tauri = await waitForTauriBridge();
  if (!tauri) {
    const err = new Error('tauri-not-available');
    console.warn(`[invoke ${name}] bridge never appeared`);
    throw err;
  }
  try {
    return await tauri.core.invoke(name, args);
  } catch (err) {
    console.warn(`[invoke ${name}] failed:`, err);
    throw err;
  }
}

// ─── Tab switching ───────────────────────────────────────────────────
function switchTab(name) {
  if (!TABS.includes(name)) return;
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.screen').forEach(s => s.classList.toggle('active', s.id === `screen-${name}`));
}

function wireTabs() {
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => switchTab(tab.dataset.tab));
  });

  document.addEventListener('keydown', (e) => {
    // Don't steal arrow keys while the user is typing.
    const tag = (e.target && e.target.tagName) || '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || (e.target && e.target.isContentEditable)) return;
    if (!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)) return;
    e.preventDefault();
    const active = document.querySelector('.tab.active');
    const current = TABS.indexOf(active?.dataset.tab);
    if (current === -1) return;
    const next = (e.key === 'ArrowDown' || e.key === 'ArrowRight')
      ? Math.min(current + 1, TABS.length - 1)
      : Math.max(current - 1, 0);
    switchTab(TABS[next]);
  });
}

// ─── Seismograph waveform ────────────────────────────────────────────
// Continuously animated line: each bucket has a state-derived amplitude,
// frequency, and scroll speed. Calm segments barely breathe; chaotic ones
// visibly flow. Every bucket is also envelope-faded at its edges so
// neighbours stitch together without visible seams.
//
// Last painted layout is stashed on the canvas so the hover handler can
// hit-test without recomputing.
// Per-state parameters ported verbatim from the Stitch reference so the
// wave reads as clean sinusoids rather than noise. Two things matter:
//   - Nyquist: the chaotic freq range (8–16 cycles/bucket) needs at least
//     ~6 pts/cycle to render smoothly, so PTS_PER_BUCKET must be ~100.
//   - Speed is in radians/sec of phase drift. Calm barely breathes,
//     chaotic visibly flows.
const PTS_PER_BUCKET = 100;

// Tiny deterministic PRNG so repaints on resize keep the same phases.
function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function paintWaveform(canvas, buckets) {
  if (!canvas) return;
  buckets = Array.isArray(buckets) ? buckets : [];

  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 1104;
  const cssH = 190;
  canvas.style.height = cssH + 'px';
  canvas.width  = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const paddingX = 4;
  const availW   = cssW - paddingX * 2;
  const midY     = cssH / 2;
  const halfH    = (cssH / 2) - 10;
  const maxCount = Math.max(1, ...buckets.map(b => b?.count || 0));
  const bucketW  = availW / Math.max(buckets.length, 1);

  // Per-bucket parameters: frequency, amplitude, scroll speed, two random
  // phase offsets so neighbouring chaotic buckets don't read as one cosine.
  // Reference-target visual density: roughly one visible cycle per bucket
  // for busy, two for chaotic. At ~12 px bucket width that yields 4–8 px per
  // cycle — enough room for the stroke to trace a legible sine rather than
  // collapsing into vertical streaks. Amplitude caps at ~55% of halfH so
  // peaks don't touch the canvas edges.
  const rnd = mulberry32(137);
  const params = buckets.map(b => {
    const state = b?.state || 'empty';
    const tN    = (b?.count || 0) / maxCount;
    const freq =
      state === 'empty'   ? 0 :
      state === 'calm'    ? 0.5 + tN * 0.4 :
      state === 'busy'    ? 1.0 + tN * 0.8 :
      /* chaotic */         2.0 + tN * 2.0;
    const amp =
      state === 'empty'   ? 0 :
      state === 'calm'    ? halfH * (0.06 + tN * 0.06) :
      state === 'busy'    ? halfH * (0.18 + tN * 0.14) :
      /* chaotic */         halfH * (0.32 + tN * 0.22);
    const speed =
      state === 'calm'    ? 0.35 + tN * 0.20 :
      state === 'busy'    ? 0.60 + tN * 0.40 :
      state === 'chaotic' ? 1.10 + tN * 0.70 :
      /* empty */           0;
    return { freq, amp, speed, ph1: rnd() * Math.PI * 2, ph2: rnd() * Math.PI * 2, state };
  });

  // Cache hit-test geometry for hover.
  canvas._viz = { buckets, paddingX, bucketW, midY, cssW, cssH };

  // Cancel any previous RAF loop still running on this canvas (e.g. resize).
  if (canvas._raf) cancelAnimationFrame(canvas._raf);

  const RISE_MS = 1100;
  const startMs = performance.now();

  function frame(ts) {
    const elapsed = (ts - startMs) / 1000;
    const riseProgress = Math.min(1, (ts - startMs) / RISE_MS);
    const rise = 1 - Math.pow(1 - riseProgress, 3); // easeOutCubic

    ctx.clearRect(0, 0, cssW, cssH);

    // Dotted baseline.
    ctx.save();
    ctx.setLineDash([2, 10]);
    ctx.strokeStyle = 'rgba(34,26,18,0.09)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(paddingX, midY + 0.5);
    ctx.lineTo(cssW - paddingX, midY + 0.5);
    ctx.stroke();
    ctx.restore();

    // Waveform path.
    ctx.strokeStyle = 'rgba(34,26,18,0.72)';
    ctx.lineWidth = 1.3;
    ctx.lineJoin = 'round';
    ctx.lineCap  = 'round';
    ctx.beginPath();

    let first = true;
    for (let i = 0; i < buckets.length; i++) {
      const p = params[i];
      for (let pt = 0; pt <= PTS_PER_BUCKET; pt++) {
        const frac = pt / PTS_PER_BUCKET;
        const x = paddingX + (i + frac) * bucketW;
        let y = midY;
        if (p.freq > 0) {
          // Edge envelope: ramp up in first 1/6, ramp down in last 1/6 of bucket.
          const env = Math.min(frac * 6, 1) * Math.min((1 - frac) * 6, 1);
          const wave = Math.sin(frac * p.freq * Math.PI * 2 + elapsed * p.speed + p.ph1);
          const harm = Math.sin(frac * p.freq * 1.7 * Math.PI * 2 + elapsed * p.speed * 1.3 + p.ph2) * 0.35;
          y = midY - (wave + harm) * p.amp * env * rise;
        }
        if (first) { ctx.moveTo(x, y); first = false; }
        else       { ctx.lineTo(x, y); }
      }
    }
    ctx.stroke();

    canvas._raf = requestAnimationFrame(frame);
  }

  canvas._raf = requestAnimationFrame(frame);
}

// ─── Hour tick labels under the canvas ───────────────────────────────
function renderHourTicks(container, buckets) {
  if (!container) return;
  if (!Array.isArray(buckets) || buckets.length === 0) {
    container.innerHTML = '';
    return;
  }
  const firstTs = buckets[0].start_ts;
  const lastTs  = buckets[buckets.length - 1].start_ts + 600; // +10 min
  const spanSec = Math.max(1, lastTs - firstTs);

  // Pick tick cadence: aim for 4-7 visible labels across the span.
  const hoursSpan = spanSec / 3600;
  const step = hoursSpan >= 12 ? 2 : 1;

  const firstHour = new Date(firstTs * 1000);
  firstHour.setMinutes(0, 0, 0);
  // Round forward to next hour if firstTs wasn't on the hour.
  if (firstHour.getTime() / 1000 < firstTs) firstHour.setHours(firstHour.getHours() + 1);

  const out = [];
  const startMs = firstHour.getTime();
  for (let t = startMs / 1000; t <= lastTs; t += step * 3600) {
    const d = new Date(t * 1000);
    const h = d.getHours();
    const label = h === 0 ? '12AM' : h === 12 ? '12PM' : h > 12 ? `${h - 12}PM` : `${h}AM`;
    const frac = (t - firstTs) / spanSec;
    out.push(`<span class="tick" style="left: ${Math.max(0, Math.min(1, frac)) * 100}%">${label}</span>`);
  }
  container.innerHTML = out.join('');
}

// ─── Hover tooltip ───────────────────────────────────────────────────
// Shows time range, state · N switches, and — lazily fetched via
// bucket_apps() — top apps the user was in during that bucket. We cache
// per-bucket results on the bucket object to avoid re-querying on every
// mousemove.
const STATE_DOT_COLOR = {
  empty:   'rgba(245,240,232,0.45)',
  calm:    'rgba(245,240,232,0.70)',
  busy:    'rgba(245,240,232,0.88)',
  chaotic: 'rgba(245,240,232,1.00)',
};

function fmtClock(ts) {
  const d = new Date(ts * 1000);
  let h = d.getHours();
  const m = d.getMinutes();
  const ampm = h >= 12 ? 'PM' : 'AM';
  h = h % 12; if (h === 0) h = 12;
  return `${h}:${String(m).padStart(2, '0')} ${ampm}`;
}

function wireVizHover(canvas, tooltip) {
  if (!canvas || !tooltip) return;

  let activeIdx = -1;

  const render = async (b, idx) => {
    tooltip.innerHTML = `
      <div class="t-time">${escapeHtml(fmtClock(b.start_ts))} – ${escapeHtml(fmtClock(b.start_ts + 600))}</div>
      <div class="t-state">
        <span class="t-dot" style="background:${STATE_DOT_COLOR[b.state] || STATE_DOT_COLOR.empty}"></span>
        ${escapeHtml(b.state)} · ${b.count} switch${b.count === 1 ? '' : 'es'}
      </div>
      <div class="t-apps" id="t-apps-${idx}">${b._apps ? escapeHtml(b._apps) : '…'}</div>`;

    if (b._apps !== undefined) return;
    try {
      const res = await invokeCmd('bucket_apps', { startTs: b.start_ts, endTs: b.start_ts + 600 });
      const apps = res?.apps?.map(a => a.app).filter(Boolean) || [];
      b._apps = apps.length ? apps.join(' · ') : '(no capture in this window)';
      if (activeIdx === idx) {
        const el = tooltip.querySelector(`#t-apps-${idx}`);
        if (el) el.textContent = b._apps;
      }
    } catch (_e) {
      b._apps = '';
    }
  };

  canvas.addEventListener('mousemove', (e) => {
    const st = canvas._viz;
    if (!st || !st.buckets.length) return;
    const rect = canvas.getBoundingClientRect();
    const xCss = e.clientX - rect.left;
    const xLocal = xCss - st.paddingX;
    const idx = Math.max(0, Math.min(st.buckets.length - 1, Math.floor(xLocal / st.bucketW)));
    const b = st.buckets[idx];
    if (!b) return;

    if (idx !== activeIdx) {
      activeIdx = idx;
      render(b, idx);
    }

    tooltip.style.display = 'block';
    // Position tooltip relative to viewport; slight offset from cursor.
    const offset = 14;
    const vw = window.innerWidth;
    const ttRect = tooltip.getBoundingClientRect();
    let left = e.clientX + offset;
    if (left + ttRect.width + 8 > vw) left = e.clientX - ttRect.width - offset;
    let top = e.clientY - ttRect.height / 2;
    if (top < 8) top = 8;
    tooltip.style.left = left + 'px';
    tooltip.style.top  = top  + 'px';
  });

  canvas.addEventListener('mouseleave', () => {
    tooltip.style.display = 'none';
    activeIdx = -1;
  });
}

// ─── Today rendering ─────────────────────────────────────────────────
function renderTodayEmpty(root, todayDate) {
  root.innerHTML = `
    <section class="hero reveal">
      <div class="hero-eyebrow">Today · ${escapeHtml(shortDateLabel(todayDate))}</div>
      <h1 class="hero-phrase">Brainloop is still listening. Come back after the next analysis tick — it runs every 30 minutes.</h1>
    </section>`;
}

function renderToday(root, summary) {
  const payload = summary.payload || {};
  const date = parseLocalDate(summary.date);
  const headline = payload.headline || '';
  const totalSwitches = payload.switches_total ?? 0;
  const buckets = Array.isArray(payload.intensity_buckets) ? payload.intensity_buckets : [];
  const acts = Array.isArray(payload.acts) ? payload.acts : [];
  const w = payload.widgets || {};

  // Hero eyebrow derived from date; subtitle line comes from the payload
  // (subtitle) when present, else we compute one.
  const focusMin = w.longest_focus?.minutes ?? null;
  const subtitle = payload.subtitle || subtitleLine(date, totalSwitches, focusMin);

  const actsHtml = acts.map((act, idx) => renderAct(act, idx)).join('');

  const legendHtml = `
    <div class="viz-legend">
      <div><span class="sw" style="background: rgba(34,26,18,0.06);"></span>empty</div>
      <div><span class="sw" style="background: rgba(34,26,18,0.26);"></span>calm · &lt;10 context switches</div>
      <div><span class="sw" style="background: rgba(34,26,18,0.58);"></span>busy · 10–22</div>
      <div><span class="sw" style="background: rgba(34,26,18,0.9);"></span>chaotic · 22+</div>
    </div>`;

  root.innerHTML = `
    <section class="hero reveal">
      <div class="hero-eyebrow">Today · ${escapeHtml(shortDateLabel(date))}</div>
      <h1 class="hero-phrase">${renderHeadline(headline)}</h1>
    </section>

    <section class="section reveal reveal-delay-1">
      <div class="section-label">How your attention moved — ${escapeHtml(totalSwitches)} switches</div>
      ${legendHtml}
      <div class="viz-wrap">
        <canvas id="seismo-canvas" data-viz="waveform"></canvas>
        <div id="viz-ticks" class="viz-ticks"></div>
      </div>
    </section>

    <section class="section reveal reveal-delay-2">
      <div class="section-label">The day in three acts</div>
      <div class="timeline">${actsHtml}</div>
    </section>

    <section class="section reveal reveal-delay-3">
      <div class="section-label">Brain widgets</div>
      ${renderWidgets(w)}
    </section>
  `;

  // Paint once after the canvas is in the DOM and has a size, and wire the
  // hover tooltip. Tick labels are rendered from the same bucket list.
  requestAnimationFrame(() => {
    const canvas = document.getElementById('seismo-canvas');
    paintWaveform(canvas, buckets);
    renderHourTicks(document.getElementById('viz-ticks'), buckets);
    wireVizHover(canvas, document.getElementById('viz-tooltip'));
  });

  // Repaint on resize — debounced.
  if (!window._seismoResizeWired) {
    let t = null;
    window.addEventListener('resize', () => {
      clearTimeout(t);
      t = setTimeout(() => {
        const c = document.getElementById('seismo-canvas');
        if (c) paintWaveform(c, buckets);
        renderHourTicks(document.getElementById('viz-ticks'), buckets);
      }, 180);
    });
    window._seismoResizeWired = true;
  }
}

function renderAct(act, idx) {
  const callouts = Array.isArray(act.callouts) ? act.callouts : [];
  const monkeys = callouts.filter(c => /gratification\s*monkey/i.test(c.label || ''));
  const monkeysHtml = monkeys.map(monkey => `
    <div class="monkey">
      <div class="monkey-header">
        <span class="monkey-name">Gratification Monkey</span>
        <span class="monkey-meta">${escapeHtml(monkey.time || '')}${monkey.duration_min != null ? ' · ' + escapeHtml(monkey.duration_min) + ' min' : ''}</span>
      </div>
      <div class="monkey-sentence">${escapeHtml(monkey.body || '')}</div>
    </div>`).join('');

  return `
    <div class="period">
      <div class="period-time">
        <div class="dot"></div>
        <div class="label">${escapeHtml(act.title || '')}</div>
        <div class="range">${escapeHtml(act.time_range || '')}</div>
      </div>
      <div class="period-prose">
        <div class="period-line1">${escapeHtml(act.one_liner || '')}</div>
        <div class="period-line2">${escapeHtml(act.narrative || '')}</div>
      </div>
      ${monkeysHtml}
    </div>`;
}

function renderWidgets(w) {
  const focus = w.longest_focus || {};
  const doom = w.doom_scroll || {};
  const hours = Array.isArray(w.hours_by_app) ? w.hours_by_app.slice(0, 8) : [];
  const calls = w.on_calls || { count: 0, minutes: 0 };
  const waitingAi = w.waiting_on_ai || {};
  const breaks = Array.isArray(w.breaks) ? w.breaks : [];
  const reading = Array.isArray(w.things_read) ? w.things_read : [];

  const maxMins = Math.max(1, ...hours.map(h => h.minutes || 0));
  const appBars = hours.map((a, i) => {
    const pct = Math.max(2, Math.round(((a.minutes || 0) / maxMins) * 100));
    return `
      <div class="app-bar">
        <div class="name">${escapeHtml(a.app || '')}</div>
        <div class="bar"><span style="width: ${pct}%; animation-delay: ${i * 120}ms;"></span></div>
        <div class="mins">${escapeHtml(a.minutes || 0)}m</div>
      </div>`;
  }).join('');

  const breakRows = breaks.length
    ? breaks.map(b => `
        <div class="break-chip">
          <div class="t">${escapeHtml(b.start || '')}</div>
          <div class="kind">${escapeHtml(b.kind || (b.end ? ('until ' + b.end) : 'break'))}</div>
          <div class="len">${escapeHtml(b.minutes || 0)}m</div>
        </div>`).join('')
    : `<div class="widget-sub" style="margin-top: 4px;">No real breaks recorded.</div>`;

  const readRows = reading.length
    ? reading.map(r => `
        <div class="read-row">
          <div class="t">${escapeHtml(r.time || '')}</div>
          <div class="title">${escapeHtml(r.title || '')}</div>
          <div class="where">${escapeHtml(r.source || '')}</div>
        </div>`).join('')
    : `<div class="widget-sub" style="margin-top: 4px;">Nothing caught yet.</div>`;

  return `
    <div class="widgets">
      <div class="widget w-6">
        <div class="widget-label">Longest focus</div>
        <div class="widget-primary">${escapeHtml(focus.minutes ?? 0)}<span class="unit">minutes</span></div>
        <div class="widget-sub">${escapeHtml(focus.label || '—')}</div>
        <div class="widget-foot">${escapeHtml(focus.range || '')}</div>
      </div>

      <div class="widget w-6">
        <div class="widget-label">Doom-scroll</div>
        <div class="widget-primary" style="color: var(--monkey);">${escapeHtml(doom.minutes ?? 0)}<span class="unit" style="color: var(--monkey);">min total</span></div>
        <div class="widget-sub">${escapeHtml(doom.detail || '—')}</div>
        <div class="widget-foot">worst: ${escapeHtml(doom.worst_range || '')}${doom.moments != null ? ' · ' + escapeHtml(doom.moments) + ' moments' : ''}</div>
      </div>

      <div class="widget w-8">
        <div class="widget-label">Where the hours went</div>
        <div style="margin-top: 6px;">${appBars || '<div class="widget-sub">No apps logged.</div>'}</div>
      </div>

      <div class="widget w-4">
        <div class="widget-label">On calls</div>
        <div class="widget-primary">${escapeHtml(calls.count ?? 0)}<span class="unit">calls · ${escapeHtml(calls.minutes ?? 0)}m</span></div>
        <div style="margin-top: 10px; display: flex; flex-direction: column; gap: 8px;"></div>
      </div>

      <div class="widget w-4">
        <div class="widget-label">Waiting on AI</div>
        <div class="widget-primary">${escapeHtml(waitingAi.minutes ?? 0)}<span class="unit">min</span></div>
        <div class="widget-sub">${escapeHtml(waitingAi.detail || '—')}</div>
        <div class="widget-foot">${escapeHtml(waitingAi.sessions ?? 0)} sessions</div>
      </div>

      <div class="widget w-4">
        <div class="widget-label">Breaks taken</div>
        <div class="break-chips" style="margin-top: 4px;">${breakRows}</div>
      </div>

      <div class="widget w-4">
        <div class="widget-label">Things you read</div>
        <div style="margin-top: 4px;">${readRows}</div>
      </div>
    </div>`;
}

// ─── Today load ──────────────────────────────────────────────────────
async function loadToday() {
  const root = document.getElementById('today-content');
  const todayDate = new Date();
  // Render empty immediately so first paint is not a flash.
  renderTodayEmpty(root, todayDate);

  try {
    const summary = await invokeCmd('today_summary');
    if (summary && summary.payload) {
      renderToday(root, summary);
    } else {
      renderTodayEmpty(root, todayDate);
    }
  } catch (err) {
    // Surface the failure reason in the empty-state headline so bugs are
    // visible without a devtools console.
    root.innerHTML = `
      <section class="hero reveal">
        <div class="hero-eyebrow">Today · ${escapeHtml(shortDateLabel(todayDate))}</div>
        <h1 class="hero-phrase">Couldn't read today's summary. ${escapeHtml(String(err && err.message ? err.message : err))}</h1>
      </section>`;
  }
}

// ─── Chat (visual stub) ──────────────────────────────────────────────
function wireChat() {
  const body = document.getElementById('chat-body');
  const input = document.getElementById('chat-input');
  const send = document.getElementById('chat-send');
  const suggestions = document.getElementById('chat-suggestions');

  function append(role, text) {
    const el = document.createElement('div');
    el.className = `chat-msg ${role}`;
    el.textContent = text;
    body.appendChild(el);
    body.scrollTop = body.scrollHeight;
  }

  function submit(textIn) {
    const text = (textIn ?? input.value).trim();
    if (!text) return;
    append('user', text);
    input.value = '';
    if (suggestions) suggestions.style.display = 'none';
    setTimeout(() => append('bot', 'Chat is coming soon'), 600);
  }

  if (send) send.addEventListener('click', () => submit());
  if (input) input.addEventListener('keydown', (e) => { if (e.key === 'Enter') submit(); });
  if (suggestions) {
    suggestions.querySelectorAll('.chat-suggestion').forEach(btn => {
      btn.addEventListener('click', () => submit(btn.textContent.trim()));
    });
  }
}

// ─── Settings wiring ─────────────────────────────────────────────────
const PROVIDER_PRESETS = {
  anthropic: { label: 'Anthropic', model: 'claude-sonnet-4-5',  baseUrl: 'https://api.anthropic.com' },
  openai:    { label: 'OpenAI',    model: 'gpt-4o',             baseUrl: 'https://api.openai.com/v1' },
  gemini:    { label: 'Gemini',    model: 'gemini-2.0-flash',   baseUrl: 'https://generativelanguage.googleapis.com' },
};

function wireSettings() {
  const tabs = document.querySelectorAll('.provider-tab');
  const apiKey  = document.getElementById('set-api-key');
  const modelEl = document.getElementById('set-model');
  const baseEl  = document.getElementById('set-base-url');

  const state = { provider: 'anthropic' };

  function applyProvider(p) {
    state.provider = p;
    const preset = PROVIDER_PRESETS[p];
    if (!preset) return;
    tabs.forEach(t => t.classList.toggle('active', t.dataset.provider === p));
    if (apiKey)  apiKey.placeholder = `sk-…  (${preset.label} key)`;
    if (modelEl) modelEl.value = preset.model;
    if (baseEl)  baseEl.value  = preset.baseUrl;
  }

  tabs.forEach(t => t.addEventListener('click', () => applyProvider(t.dataset.provider)));
  applyProvider('anthropic');

  const exp = document.getElementById('btn-export-today');
  const del = document.getElementById('btn-delete-all');
  if (exp) exp.addEventListener('click', () => alert('Export today — coming soon.'));
  if (del) del.addEventListener('click', () => alert('Delete all data — coming soon.'));
}

// Format a "running since HH:MM" or "silent for N min" line for the
// daemon row. `ageSecs` is how old the last row is.
function daemonDescLine(running, ageSecs) {
  if (ageSecs == null) return 'No activity captured yet.';
  if (running) {
    const now = new Date();
    const since = new Date(now.getTime() - ageSecs * 1000);
    const hh = String(since.getHours()).padStart(2, '0');
    const mm = String(since.getMinutes()).padStart(2, '0');
    return `Heartbeat ${Math.max(0, Math.round(ageSecs))}s ago · last row at ${hh}:${mm} · checks every 60s`;
  } else {
    const mins = Math.max(1, Math.round(ageSecs / 60));
    return `Silent for ${mins} min — daemon may be stopped.`;
  }
}

async function loadDaemonStatus() {
  const badge = document.getElementById('daemon-badge');
  const desc  = document.getElementById('daemon-desc');
  try {
    const status = await invokeCmd('daemon_status');
    if (!status) throw new Error('no status');
    if (status.running) {
      badge.className = 'perm-badge ok';
      badge.textContent = 'running';
    } else {
      badge.className = 'perm-badge stopped';
      badge.textContent = 'stopped';
    }
    desc.textContent = daemonDescLine(status.running, status.last_row_age_secs);
  } catch (_e) {
    badge.className = 'perm-badge warn';
    badge.textContent = 'unknown';
    desc.textContent = 'Status unavailable (daemon bridge not reachable).';
  }
}

// ─── Bootstrap ───────────────────────────────────────────────────────
function setTopbarDate() {
  const meta = document.getElementById('meta-date');
  if (meta) meta.textContent = longDateLabel(new Date());
}

document.addEventListener('DOMContentLoaded', () => {
  setTopbarDate();
  wireTabs();
  wireChat();
  wireSettings();
  loadToday();
  loadDaemonStatus();
});
