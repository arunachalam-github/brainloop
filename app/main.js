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

// ─── Seismo canvas paint ─────────────────────────────────────────────
// Vertical bars — one per bucket — coloured by `state`, height encoding
// `count`. Animates rise on first paint (1200ms), then stays still.
const STATE_ALPHA = { empty: 0.06, calm: 0.26, busy: 0.58, chaotic: 0.9 };
const STATE_HEIGHT_MIN = { empty: 0.06, calm: 0.22, busy: 0.58, chaotic: 1.0 };

function paintSeismo(canvas, buckets) {
  if (!canvas) return;
  if (!Array.isArray(buckets) || buckets.length === 0) {
    // Paint a thin baseline so the canvas isn't a blank square.
    buckets = [];
  }
  const dpr = window.devicePixelRatio || 1;
  const cssW = canvas.clientWidth || 1104;
  const cssH = 190;
  canvas.style.height = cssH + 'px';
  canvas.width  = Math.round(cssW * dpr);
  canvas.height = Math.round(cssH * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  const n = Math.max(buckets.length, 1);
  const maxCount = Math.max(1, ...buckets.map(b => b?.count || 0));
  const paddingX = 2;
  const availW = cssW - paddingX * 2;
  const barSlot = availW / n;
  const barWidth = Math.max(3, Math.min(18, barSlot - 3));
  const baselineY = cssH - 8;
  const topMargin = 14;
  const usableH = baselineY - topMargin;

  const start = performance.now();
  const DURATION = 1200;

  // Cancel any previous RAF loop still running on this canvas.
  if (canvas._raf) cancelAnimationFrame(canvas._raf);

  function frame(t) {
    const progress = Math.min(1, (t - start) / DURATION);
    const eased = 1 - Math.pow(1 - progress, 3); // easeOutCubic

    ctx.clearRect(0, 0, cssW, cssH);

    // faint baseline
    ctx.strokeStyle = 'rgba(34,26,18,0.12)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, baselineY + 0.5);
    ctx.lineTo(cssW, baselineY + 0.5);
    ctx.stroke();

    for (let i = 0; i < buckets.length; i++) {
      const b = buckets[i] || {};
      const state = b.state || 'empty';
      const alpha = STATE_ALPHA[state] ?? 0.06;

      // height normalisation: combine absolute count with state floor.
      const stateFloor = STATE_HEIGHT_MIN[state] ?? 0.06;
      const countNorm = (b.count || 0) / maxCount;
      const h = Math.max(stateFloor, countNorm) * usableH * eased;

      const xCenter = paddingX + (i + 0.5) * barSlot;
      const x = xCenter - barWidth / 2;
      const y = baselineY - h;

      ctx.fillStyle = `rgba(34,26,18,${alpha})`;
      // tiny rounded tops
      const r = Math.min(barWidth / 2, 2);
      ctx.beginPath();
      ctx.moveTo(x, baselineY);
      ctx.lineTo(x, y + r);
      ctx.quadraticCurveTo(x, y, x + r, y);
      ctx.lineTo(x + barWidth - r, y);
      ctx.quadraticCurveTo(x + barWidth, y, x + barWidth, y + r);
      ctx.lineTo(x + barWidth, baselineY);
      ctx.closePath();
      ctx.fill();
    }

    if (progress < 1) {
      canvas._raf = requestAnimationFrame(frame);
    } else {
      canvas._raf = null;
    }
  }

  canvas._raf = requestAnimationFrame(frame);
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
        <canvas id="seismo-canvas" data-viz="seismo"></canvas>
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

  // Paint once after the canvas is in the DOM and has a size.
  requestAnimationFrame(() => {
    const canvas = document.getElementById('seismo-canvas');
    paintSeismo(canvas, buckets);
  });

  // repaint on resize — debounced.
  if (!window._seismoResizeWired) {
    let t = null;
    window.addEventListener('resize', () => {
      clearTimeout(t);
      t = setTimeout(() => {
        const c = document.getElementById('seismo-canvas');
        if (c) paintSeismo(c, buckets);
      }, 180);
    });
    window._seismoResizeWired = true;
  }
}

function renderAct(act, idx) {
  const callouts = Array.isArray(act.callouts) ? act.callouts : [];
  const monkey = callouts.find(c => /gratification\s*monkey/i.test(c.label || ''));
  const monkeyHtml = monkey ? `
    <div class="monkey">
      <div class="monkey-header">
        <span class="monkey-name">Gratification Monkey</span>
        <span class="monkey-meta">${escapeHtml(monkey.time || '')}${monkey.duration_min != null ? ' · ' + escapeHtml(monkey.duration_min) + ' min' : ''}</span>
      </div>
      <div class="monkey-sentence">${escapeHtml(monkey.body || '')}</div>
    </div>` : '';

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
      ${monkeyHtml}
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
