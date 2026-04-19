"""
brainloop.prompts
~~~~~~~~~~~~~~~~~
Prompt + JSON-schema constants for the day-summary analyzer.

The schema mirrors the structure of the final Today UI (headline, "three acts",
widgets, timeline) so the UI can render it verbatim with no client-side
post-processing. The LLM is responsible for:

- writing the editorial headline and per-act narratives,
- clustering browser dwells into named themes ("Sarpatta BGMs", "Master"),
- flagging entertainment consumption as "the monkey" when it dominates an act,
- computing the derived widgets (longest_focus, doom_scroll, etc.) from the
  aggregated context it was given. We do *not* compute these in Python — the
  LLM has enough signal and its labels are better than a heuristic's.
"""

SYSTEM_PROMPT = """\
You are Brainloop's editorial observer. The user records their own computer
activity into a local SQLite database; you're given an aggregate for one day
and your job is to write a calm, honest, slightly literary daily summary.

Voice:
- Journal tone. Short sentences. Reference specific moments.
- Be gentle about entertainment. Do NOT moralize, lecture, or congratulate.
- Name what the user was actually watching/reading by clustering page_text
  slices into themes ("Sarpatta BGMs", "Master", "Tamil cinema", "Facebook
  feed"). Never list raw URL-style strings. If several page_text samples
  share a subject, collapse them into a single named theme.
- Avoid metrics-speak ("productivity score", "efficiency"). Prefer "focus",
  "wandered", "drifted", "settled".
- Present tense for the "Now" act, past tense for earlier acts.

The Monkey (important — always follow):
- "The monkey" is our running nickname for when YouTube / social / video /
  news-feed consumption ate an act. If that happened at any point in the
  day, the noun "monkey" MUST appear somewhere in the headline, lowercase,
  exactly that spelling. It will be styled italic + terracotta in the UI.
  Examples: "The monkey arrived before the work did." · "A morning of
  anime; the monkey took the early hours." · "Sarpatta, Master, and the
  monkey — then brainloop." Only omit "monkey" when the day had zero
  entertainment consumption; in that rare case the headline stays plain.
- AT MOST ONE "GRATIFICATION MONKEY" callout per act. If an act contains
  multiple distraction blocks (e.g. two separate YouTube sessions within
  the same Early morning), you MUST merge them into a single callout —
  never emit two monkey callouts in the same act. In the merged callout:
    - `label` is exactly "GRATIFICATION MONKEY".
    - `time` is the HH:MM of the FIRST distraction block in the act.
    - `duration_min` is the SUM of all distraction minutes in the act.
    - `body` is ONE flowing sentence listing the specific videos / clips /
      posts by title across all blocks, grouped by platform. Example:
      "Watched Sathyadev Furious Action - Yennai Arindhal, Theeran Movie
      Scenes - Karthi, and Vaikuntapuram - Back to Back on YouTube."
  If an act had no entertainment at all, its callouts array should be
  empty; do not invent a monkey.

Structure:
- headline: one or two sentences, editorial, specific to today's events.
  If any entertainment happened, include the word "monkey" (see above).
- subtitle: lowercase "{weekday}, {month} {day} · {N} switches · {H}h{M} focus"
- acts: 3-5 entries covering the user's ACTIVE portion of the day —
  i.e. from the WOKE UP time shown in the context through "now". NEVER
  produce an act with a time_range that starts before the WOKE UP
  time, even if "Early morning" or "Mid-morning" would otherwise be
  appropriate — those hours were sleep. Pick titles from {Past
  midnight, Early morning, Mid-morning, Late morning, Midday,
  Afternoon, Late afternoon, Evening, Night, Now} that match the
  actual clock time of the block. Use "Past midnight" (NOT "Early
  morning") for any block whose start_time is before 04:00 — "Early
  morning" only applies to 04:00-08:00. Each act should span roughly 1-3 hours; NEVER
  compress more than 3 hours of active work into a single act unless
  the user did one continuous thing the whole time (e.g. a 4-hour
  coding session with no context switches). If the active window is
  shorter than 2 hours, fewer acts are okay (minimum 1). The final
  act should always be titled "Now" and end at the current time.
  Each act has title, time_range, one_liner, narrative (2-3 sentences),
  and 0-2 callouts. Callouts highlight specific blocks —
  GRATIFICATION MONKEY for entertainment, "FOCUS" or "CALL" for other
  notable blocks. Gratification monkey callouts are REQUIRED for any
  act with >= 5 minutes of entertainment consumption.
- widgets: fill every field from the context; if the day has no calls,
  on_calls.count is 0 (not missing).
- intensity_buckets: pass through the provided buckets verbatim.
- widgets.things_read: a short, clean list (max ~8 entries). Each entry's
  `title` MUST be the actual content title — like a single video name,
  article headline, or named feed — NEVER a raw page_text excerpt and
  NEVER a description. Extract titles from the page_text slices (they
  usually appear near the top). Good titles: "Yennai Arindhal scenes",
  "Sarpatta Parambarai scenes", "Rahul Gandhi - Lok Sabha", "Facebook
  feed", "Twitter home". Bad titles: "Create Home Shorts Subscriptions
  …", "Workflow Credential Project Enterprise Overview …", or any
  sidebar/nav text.
  `source` is ALWAYS the content platform — NEVER a browser or app
  name ("Comet", "Chrome", "Safari"). Prefer the `platform` field on
  the corresponding browser_dwell entry verbatim when it is non-empty
  — that's derived from the URL and is ground truth. Only fall back to
  inferring from page_text (YouTube has "views · # likes", Reddit has
  "r/<sub>", Twitter has "@handle", etc.) when `platform` is empty
  (typically Comet dwells, which have no browser_url). If after both
  checks you still can't identify a platform with confidence, omit the
  entry rather than labeling it with the browser name.

Output strictly valid JSON matching the schema. No prose outside the JSON.
"""


# JSON Schema (draft 2020-12) used by providers that support structured output.
# Passed verbatim to OpenAI's `response_format: json_schema`; the Anthropic
# adapter wraps it in a tool_use shape. All fields are required so the UI
# never has to deal with "missing" states — if the LLM has no data for a
# widget, it sets a sentinel (e.g. minutes: 0, detail: "").
PAYLOAD_SCHEMA = {
    "name": "day_summary",
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "headline",
            "subtitle",
            "switches_total",
            "activity_state",
            "intensity_buckets",
            "acts",
            "widgets",
        ],
        "properties": {
            # Length caps on every string: Gemini 2.5 has been observed to
            # fall into "\n\n\n…" repetition loops inside JSON strings,
            # burning the entire output budget on whitespace. maxLength
            # forces the decoder to close the string before that happens.
            "headline": {"type": "string", "maxLength": 280},
            "subtitle": {"type": "string", "maxLength": 120},
            "switches_total": {"type": "integer"},
            "activity_state": {
                "type": "string",
                "enum": ["empty", "calm", "busy", "chaotic"],
            },
            "intensity_buckets": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["start_ts", "state", "count"],
                    "properties": {
                        "start_ts": {"type": "integer"},
                        "state": {
                            "type": "string",
                            "enum": ["empty", "calm", "busy", "chaotic"],
                        },
                        "count": {"type": "integer"},
                    },
                },
            },
            "acts": {
                "type": "array",
                # 1 act is fine when the user just woke up / day just started.
                # A stricter minimum causes the model to duplicate its only
                # act to satisfy the schema, which users see as "2 Now acts".
                "minItems": 1,
                "maxItems": 5,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "title",
                        "time_range",
                        "one_liner",
                        "narrative",
                        "callouts",
                    ],
                    "properties": {
                        "title": {"type": "string", "maxLength": 40},
                        "time_range": {"type": "string", "maxLength": 30},
                        "one_liner": {"type": "string", "maxLength": 200},
                        "narrative": {"type": "string", "maxLength": 400},
                        "callouts": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": [
                                    "label",
                                    "time",
                                    "duration_min",
                                    "body",
                                ],
                                "properties": {
                                    "label": {"type": "string", "maxLength": 40},
                                    "time": {"type": "string", "maxLength": 10},
                                    "duration_min": {"type": "integer"},
                                    "body": {"type": "string", "maxLength": 600},
                                },
                            },
                        },
                    },
                },
            },
            "widgets": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "longest_focus",
                    "doom_scroll",
                    "hours_by_app",
                    "on_calls",
                    "waiting_on_ai",
                    "breaks",
                    "things_read",
                ],
                "properties": {
                    "longest_focus": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["minutes", "label", "range"],
                        "properties": {
                            "minutes": {"type": "integer"},
                            "label": {"type": "string", "maxLength": 80},
                            "range": {"type": "string", "maxLength": 30},
                        },
                    },
                    "doom_scroll": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["minutes", "detail", "worst_range"],
                        "properties": {
                            "minutes": {"type": "integer"},
                            "detail": {"type": "string", "maxLength": 300},
                            "worst_range": {"type": "string", "maxLength": 30},
                        },
                    },
                    "hours_by_app": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["app", "minutes"],
                            "properties": {
                                "app": {"type": "string", "maxLength": 60},
                                "minutes": {"type": "integer"},
                            },
                        },
                    },
                    "on_calls": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["count", "minutes"],
                        "properties": {
                            "count": {"type": "integer"},
                            "minutes": {"type": "integer"},
                        },
                    },
                    "waiting_on_ai": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["minutes", "detail", "sessions"],
                        "properties": {
                            "minutes": {"type": "integer"},
                            "detail": {"type": "string", "maxLength": 200},
                            "sessions": {"type": "integer"},
                        },
                    },
                    "breaks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["start", "end", "minutes"],
                            "properties": {
                                "start": {"type": "string", "maxLength": 10},
                                "end":   {"type": "string", "maxLength": 10},
                                "minutes": {"type": "integer"},
                            },
                        },
                    },
                    "things_read": {
                        "type": "array",
                        "maxItems": 8,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["time", "title", "source"],
                            "properties": {
                                "time":   {"type": "string", "maxLength": 10},
                                # 60 chars forces the model to emit an actual
                                # title instead of padding out a page_text
                                # excerpt. Real video / article titles fit.
                                "title":  {"type": "string", "maxLength": 60},
                                "source": {"type": "string", "maxLength": 24},
                            },
                        },
                    },
                },
            },
        },
    },
}


def user_message(context: dict) -> str:
    """Format the aggregated context dict into the user-facing prompt body.

    The structure is deliberately plain — labeled blocks that an LLM can skim.
    Keeping it readable means debugging bad summaries is just reading the prompt.
    """
    import json
    return (
        "Here is today's aggregated activity. Read it carefully and produce "
        "the day_summary JSON object.\n\n"
        f"DATE: {context['date']} ({context['weekday']})\n"
        f"TIMEZONE: {context['timezone']}\n"
        f"NOW: {context['now_hhmm']} local time\n"
        f"WOKE UP: {context.get('wake_hhmm') or 'unknown'} — do NOT narrate "
        "any act before this time. Everything earlier is sleep.\n"
        f"TOTAL SWITCHES: {context['switches_total']}\n\n"
        "INTENSITY BUCKETS (10-min buckets, state = empty/calm/busy/chaotic):\n"
        f"{json.dumps(context['intensity_buckets'], indent=2)}\n\n"
        "PER-APP MINUTES (from heartbeat rows, higher = longer used):\n"
        f"{json.dumps(context['hours_by_app'], indent=2)}\n\n"
        "BROWSER DWELLS (each is a window of time on a browser page; page_text is "
        "a slice of what was visible on screen — the LLM should cluster these into "
        "named themes):\n"
        f"{json.dumps(context['browser_dwells'], indent=2)}\n\n"
        "CALL / MEDIA WINDOWS (mic_active=call candidate; audio_playing alone=media):\n"
        f"{json.dumps(context['calls_and_media'], indent=2)}\n\n"
        "BREAK CANDIDATES (heartbeat gaps or loginwindow runs):\n"
        f"{json.dumps(context['breaks'], indent=2)}\n\n"
        "AI-WAIT SIGNALS (Claude Code / Cursor spinner titles + dwell):\n"
        f"{json.dumps(context['ai_waits'], indent=2)}\n"
    )


# ── Chat (run_sql-powered Q&A) ────────────────────────────────────────────────
# Chat reuses CLAUDE.md as its schema + examples reference so any updates to
# that file (new columns, new example queries) flow straight into the chat
# model's behavior. In dev we read it from the repo root; in a bundled binary
# PyInstaller stages it at sys._MEIPASS/CLAUDE.md (see build/brainloopd.spec).

import os
import sys
from pathlib import Path


def _load_claude_md() -> str:
    """Return the content of CLAUDE.md, or a minimal fallback schema note."""
    candidates: list[Path] = []
    # PyInstaller bundle: CLAUDE.md lands alongside the executable's data files.
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "CLAUDE.md")
    # Dev / source run: two levels up from daemon/prompts.py.
    candidates.append(Path(__file__).resolve().parent.parent / "CLAUDE.md")
    for c in candidates:
        try:
            return c.read_text(encoding="utf-8")
        except Exception:
            continue
    return (
        "CLAUDE.md not found. Minimal schema: activity_log with columns "
        "ts (unix float), ts_iso, trigger, app_name, bundle_id, window_title, "
        "browser_url, page_text, visible_text, audio_playing (0/1), mic_active "
        "(0/1). Use ts >= strftime('%s', date('now','localtime')) for today."
    )


CHAT_SCHEMA_DOC = _load_claude_md()

CHAT_SYSTEM_PROMPT = (
    "You are Brainloop's chat — an observer who can look at the user's Mac "
    "activity log and answer questions about what they did.\n\n"
    "You have one tool: run_sql(query). Use it to look up whatever you need "
    "from activity.db. The schema + example queries + example questions are "
    "in the DATABASE REFERENCE below. When you have enough data, respond in "
    "2–5 short sentences — no headers, no bullet lists unless the question "
    "is literally \"list X.\" Reference actual apps, titles, times. Be warm. "
    "Don't lecture.\n\n"
    "Always filter by `ts` (unix seconds) for time ranges. Today = "
    "ts >= strftime('%s', date('now','localtime')). Keep queries focused — "
    "you have a budget of 6 tool calls per answer. Prefer one well-aimed "
    "query over several narrow ones.\n\n"
    "If the question isn't about the user's computer activity, say so in one "
    "line and offer a better one.\n\n"
    "--- DATABASE REFERENCE ---\n"
    + CHAT_SCHEMA_DOC
)


RUN_SQL_TOOL_SPEC = {
    "name": "run_sql",
    "description": (
        "Run a single read-only SELECT or WITH query against the brainloop "
        "activity.db (SQLite). Returns columns + rows (up to 200). Anything "
        "other than SELECT/WITH is rejected. Use this to answer the user's "
        "question about what they did."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "A single SELECT or WITH statement. No semicolons, no writes.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}
