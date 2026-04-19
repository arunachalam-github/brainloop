"""
brainloop.chat
~~~~~~~~~~~~~~
SQL-tool-use powered chat over the activity_log.

The Chat tab in the UI writes `user` rows into `chat_messages` with
`status='pending'`. This module polls those rows (via a CFRunLoopTimer
registered in daemon.py), runs a tool-use loop against the configured LLM,
lets the model query activity.db through a sandboxed `run_sql` tool, and
writes the final text as an `assistant` row.

Safety rails on `run_sql`:
  1. Only SELECT / WITH statements are accepted (prefix allowlist).
  2. The connection used for execution is opened with `file:...?mode=ro`
     and `PRAGMA query_only = ON` — two independent layers.
  3. Single-statement only (';' anywhere rejects the query).
  4. Hard row cap + execution timeout so a runaway query can't pin the
     daemon's CFRunLoop.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from typing import Any

from . import providers
from .analyze import _load_api_key, _load_config
from .config import (
    CHAT_MAX_HISTORY_TURNS,
    CHAT_MAX_ROWS_PER_SQL,
    CHAT_MAX_TOOL_CALLS,
    CHAT_SQL_TIMEOUT_SECS,
    DB_PATH,
)
from .prompts import CHAT_SYSTEM_PROMPT, RUN_SQL_TOOL_SPEC

log = logging.getLogger("brainloop.chat")


# ── run_sql — the only tool the chat model has ────────────────────────────────

_ALLOWED_PREFIXES = ("select", "with")


def run_sql(query: str) -> dict:
    """Execute a single read-only SELECT/WITH against activity.db.

    Returns a compact dict the model can read on the tool_result channel:
      {"columns": [..], "rows": [[..], ..], "row_count": N, "truncated": bool}
    On rejection or failure:
      {"error": "..."}
    """
    if not isinstance(query, str):
        return {"error": "query must be a string"}
    q = query.strip().rstrip(";").lstrip()
    if not q:
        return {"error": "empty query"}
    if ";" in q:
        return {"error": "only a single statement is allowed (no ';')"}
    if not q.lower().lstrip("(").startswith(_ALLOWED_PREFIXES):
        return {"error": "only SELECT / WITH queries are allowed"}

    conn = None
    try:
        conn = sqlite3.connect(
            f"file:{DB_PATH}?mode=ro",
            uri=True,
            timeout=CHAT_SQL_TIMEOUT_SECS,
            isolation_level=None,
        )
        conn.execute("PRAGMA query_only = ON")
        # Keep result sizes bounded. We ask for N+1 so we can tell if we truncated.
        cur = conn.execute(q)
        cols = [d[0] for d in (cur.description or [])]
        rows = cur.fetchmany(CHAT_MAX_ROWS_PER_SQL + 1)
        truncated = len(rows) > CHAT_MAX_ROWS_PER_SQL
        rows = rows[:CHAT_MAX_ROWS_PER_SQL]
        # Convert values to JSON-friendly primitives (ints, floats, strings, None).
        clean_rows = [[_json_safe(v) for v in r] for r in rows]
        return {
            "columns": cols,
            "rows": clean_rows,
            "row_count": len(clean_rows),
            "truncated": truncated,
        }
    except Exception as e:
        return {"error": str(e)[:400]}
    finally:
        if conn is not None:
            try: conn.close()
            except Exception: pass


def _json_safe(v: Any) -> Any:
    if v is None or isinstance(v, (int, float, str, bool)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return f"<{len(v)}b>"
    return str(v)


# ── Orchestration: called every CHAT_POLL_SECS by the daemon ──────────────────

def check_pending(conn: sqlite3.Connection, now_ts: float | None = None) -> None:
    """Pick up the oldest pending user row, run one LLM turn, write the reply."""
    now_ts = now_ts or time.time()
    try:
        row = conn.execute(
            "SELECT id, content FROM chat_messages "
            "WHERE status='pending' AND role='user' ORDER BY id ASC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        # Table doesn't exist yet (fresh DB, pre-migration) — nothing to do.
        return
    if not row:
        return
    user_id, question = row[0], row[1]
    # Atomic flip to 'processing' — prevents a second tick from double-serving
    # the same row if the LLM call runs long.
    updated = conn.execute(
        "UPDATE chat_messages SET status='processing' WHERE id=? AND status='pending'",
        (user_id,),
    ).rowcount
    conn.commit()
    if not updated:
        return

    log.info("chat turn start id=%d", user_id)

    cfg = _load_config(conn)
    if not cfg:
        _write_error(conn, user_id, "Add an AI key in Settings to chat.")
        return
    api_key = cfg.get("ai_api_key") or _load_api_key(
        cfg.get("ai_key_ref") or cfg.get("ai_provider") or ""
    )
    if not api_key:
        _write_error(conn, user_id, "Add an AI key in Settings to chat.")
        return

    # Build message list: prior done-turns (capped) + the new user question.
    history = _load_history(conn, exclude_id=user_id)
    messages: list[dict] = []
    for h in history:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": question})

    # Tool-use loop.
    tokens_in_total = 0
    tokens_out_total = 0
    tool_calls_log: list[dict] = []
    final_text: str | None = None
    started = time.time()

    for step in range(CHAT_MAX_TOOL_CALLS):
        try:
            resp, tin, tout = providers.call_chat(
                provider=cfg["ai_provider"],
                base_url=cfg["ai_base_url"],
                api_key=api_key,
                model=cfg["ai_model"],
                system=CHAT_SYSTEM_PROMPT,
                messages=messages,
                tools=[RUN_SQL_TOOL_SPEC],
            )
        except providers.LLMError as e:
            log.warning("chat turn id=%d failed: %s", user_id, e)
            _write_error(conn, user_id, f"LLM error: {str(e)[:200]}")
            return
        tokens_in_total += tin
        tokens_out_total += tout

        if resp["type"] == "text":
            final_text = resp["content"].strip()
            break

        # tool_calls: run each run_sql, append assistant+tool rows to messages.
        assistant_entry = _assistant_tool_use_entry(resp["calls"], resp.get("raw_assistant"))
        messages.append(assistant_entry)
        for call in resp["calls"]:
            if call["name"] != "run_sql":
                tool_output = {"error": f"unknown tool: {call['name']}"}
            else:
                q = (call["arguments"] or {}).get("query") or ""
                tool_output = run_sql(q)
                tool_calls_log.append({
                    "step": step + 1,
                    "query": q,
                    "row_count": tool_output.get("row_count"),
                    "error": tool_output.get("error"),
                })
                log.info(
                    "run_sql %d/%d id=%d rows=%s err=%s",
                    step + 1, CHAT_MAX_TOOL_CALLS, user_id,
                    tool_output.get("row_count"), tool_output.get("error"),
                )
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(tool_output)[:6000],
            })
    else:
        # Ran out of tool budget. Force one more round with no tools so the
        # model must answer from what it's already seen.
        try:
            resp, tin, tout = providers.call_chat(
                provider=cfg["ai_provider"],
                base_url=cfg["ai_base_url"],
                api_key=api_key,
                model=cfg["ai_model"],
                system=CHAT_SYSTEM_PROMPT,
                messages=messages + [{
                    "role": "user",
                    "content": "Answer with what you have — no more tool calls.",
                }],
                tools=[],
            )
            tokens_in_total += tin
            tokens_out_total += tout
            if resp["type"] == "text":
                final_text = resp["content"].strip()
            else:
                final_text = "I ran out of queries before I could answer that."
        except providers.LLMError as e:
            _write_error(conn, user_id, f"LLM error: {str(e)[:200]}")
            return

    if not final_text:
        final_text = "I couldn't find an answer in your activity log."

    duration = time.time() - started
    conn.execute(
        "INSERT INTO chat_messages "
        "(created_at, role, content, tool_calls_json, status, model, tokens_in, tokens_out) "
        "VALUES (?, 'assistant', ?, ?, 'done', ?, ?, ?)",
        (int(time.time()), final_text, json.dumps(tool_calls_log) if tool_calls_log else None,
         cfg["ai_model"], tokens_in_total, tokens_out_total),
    )
    conn.execute("UPDATE chat_messages SET status='done' WHERE id=?", (user_id,))
    conn.commit()
    log.info(
        "chat turn OK id=%d tokens=%d→%d duration=%.1fs tools=%d",
        user_id, tokens_in_total, tokens_out_total, duration, len(tool_calls_log),
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_history(conn: sqlite3.Connection, exclude_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM chat_messages "
        "WHERE status='done' AND role IN ('user','assistant') AND id != ? "
        "ORDER BY id ASC",
        (exclude_id,),
    ).fetchall()
    # Keep only the most recent CHAT_MAX_HISTORY_TURNS pairs.
    # We approximate a "pair" as 2 rows; cap rows to 2 * N.
    max_rows = 2 * CHAT_MAX_HISTORY_TURNS
    rows = rows[-max_rows:]
    return [{"role": r[0], "content": r[1]} for r in rows]


def _assistant_tool_use_entry(calls: list[dict], raw_assistant: Any) -> dict:
    """Build the OpenAI-flavored assistant message that carries tool_calls.

    Anthropic's provider adapter will translate this via _to_anthropic_messages
    so the caller can stay provider-agnostic.
    """
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": c["id"],
                "type": "function",
                "function": {
                    "name": c["name"],
                    "arguments": json.dumps(c["arguments"] or {}),
                },
            }
            for c in calls
        ],
    }


def _write_error(conn: sqlite3.Connection, user_id: int, message: str) -> None:
    conn.execute(
        "INSERT INTO chat_messages (created_at, role, content, status, error) "
        "VALUES (?, 'error', ?, 'done', ?)",
        (int(time.time()), message, message),
    )
    conn.execute("UPDATE chat_messages SET status='error' WHERE id=?", (user_id,))
    conn.commit()
    log.info("chat turn id=%d written as error: %s", user_id, message)
