"""
brainloop.db
~~~~~~~~~~~~
SQLite setup and snapshot writes.

All writes go through write_snapshot(); callers only pass a trigger string.
The function collects the current macOS state internally so every module
that needs to write a record has a single call-site.
"""

import sqlite3
import time
import traceback
import logging
from datetime import datetime

from .config import DB_DIR, DB_PATH, DEBOUNCE_SECS

log = logging.getLogger("brainloop.db")

# ── Module-level state ────────────────────────────────────────────────────────
_db:            sqlite3.Connection | None = None
_total_records: int   = 0
_last_write_ts: float = 0.0   # for debounce


def open_db() -> sqlite3.Connection:
    """Create DB directory, open connection, apply schema, return connection."""
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS activity_log (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            ts               REAL    NOT NULL,
            ts_iso           TEXT    NOT NULL,
            trigger          TEXT,
            app_name         TEXT,
            bundle_id        TEXT,
            pid              INTEGER,
            window_title     TEXT,
            browser_url      TEXT,
            ax_element_text  TEXT,
            ax_role          TEXT,
            visible_text     TEXT,
            page_text        TEXT,
            audio_playing    INTEGER DEFAULT 0,
            mic_active       INTEGER DEFAULT 0,
            audio_device     TEXT,
            mic_device       TEXT,
            is_speaking      INTEGER DEFAULT 0,
            mic_app          TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts       ON activity_log(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_app_name ON activity_log(app_name)")

    # day_summary: one row per local-date, overwritten in place as the day progresses.
    # payload_json holds the full structured summary consumed by the UI.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS day_summary (
            date          TEXT PRIMARY KEY,
            generated_at  INTEGER NOT NULL,
            model         TEXT    NOT NULL,
            activity_rows INTEGER NOT NULL,
            payload_json  TEXT    NOT NULL,
            tokens_in     INTEGER,
            tokens_out    INTEGER
        )
    """)

    # app_config: key/value settings written by the UI, read by the daemon.
    # Holds ai_provider, ai_model, ai_base_url, ai_key_ref (Keychain lookup).
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_config (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # chat_messages: the Chat tab's conversation log. User turns arrive from
    # the UI with status='pending'; the chat-poll timer in daemon.py picks
    # them up, runs one LLM round with the run_sql tool, writes an assistant
    # reply row, then flips the user row to status='done'. Conversation is
    # persistent across sessions — the UI re-renders it on mount.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      INTEGER NOT NULL,
            role            TEXT    NOT NULL,
            content         TEXT    NOT NULL,
            tool_calls_json TEXT,
            status          TEXT    NOT NULL DEFAULT 'done',
            model           TEXT,
            tokens_in       INTEGER,
            tokens_out      INTEGER,
            error           TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_chat_status ON chat_messages(status, id)"
    )

    # Migrations: add columns added after initial schema
    for col_def in (
        "page_text TEXT",
        "audio_playing INTEGER DEFAULT 0",
        "mic_active INTEGER DEFAULT 0",
        "audio_device TEXT",
        "mic_device TEXT",
        "is_speaking INTEGER DEFAULT 0",
        "mic_app TEXT",
    ):
        try:
            conn.execute(f"ALTER TABLE activity_log ADD COLUMN {col_def}")
        except Exception:
            pass  # column already exists
    conn.commit()
    log.info("DB opened: %s", DB_PATH)
    return conn


def init(conn: sqlite3.Connection) -> None:
    """Store the connection for use by write_snapshot()."""
    global _db
    _db = conn


def write_snapshot(trigger: str, debounce: bool = False) -> None:
    """
    Capture current macOS state and write one row to activity_log.

    Parameters
    ----------
    trigger:  label for what caused this write (e.g. 'app_switch', 'heartbeat')
    debounce: if True, skip the write if we wrote within DEBOUNCE_SECS
    """
    global _last_write_ts, _total_records

    if _db is None:
        log.warning("write_snapshot called before init()")
        return

    now = time.time()
    if debounce and (now - _last_write_ts) < DEBOUNCE_SECS:
        return
    _last_write_ts = now

    try:
        # Import here to avoid circular imports; ax.py also imports config.
        from .capture.ax import (
            get_active_app,
            get_window_title,
            get_browser_url,
            get_focused_element,
            get_visible_text,
            get_page_text,
        )
        from .capture.audio import audio_playing, mic_active, output_device_name, input_device_name

        app_name, bundle_id, pid = get_active_app()
        window_title             = get_window_title(pid)
        browser_url              = get_browser_url(pid, bundle_id) if pid and bundle_id else None
        ax_text, ax_role         = get_focused_element(pid)
        visible_text             = get_visible_text(pid)
        page_text                = get_page_text(bundle_id)
        is_audio                 = 1 if audio_playing() else 0
        is_mic                   = 1 if mic_active() else 0
        out_device               = output_device_name() if is_audio else None
        in_device                = input_device_name() if is_mic else None

        _db.execute("""
            INSERT INTO activity_log
              (ts, ts_iso, trigger, app_name, bundle_id, pid,
               window_title, browser_url, ax_element_text, ax_role,
               visible_text, page_text, audio_playing, mic_active,
               audio_device, mic_device)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now,
            datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S"),
            trigger,
            app_name, bundle_id, pid,
            window_title, browser_url,
            ax_text, ax_role,
            visible_text,
            page_text,
            is_audio,
            is_mic,
            out_device,
            in_device,
        ))
        _db.commit()
        _total_records += 1
        log.debug("[%s] %s | %s | audio=%d mic=%d", trigger, app_name, window_title or "—", is_audio, is_mic)

    except Exception:
        log.error("write_snapshot error:\n%s", traceback.format_exc())


def total_records() -> int:
    return _total_records
