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


# ── Payload sanitizer ─────────────────────────────────────────────────────────

_BROWSER_SOURCES = {
    "comet", "chrome", "google chrome", "safari", "arc", "brave", "brave browser",
    "firefox", "edge", "microsoft edge", "opera", "vivaldi", "chromium",
    "duckduckgo",
}

# Platform fingerprints for the sanitizer's fallback labeling: when the LLM
# emits source=<browser>, try to infer the actual platform by scanning the
# title + body for these keywords. First match wins, so order matters — put
# more specific fingerprints before generic ones.
_PLATFORM_FINGERPRINTS = (
    # Crunchyroll first — its "simulcast" keyword was being swallowed by a
    # YouTube rule that also listed simulcast, causing every Crunchyroll
    # anime page to get mislabeled as YouTube.
    ("Crunchyroll", ("crunchyroll", "simulcast", "funimation")),
    ("YouTube",     ("youtube", "shorts subscriptions", "subscriptions library", "watch later")),
    ("Netflix",     ("netflix",)),
    ("Twitch",      ("twitch", "streamer")),
    ("X / Twitter", ("twitter", "tweet", " x ")),
    ("Reddit",      ("reddit", "r/", "u/")),
    ("Facebook",    ("facebook", "news feed")),
    ("Instagram",   ("instagram", "reels")),
    ("LinkedIn",    ("linkedin",)),
    ("Hacker News", ("hacker news", "news.ycombinator")),
    ("GitHub",      ("github.com", "pull request", "commit ")),
    ("Medium",      ("medium.com", "min read")),
    ("Wikipedia",   ("wikipedia", "encyclopedia")),
)


def _infer_platform(title: str, haystack: str = "") -> str | None:
    """Best-effort platform label from the entry's title + any haystack text.

    Returns None if nothing matches — caller decides what to do with
    browser-labeled entries in that case.
    """
    blob = f"{title} {haystack}".lower()
    for platform, needles in _PLATFORM_FINGERPRINTS:
        if any(n in blob for n in needles):
            return platform
    return None


def _sanitize_payload(payload: dict) -> None:
    """Trim widgets the model got wrong. Mutates in place.

    For `things_read`: if `source` looks like a browser app name ("Comet",
    "Chrome", ...), try to relabel it with an inferred platform from the
    entry's title. Only drop an entry if inference also fails — better to
    show "Yennai Arindhal scenes · YouTube" than nothing at all when the
    model was timid about naming the platform.
    """
    try:
        items = payload.get("widgets", {}).get("things_read") or []
    except AttributeError:
        return
    cleaned: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        src = (it.get("source") or "").strip()
        if src.lower() in _BROWSER_SOURCES:
            inferred = _infer_platform(it.get("title") or "")
            if inferred:
                it["source"] = inferred
            else:
                # No platform signal at all — keep it with a neutral "Web"
                # label rather than dropping; the user can still see they
                # spent time reading something.
                it["source"] = "Web"
        cleaned.append(it)
    payload["widgets"]["things_read"] = cleaned


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
