"""
brainloop.analyze
~~~~~~~~~~~~~~~~~
Scheduled day-summary analyzer.

Runs inside the capture daemon on a 30-min CFRunLoopTimer (see
`daemon.py`). Each tick:

  1. reads app_config + loads the API key from Keychain,
  2. aggregates today's activity_log rows into a compact context dict,
  3. sends it to an OpenAI-compatible or Anthropic endpoint,
  4. writes the structured JSON payload into `day_summary` (PK = date),
     overwriting in place as the day evolves.

Everything is best-effort: any failure (no config, no network, bad JSON)
logs and returns silently so the capture loop is never affected.

Also exposes a `python -m daemon.analyze --once` entrypoint for manual
runs during development.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import providers
from .config import (
    ANALYZER_IDLE_SKIP_MINS,
    ANALYZER_KEYCHAIN_SERVICE,
    ANALYZER_MAX_DWELLS,
    ANALYZER_MIN_REGEN_SECS,
    ANALYZER_PAGE_TEXT_CHARS,
    DB_PATH,
)
from .prompts import PAYLOAD_SCHEMA, SYSTEM_PROMPT, user_message

log = logging.getLogger("brainloop.analyze")


# ── Public entry point (called by the daemon's timer) ─────────────────────────

def tick(conn: sqlite3.Connection, now_ts: float | None = None, force: bool = False) -> None:
    """One scheduled pass. Silent on missing config, idle days, or network errors.

    `force=True` bypasses the min-regen and new-row gates (useful from the CLI
    when iterating on prompts). Config/key gates are always enforced.
    """
    now_ts = now_ts or time.time()
    local_tz = _local_tz()
    today = datetime.fromtimestamp(now_ts, tz=local_tz).strftime("%Y-%m-%d")

    # Gate 1: config must be present.
    cfg = _load_config(conn)
    if not cfg:
        log.debug("analyzer skip: ai config not set in app_config")
        return
    # Key resolution: prefer the value saved from Settings UI into app_config.
    # Fall back to Keychain for users who configured via `security add-generic-password`.
    api_key = cfg.get("ai_api_key") or _load_api_key(
        cfg.get("ai_key_ref") or cfg.get("ai_provider") or ""
    )
    if not api_key:
        log.debug("analyzer skip: no api key in app_config or Keychain")
        return

    # Gate 2: don't regenerate too often (skipped under --force).
    prev = conn.execute(
        "SELECT generated_at FROM day_summary WHERE date = ?", (today,)
    ).fetchone()
    if not force and prev and (now_ts - prev[0]) < ANALYZER_MIN_REGEN_SECS:
        log.debug("analyzer skip: regenerated %ds ago", int(now_ts - prev[0]))
        return

    # Gate 3: last activity row mustn't be stale (user away/asleep).
    last_row = conn.execute("SELECT MAX(ts) FROM activity_log").fetchone()
    last_ts = last_row[0] if last_row and last_row[0] else 0
    if (now_ts - last_ts) > ANALYZER_IDLE_SKIP_MINS * 60:
        log.debug("analyzer skip: no activity for %d min", int((now_ts - last_ts) / 60))
        return

    # Gate 4: require at least one new row since the previous run (skipped under --force).
    if not force and prev:
        new_rows = conn.execute(
            "SELECT COUNT(*) FROM activity_log WHERE ts >= ?", (prev[0],)
        ).fetchone()[0]
        if new_rows == 0:
            log.debug("analyzer skip: no new activity_log rows since last tick")
            return

    # Build the context and call the LLM.
    context = build_context(conn, today, local_tz, now_ts)
    if context["total_rows"] == 0:
        log.info("analyzer skip: no activity rows for %s yet", today)
        return

    started = time.time()
    try:
        payload, tok_in, tok_out = providers.call(
            provider=cfg["ai_provider"],
            base_url=cfg["ai_base_url"],
            api_key=api_key,
            model=cfg["ai_model"],
            system=SYSTEM_PROMPT,
            user=user_message(context),
            schema=PAYLOAD_SCHEMA,
        )
    except providers.LLMError as e:
        log.warning("analyzer tick failed: %s", e)
        return

    _sanitize_payload(payload)

    duration = time.time() - started

    conn.execute(
        """
        INSERT INTO day_summary (date, generated_at, model, activity_rows, payload_json, tokens_in, tokens_out)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(date) DO UPDATE SET
            generated_at=excluded.generated_at,
            model=excluded.model,
            activity_rows=excluded.activity_rows,
            payload_json=excluded.payload_json,
            tokens_in=excluded.tokens_in,
            tokens_out=excluded.tokens_out
        """,
        (today, int(now_ts), cfg["ai_model"], context["total_rows"],
         json.dumps(payload), tok_in, tok_out),
    )
    conn.commit()
    log.info(
        "analyzer tick OK date=%s rows=%d model=%s tokens=%d→%d duration=%.1fs",
        today, context["total_rows"], cfg["ai_model"], tok_in, tok_out, duration,
    )


# ── Manual refresh (UI-triggered) ─────────────────────────────────────────────

def check_manual_request(conn: sqlite3.Connection, now_ts: float | None = None) -> None:
    """Observe + serve a pending manual-refresh request from the UI.

    The Tauri command `analyze_now` writes `analyze_requested_at = <unix_ts>`
    into app_config. We compare against `analyze_served_at` and, if a request
    is pending, run one forced tick and stamp served_at to the request ts.
    Silent on any error — the 30-min timer continues regardless.
    """
    try:
        rows = conn.execute(
            "SELECT key, value FROM app_config "
            "WHERE key IN ('analyze_requested_at','analyze_served_at')"
        ).fetchall()
        kv = {k: v for k, v in rows}
        req = int(kv.get("analyze_requested_at") or 0)
        served = int(kv.get("analyze_served_at") or 0)
        if req == 0 or req <= served:
            return
        log.info("manual analyzer request seen (req=%d served=%d)", req, served)
        tick(conn, now_ts=now_ts, force=True)
        # Stamp served_at even when tick returned early (no key, idle, etc.)
        # so we don't re-fire on the same request every 5s. The UI's poll
        # timeout is how the user sees the failure.
        conn.execute(
            "INSERT INTO app_config(key,value) VALUES('analyze_served_at',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(req),),
        )
        conn.commit()
    except Exception:
        log.exception("check_manual_request raised — suppressing")


# ── Payload sanitizer ─────────────────────────────────────────────────────────

_BROWSER_SOURCES = {
    "comet", "chrome", "google chrome", "safari", "arc", "brave", "brave browser",
    "firefox", "edge", "microsoft edge", "opera", "vivaldi", "chromium",
    "duckduckgo",
}


# Only a small nudge: pretty-names for the handful of domains whose display
# form isn't obvious from the registrable domain ("x.com" → "X / Twitter",
# "news.ycombinator.com" → "Hacker News"). Everything else gets title-cased
# automatically — "crunchyroll.com" → "Crunchyroll", "medium.com" → "Medium".
# Adding new sites is a one-line entry when a user cares.
_DOMAIN_ALIASES = {
    "youtube.com":         "YouTube",
    "youtu.be":            "YouTube",
    "x.com":               "X / Twitter",
    "twitter.com":         "X / Twitter",
    "news.ycombinator.com": "Hacker News",
    "en.wikipedia.org":    "Wikipedia",
}


def _extract_platform(url: str | None) -> str:
    """Derive a platform label from a URL. Empty string when we have no URL.

    The result is ground truth (comes from the actual domain), so the LLM
    doesn't need to guess. For URLs we don't have aliases for, fall through
    to a title-cased registrable domain — "crunchyroll.com" → "Crunchyroll".
    """
    if not url:
        return ""
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower().strip()
        if host.startswith("www."):
            host = host[4:]
        if not host:
            return ""
        if host in _DOMAIN_ALIASES:
            return _DOMAIN_ALIASES[host]
        # Drop common TLDs for a cleaner display name. "crunchyroll.com" →
        # "Crunchyroll", "example.co.uk" → "Example" — good enough for the
        # "things you read" widget; exact strings can be added to the alias
        # map later if the auto-derivation looks wrong.
        parts = host.split(".")
        if len(parts) >= 2:
            stem = parts[-2]
        else:
            stem = parts[0]
        return stem[:1].upper() + stem[1:] if stem else ""
    except Exception:
        return ""


def _infer_source_from_title(title: str) -> str:
    """Last-ditch platform guess from a content title alone.

    Used when both `platform` (URL-derived ground truth) and the LLM's own
    inference came back empty — typically because the dwell happened in a
    browser whose URL the daemon couldn't read at the time (Comet pre-fix,
    or any user with AX denied). Better to surface a confident-but-generic
    label than to leave line 2 reading just "08:25" with no source at all.

    Order matters: more specific patterns first (Substack-style author posts,
    Reddit r/X, GitHub PR/Issue) before falling back to generic "Video" or
    "Article".
    """
    t = title.strip()
    if not t:
        return ""
    low = t.lower()

    # Hacker News landings
    if low in ("hacker news", "hn", "hacker news front page", "hacker news top"):
        return "Hacker News"
    # Reddit pattern: "r/<sub>" anywhere in the title
    import re
    if re.search(r"\br/\w+", t):
        return "Reddit"
    # Twitter/X pattern: leading "@handle"
    if re.match(r"^@\w+", t):
        return "X"
    # GitHub patterns
    if re.search(r"#\d+|\bpull request\b|\bissue\b", low):
        return "GitHub"
    # Substack — "Author Name - Post Title" with capital words on both sides
    # is the giveaway, but author dashes are common to YouTube too. Prefer
    # the YouTube guess for those (videos dominate the dataset).

    # YouTube heuristics:
    #   - "Episode N" or "Ep N" patterns (podcast/series videos)
    #   - "<author> - <title>" with no domain marker (typical YT pattern)
    #   - Anything not matched above with non-trivial length is most likely
    #     a video, given how much of users' browsing time goes to YouTube.
    # Episode N pattern is a high-confidence YouTube/podcast signal that
    # generally holds across browsers — keep it. But avoid wider patterns
    # like "X - Y" or "3+ words → YouTube"; those misfire on Prime Video,
    # Substack posts, podcast pages, etc. Better to leave the source as
    # "Web" (which the UI collapses to time-only) than label something
    # confidently wrong.
    if re.search(r"\bepisode\s+\d+\b|\bep\.?\s*\d+\b", low):
        return "YouTube"

    return "Web"


def _sanitize_payload(payload: dict) -> None:
    """Post-process the LLM payload to shake off a few repeatable model tics.

    1. Relabel browser-app source values ("Chrome", "Comet") in things_read
       to "Web" when platform inference fails upstream.
    2. Backfill empty `source` from a title-only heuristic so the UI never
       shows a row with just a time and a blank source pill.
    3. Deduplicate `acts`: when the day just started, a model will
       sometimes emit two identical "Now" acts (same title + narrative) to
       satisfy prior minimum-count schemas. Drop any act that repeats the
       (title, time_range, narrative) signature of an earlier one.
    """
    try:
        widgets = payload.get("widgets", {})
        items = widgets.get("things_read") or []
    except AttributeError:
        items = []
    for it in items:
        if not isinstance(it, dict):
            continue
        src = (it.get("source") or "").strip()
        # Treat browser-app names AND the generic "Web" placeholder as
        # candidates for upgrade — they're both signals that the LLM
        # couldn't classify and we should try harder from the title alone.
        if not src or src.lower() in _BROWSER_SOURCES or src.lower() == "web":
            inferred = _infer_source_from_title(it.get("title") or "")
            it["source"] = inferred or "Web"
    if "widgets" in payload:
        payload["widgets"]["things_read"] = items

    acts = payload.get("acts")
    if isinstance(acts, list):
        seen: set[tuple[str, str, str]] = set()
        unique: list[dict] = []
        for a in acts:
            if not isinstance(a, dict):
                continue
            sig = (
                (a.get("title") or "").strip().lower(),
                (a.get("time_range") or "").strip(),
                (a.get("narrative") or "").strip()[:120].lower(),
            )
            if sig in seen:
                continue
            seen.add(sig)
            unique.append(a)
        payload["acts"] = unique


# ── Context builder (pure aggregation, no LLM) ────────────────────────────────

def build_context(
    conn: sqlite3.Connection,
    date_str: str,
    tz: ZoneInfo,
    now_ts: float,
) -> dict:
    """Aggregate activity_log rows for `date_str` into a dict the LLM can read.

    The shape is intentionally labeled and compact — every field maps to a
    labeled block in user_message(). No LLM preprocessing; no heuristics for
    "the monkey" or theme-naming — those are the LLM's job.
    """
    day_start = int(datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=tz).timestamp())
    day_end = day_start + 24 * 3600

    rows = conn.execute(
        """
        SELECT ts, trigger, app_name, bundle_id, window_title, browser_url,
               page_text, audio_playing, mic_active
        FROM activity_log
        WHERE ts >= ? AND ts < ?
        ORDER BY ts
        """,
        (day_start, day_end),
    ).fetchall()

    if not rows:
        return {"date": date_str, "total_rows": 0, "intensity_buckets": [],
                "hours_by_app": [], "browser_dwells": [],
                "calls_and_media": [], "breaks": [], "ai_waits": [],
                "weekday": datetime.fromtimestamp(now_ts, tz=tz).strftime("%A").lower(),
                "timezone": str(tz), "now_hhmm":
                    datetime.fromtimestamp(now_ts, tz=tz).strftime("%H:%M"),
                "switches_total": 0}

    # Intensity buckets: 10 minutes each, from day_start to now.
    bucket_end = int(now_ts)
    bucket_start = day_start
    buckets = []
    cursor = bucket_start
    switches_total = 0
    while cursor < bucket_end:
        nxt = cursor + 600
        count = sum(1 for r in rows if cursor <= r[0] < nxt and r[1] == "app_switch")
        switches_total += count
        state = _classify_intensity(count)
        buckets.append({"start_ts": cursor, "state": state, "count": count})
        cursor = nxt

    # Per-app minutes from heartbeats (each heartbeat ≈ one minute of presence).
    heartbeat_rows = [r for r in rows if r[1] == "heartbeat" and r[2]]
    app_counter: Counter[str] = Counter(r[2] for r in heartbeat_rows)
    # Also collect top window titles per app for context.
    app_titles: dict[str, Counter[str]] = defaultdict(Counter)
    for r in heartbeat_rows:
        t = (r[4] or "").strip()
        if t:
            app_titles[r[2]][t] += 1
    hours_by_app = [
        {"app": app, "minutes": mins,
         "top_titles": [t for t, _ in app_titles[app].most_common(3)]}
        for app, mins in app_counter.most_common(25)
    ]

    # Browser dwells: collapse consecutive rows on the same browser+page.
    dwells = _extract_browser_dwells(rows)
    # Trim and limit.
    dwells.sort(key=lambda d: d["duration_sec"], reverse=True)
    dwells = dwells[:ANALYZER_MAX_DWELLS]
    dwells.sort(key=lambda d: d["start_ts"])  # restore chronological order for the LLM

    # Calls & media windows.
    calls_and_media = _extract_calls_media(rows)

    # Break candidates: gaps > 5 min between consecutive rows or loginwindow stretches.
    breaks = _extract_breaks(rows, tz=tz)

    # AI-wait signals: Claude Code / Cursor spinner window_titles.
    ai_waits = _extract_ai_waits(heartbeat_rows, tz=tz)

    # Expose the real wake moment so the LLM doesn't fabricate "Early
    # morning" / "Mid-morning" acts over a sleep window. _find_wake_ts
    # already handles the "active past midnight, then slept late" case
    # (user's session from 00:05-00:22 followed by a 10h lock → wake at
    # the post-lock unlock time, not the pre-midnight tail).
    wake_ts = _find_wake_ts(rows)
    if wake_ts == float("inf"):
        wake_hhmm = ""
    else:
        wake_hhmm = datetime.fromtimestamp(wake_ts, tz=tz).strftime("%H:%M")

    now_dt = datetime.fromtimestamp(now_ts, tz=tz)
    return {
        "date": date_str,
        "timezone": str(tz),
        "weekday": now_dt.strftime("%A").lower(),
        "now_hhmm": now_dt.strftime("%H:%M"),
        "wake_hhmm": wake_hhmm,
        "total_rows": len(rows),
        "switches_total": switches_total,
        "intensity_buckets": buckets,
        "hours_by_app": hours_by_app,
        "browser_dwells": dwells,
        "calls_and_media": calls_and_media,
        "breaks": breaks,
        "ai_waits": ai_waits,
    }


# ── Extractors (pure helpers) ─────────────────────────────────────────────────

_BROWSER_APPS = {
    "Google Chrome", "Safari", "Arc", "Brave Browser", "Microsoft Edge",
    "Comet", "Firefox", "DuckDuckGo", "Opera", "Vivaldi", "Chromium",
}

_AI_CODING_APPS = {"Claude", "Claude Code", "Cursor", "iTerm2"}


def _classify_intensity(switches_in_10min: int) -> str:
    if switches_in_10min == 0:
        return "empty"
    if switches_in_10min < 10:
        return "calm"
    if switches_in_10min < 22:
        return "busy"
    return "chaotic"


def _extract_browser_dwells(rows: list) -> list[dict]:
    """Collapse runs of rows on the same browser+page into one dwell each."""
    dwells: list[dict] = []
    current: dict | None = None

    for ts, trigger, app_name, bundle_id, window_title, browser_url, page_text, *_ in rows:
        if app_name not in _BROWSER_APPS:
            if current:
                current["end_ts"] = int(ts)
                current["duration_sec"] = current["end_ts"] - current["start_ts"]
                if current["duration_sec"] >= 60:
                    dwells.append(current)
                current = None
            continue

        # Still on a browser. Decide if it's a new dwell or continuation.
        page_sig = (browser_url or window_title or "").strip()
        if current and current["_sig"] == page_sig and current["app"] == app_name:
            # same page — extend; refresh page_text if we got a better slice.
            if page_text and len(page_text) > len(current.get("_raw_text") or ""):
                current["_raw_text"] = page_text
            continue

        # close previous
        if current:
            current["end_ts"] = int(ts)
            current["duration_sec"] = current["end_ts"] - current["start_ts"]
            if current["duration_sec"] >= 60:
                dwells.append(current)

        current = {
            "app": app_name,
            "start_ts": int(ts),
            "window_title": window_title,
            "browser_url": browser_url or "",
            "_sig": page_sig,
            "_raw_text": page_text or "",
        }

    if current:
        current["end_ts"] = int(rows[-1][0])
        current["duration_sec"] = current["end_ts"] - current["start_ts"]
        if current["duration_sec"] >= 60:
            dwells.append(current)

    # Trim page_text: head 400 + tail 400 to keep the video title and footer.
    half = ANALYZER_PAGE_TEXT_CHARS // 2
    for d in dwells:
        raw = (d.pop("_raw_text", "") or "").replace("\n", " ").strip()
        if len(raw) <= ANALYZER_PAGE_TEXT_CHARS:
            d["page_text"] = raw
        else:
            d["page_text"] = raw[:half] + " … " + raw[-half:]
        d.pop("_sig", None)
        d["duration_min"] = d["duration_sec"] // 60
        # Derive the platform from the URL when we have one — this is ground
        # truth (the domain says what site it is), so the LLM doesn't have
        # to guess from body text. Empty string for Comet / any row whose
        # browser_url is null; the LLM falls back to page_text inference.
        d["platform"] = _extract_platform(d.get("browser_url"))
    return dwells


def _extract_calls_media(rows: list) -> list[dict]:
    """Windows where mic_active=1 (call) or audio_playing=1 (media)."""
    out: list[dict] = []
    state = None  # None, "call", "media"
    start_ts = 0
    app = ""
    for ts, trigger, app_name, _bundle, _wt, _url, _pt, audio, mic in rows:
        if mic:
            new_state = "call"
        elif audio:
            new_state = "media"
        else:
            new_state = None
        if new_state != state:
            if state:
                out.append({
                    "kind": state,
                    "app": app,
                    "start_ts": start_ts,
                    "end_ts": int(ts),
                    "duration_min": (int(ts) - start_ts) // 60,
                })
            state = new_state
            start_ts = int(ts)
            app = app_name or ""
    if state and rows:
        out.append({
            "kind": state,
            "app": app,
            "start_ts": start_ts,
            "end_ts": int(rows[-1][0]),
            "duration_min": (int(rows[-1][0]) - start_ts) // 60,
        })
    # Only return windows of at least 1 minute.
    return [w for w in out if w["duration_min"] >= 1]


_MAX_BREAK_MIN = 90  # longer gaps are sleep / away-all-afternoon, not a workday break


def _find_wake_ts(rows: list) -> float:
    """Timestamp of the user's real wake-up transition for the day.

    Specifically: the first non-loginwindow row that comes out of a
    loginwindow stretch of at least 30 minutes. That's the signature of
    a genuine sleep→wake transition — distinct from a short lock during
    a single working session. Treating "first non-loginwindow row"
    naively as wake breaks when the user was active just after midnight
    and then slept until late morning; the stretch-check rules that out.

    Falls back to the first non-loginwindow row if no long lock is seen
    (e.g. the user just started fresh this morning with no pre-midnight
    session). Returns infinity if the laptop stayed locked all day.
    """
    long_lock_secs = 30 * 60
    first_unlocked: float | None = None
    lock_start: float | None = None

    for ts, _trigger, app_name, *_rest in rows:
        if app_name == "loginwindow":
            if lock_start is None:
                lock_start = ts
            continue
        if first_unlocked is None:
            first_unlocked = ts
        if lock_start is not None and ts - lock_start >= long_lock_secs:
            return ts
        lock_start = None

    return first_unlocked if first_unlocked is not None else float("inf")


def _extract_breaks(rows: list, tz: ZoneInfo) -> list[dict]:
    if not rows:
        return []

    wake_ts = _find_wake_ts(rows)
    out: list[dict] = []

    def maybe_append(start_ts: float, end_ts: float, kind: str) -> None:
        if start_ts < wake_ts:
            return
        minutes = int((end_ts - start_ts) // 60)
        if minutes < 5 or minutes > _MAX_BREAK_MIN:
            return
        out.append({
            "start": datetime.fromtimestamp(start_ts, tz=tz).strftime("%H:%M"),
            "end":   datetime.fromtimestamp(end_ts,   tz=tz).strftime("%H:%M"),
            "minutes": minutes,
            "kind": kind,
        })

    gap_threshold = 5 * 60
    for i in range(1, len(rows)):
        dt = rows[i][0] - rows[i - 1][0]
        if dt >= gap_threshold:
            maybe_append(rows[i - 1][0], rows[i][0], "idle_gap")

    # loginwindow stretches
    run_start = None
    last = None
    for r in rows:
        if r[2] == "loginwindow":
            if run_start is None:
                run_start = r[0]
            last = r[0]
        else:
            if run_start is not None and last is not None and last - run_start >= 60:
                maybe_append(run_start, last, "screen_lock")
            run_start = None
    if run_start is not None and last is not None and last - run_start >= 60:
        maybe_append(run_start, last, "screen_lock")

    # A loginwindow stretch also registers as an idle_gap from the row-
    # gap scan above. Prefer the more informative screen_lock label when
    # both describe the same window.
    locks = {(b["start"], b["end"]) for b in out if b["kind"] == "screen_lock"}
    return [b for b in out if not (b["kind"] == "idle_gap" and (b["start"], b["end"]) in locks)]


def _extract_ai_waits(heartbeat_rows: list, tz: ZoneInfo) -> list[dict]:
    """Rough count of heartbeats where the focused window looked like a spinner."""
    spinner_chars = ("✳", "⠂", "⠐", "⠈", "⠉", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
    sessions = 0
    minutes = 0
    samples: list[str] = []
    prev_was_spinner = False
    for r in heartbeat_rows:
        app = r[2] or ""
        title = (r[4] or "")
        is_ai = app in _AI_CODING_APPS and any(ch in title for ch in spinner_chars)
        if is_ai:
            minutes += 1
            if not prev_was_spinner:
                sessions += 1
                if len(samples) < 5 and title.strip():
                    samples.append(title.strip())
            prev_was_spinner = True
        else:
            prev_was_spinner = False
    return [{
        "minutes": minutes,
        "sessions": sessions,
        "example_titles": samples,
    }]


# ── Config + Keychain ─────────────────────────────────────────────────────────

def _load_config(conn: sqlite3.Connection) -> dict | None:
    rows = conn.execute("SELECT key, value FROM app_config").fetchall()
    cfg = {k: v for k, v in rows}
    required = ("ai_provider", "ai_model", "ai_base_url")
    if not all(k in cfg for k in required):
        return None
    return cfg


def _load_api_key(account_hint: str) -> str | None:
    """Read the API key out of Keychain via `security`.

    `account_hint` is either the provider name ('anthropic', 'openai', 'gemini')
    or a colon-form ref 'com.brainloop.ai:anthropic'. We only use the tail
    (account), the service stays ANALYZER_KEYCHAIN_SERVICE.
    """
    account = account_hint.split(":")[-1].strip() if account_hint else ""
    try:
        cmd = ["security", "find-generic-password", "-s", ANALYZER_KEYCHAIN_SERVICE, "-w"]
        if account:
            cmd.extend(["-a", account])
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if out.returncode != 0:
            return None
        key = out.stdout.strip()
        return key or None
    except Exception:
        return None


# ── Timezone helper ───────────────────────────────────────────────────────────

def _local_tz() -> ZoneInfo:
    """Best-effort system timezone. Falls back to UTC."""
    try:
        # Python picks up /etc/localtime automatically on macOS.
        return datetime.now().astimezone().tzinfo or ZoneInfo("UTC")  # type: ignore[return-value]
    except Exception:
        return ZoneInfo("UTC")


# ── CLI: `python -m daemon.analyze --once` ────────────────────────────────────

def _main() -> int:
    p = argparse.ArgumentParser(description="Brainloop day-summary analyzer")
    p.add_argument("--once", action="store_true", help="run exactly one tick and exit")
    p.add_argument("--force", action="store_true", help="bypass min-regen + new-row gates (dev)")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )
    if not args.once:
        p.print_help()
        return 2

    conn = sqlite3.connect(str(DB_PATH))
    try:
        tick(conn, force=args.force)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
