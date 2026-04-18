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

def tick(conn: sqlite3.Connection, now_ts: float | None = None) -> None:
    """One scheduled pass. Silent on missing config, idle days, or network errors."""
    now_ts = now_ts or time.time()
    local_tz = _local_tz()
    today = datetime.fromtimestamp(now_ts, tz=local_tz).strftime("%Y-%m-%d")

    # Gate 1: config must be present.
    cfg = _load_config(conn)
    if not cfg:
        log.debug("analyzer skip: ai config not set in app_config")
        return
    api_key = _load_api_key(cfg.get("ai_key_ref") or cfg.get("ai_provider") or "")
    if not api_key:
        log.debug("analyzer skip: api key not in Keychain (service=%s)", ANALYZER_KEYCHAIN_SERVICE)
        return

    # Gate 2: don't regenerate too often.
    prev = conn.execute(
        "SELECT generated_at FROM day_summary WHERE date = ?", (today,)
    ).fetchone()
    if prev and (now_ts - prev[0]) < ANALYZER_MIN_REGEN_SECS:
        log.debug("analyzer skip: regenerated %ds ago", int(now_ts - prev[0]))
        return

    # Gate 3: last activity row mustn't be stale (user away/asleep).
    last_row = conn.execute("SELECT MAX(ts) FROM activity_log").fetchone()
    last_ts = last_row[0] if last_row and last_row[0] else 0
    if (now_ts - last_ts) > ANALYZER_IDLE_SKIP_MINS * 60:
        log.debug("analyzer skip: no activity for %d min", int((now_ts - last_ts) / 60))
        return

    # Gate 4: require at least one new row since the previous run.
    if prev:
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

    now_dt = datetime.fromtimestamp(now_ts, tz=tz)
    return {
        "date": date_str,
        "timezone": str(tz),
        "weekday": now_dt.strftime("%A").lower(),
        "now_hhmm": now_dt.strftime("%H:%M"),
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


def _extract_breaks(rows: list, tz: ZoneInfo) -> list[dict]:
    out: list[dict] = []
    gap_threshold = 5 * 60
    for i in range(1, len(rows)):
        dt = rows[i][0] - rows[i - 1][0]
        if dt >= gap_threshold:
            out.append({
                "start": datetime.fromtimestamp(rows[i - 1][0], tz=tz).strftime("%H:%M"),
                "end":   datetime.fromtimestamp(rows[i][0],     tz=tz).strftime("%H:%M"),
                "minutes": int(dt // 60),
                "kind": "idle_gap",
            })
    # loginwindow stretches
    run_start = None
    for r in rows:
        if r[2] == "loginwindow":
            if run_start is None:
                run_start = r[0]
            last = r[0]
        else:
            if run_start is not None and last - run_start >= 60:
                out.append({
                    "start": datetime.fromtimestamp(run_start, tz=tz).strftime("%H:%M"),
                    "end":   datetime.fromtimestamp(last, tz=tz).strftime("%H:%M"),
                    "minutes": int((last - run_start) // 60),
                    "kind": "screen_lock",
                })
            run_start = None
    return out


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
        tick(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main())
