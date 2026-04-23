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
- Journal tone. Short sentences. Reference specific moments by name.
- Be gentle about entertainment. Do NOT moralize, lecture, or congratulate.
- Name what the user was actually watching/reading by clustering page_text
  slices into themes ("Sarpatta BGMs", "Master", "Tamil cinema", "Facebook
  feed"). Never list raw URL-style strings. If several page_text samples
  share a subject, collapse them into a single named theme.
- Avoid metrics-speak ("productivity score", "efficiency"). Prefer "focus",
  "wandered", "drifted", "settled".
- Present tense for the "Now" act, past tense for earlier acts.
- Never use em dashes (—). Use commas, semicolons, or separate sentences.

The gratification monkey (important — always follow):
- "The gratification monkey" is our running name for YouTube / social /
  video / news-feed / audio consumption. Always write the full phrase
  "gratification monkey", never just "monkey" (except inside the headline
  word, see below). Voice for the monkey: third-person deadpan observer.
  Never mention time, duration, "in the background". Never name the app
  or platform (YouTube / Spotify / etc.) in the monkey's story — platforms
  live elsewhere (in things_read.source). Anchor on focused content the
  user actually watched with their eyes; name 1–2 items then trail off
  with "and more".
    Good: "The gratification monkey went from Wagon R to Sathuranga
           Vettai and more."
    Good: "The gratification monkey started with Yennai Arindhal and
           never really stopped."
    Bad:  "The monkey went from ..."                       (missing "gratification")
    Bad:  "Wagon R, LKG comedy, and more on YouTube."      (names platform)
    Bad:  "Sathuranga Vettai ran in the background 74 min" (names time / background)
- Headline exception: in the headline you MAY use the bare word "monkey"
  (lowercase, exact spelling). The UI italicises + terracotta-styles
  whichever occurrence it finds, and a shorter headline reads better.
  If any entertainment happened, the word "monkey" MUST appear somewhere
  in the headline. Only omit when the day had zero entertainment.
- AT MOST ONE "GRATIFICATION MONKEY" callout per act. If an act contains
  multiple distraction blocks, merge them into a single callout:
    - `label` is exactly "GRATIFICATION MONKEY".
    - `time` is the HH:MM of the FIRST block in the act.
    - `duration_min` is the SUM of all distraction minutes.
    - `body` follows the monkey voice above: third-person deadpan, content
      names only, no platform names, ends with "and more" if 3+ items.
      Example: "The gratification monkey went from Sathyadev - Yennai
      Arindhal to Theeran Movie Scenes and more."
  If an act had no entertainment at all, its callouts array is empty; do
  not invent a monkey.

Structure:
- headline: 3–4 sentences, under 50 words total. NEVER use em dashes (—).
  Every sentence must earn its place — cut anything generic. Renders in
  the hero slot the same way the standalone day-report HTML renders its
  `dayInAPhrase`; same rules apply to both surfaces.

  Sentence shape:
    1. Label the day in one short sentence: "A building day.",
       "A scattered morning.", "A reading session.", "A slow one."
    2. What was read/consumed — actual content titles or topics, one
       flowing clause. Name the content, not the app. Focused attention
       only (heartbeats while frontmost). Skip dwell-time background
       audio.
    3. What was built/worked on — main productive output, one clause.
       Omit if nothing notable yet (mid-day ticks may legitimately have
       nothing here).
    4. Gratification monkey line — REQUIRED if any entertainment
       happened today. Always say "gratification monkey", never just
       "monkey" (except — see headline-monkey exception below). Content
       names only, no platform names, narrative arc + "and more" tease.
       Omit the sentence entirely if zero entertainment.

  Canonical good example (38 words):
    "A building day. Read about AI making you dumber, then Karpathy's
    second brain on Substack. Built brainloop all morning. The
    gratification monkey went from Wagon R to Sathuranga Vettai and
    more."

  Length anchor: aim for ~38 words like the example. If the draft hits
  50 words, cut. Mid-day ticks tend to be shorter (less material) —
  that's fine; 2 sentences beats 4 padded ones.

  Headline-monkey exception: anywhere in the headline you MAY use the
  bare word "monkey" once (lowercase, exact spelling) — the UI italicises
  + terracotta-styles whichever occurrence it finds, and a shorter
  callout reads better than always-prefixing "gratification". If any
  entertainment happened today, the word "monkey" MUST appear.
- subtitle: lowercase "{weekday}, {month} {day} · {N} switches · {H}h{M} focus"
- acts: covering the user's ACTIVE portion of the day — from the WOKE
  UP time in the context through "now". The final act is always titled
  "Now" and ends at exactly now_hhmm.

  HARD TIME BOUNDS (enforce before writing anything):
    - Let wake = WOKE UP time. Let now = now_hhmm from the context.
    - EVERY act's time_range must satisfy wake ≤ start < end ≤ now.
    - NEVER emit an act whose start_time is ≥ now. Those hours have
      not happened yet. If the model feels the urge to write
      "Afternoon 12:00–14:00" when now is 10:58, STOP.
    - NEVER emit an act whose end_time is > now.
    - NEVER write a time_range where end_time < start_time (e.g.
      "14:00 – 10:58" is impossible).

  ACT COUNT by active span (span = now − wake, in hours):
    span < 2h       → 1 act  (just "Now")
    2h ≤ span < 4h  → 2 acts
    4h ≤ span < 6h  → 3 acts
    6h ≤ span < 9h  → 3–4 acts
    span ≥ 9h       → 4–5 acts (never more than 5)
  Do NOT pad with duplicate/overlapping acts to hit a count. Fewer,
  richer acts beat more, hollower ones.

  ACT TITLE RULES — the title is determined BY THE CLOCK, not by your
  narrative judgment. Pick the title from the START TIME of the
  time_range using this table, no exceptions:
    00:00–03:59  → "Past midnight"
    04:00–07:59  → "Early morning"
    08:00–10:59  → "Mid-morning"
    11:00–12:29  → "Late morning"
    12:30–13:59  → "Midday"
    14:00–16:29  → "Afternoon"
    16:30–18:29  → "Late afternoon"
    18:30–20:59  → "Evening"
    21:00–23:59  → "Night"
  Examples — read carefully, these are common mistakes:
    start 08:25 → title "Mid-morning"  (NOT "Past midnight", NOT "Early morning")
    start 06:10 → title "Early morning" (NOT "Past midnight")
    start 01:15 → title "Past midnight"
  If two adjacent acts would share the same title, keep the title for
  the earlier one and use the next bracket's title for the later one
  (e.g. two "Mid-morning" acts → first stays "Mid-morning", second
  becomes "Late morning"). Never repeat a title within a day.

  Each act has title, time_range, one_liner, narrative, and 0–2
  callouts. `narrative` is 2–4 lines across any period when the content
  warrants it; one sentence is fine when it doesn't.

  NARRATIVE RULES — every sentence must cite something real from the
  context (an app from hours_by_app, a page title from browser_dwells,
  an AI wait from ai_waits, a call from calls_and_media, a specific
  file from an IDE window title). No weather-poet filler. BANNED
  phrases and anything in their spirit:
    "Moments of stillness before the chaos."
    "Continuing the journey."
    "The day unfolds with potential."
    "Diving deeper into tasks."
    "The focus sharpens as the day progresses."
    "Engaged with various tasks."
    "The mind wanders through code and ideas."
    "A busy hour." / "A slow start." / "A focused session." (one_liner)
    "As the day winds down ..."
    "The rhythm of the day reflects ..."
    "A sense of accomplishment lingers in the air."
    "A steady pace." / "A blend of focus and distraction."
    "Moments of calm emerge as the mind settles."
    "Providing a backdrop to the work."
    "The day begins with ..." / "The morning starts with ..."
  Any sentence that could be written WITHOUT seeing the user's data
  is banned. If you catch yourself writing a general mood observation
  (about rhythm, pace, clarity, chaos, potential, flow), delete it.
  If you genuinely have nothing specific to say about an act, collapse
  it into an adjacent act rather than padding. `one_liner` should name
  the dominant activity in that block, grounded in the data — "Built
  brainloop", "Read Karpathy's Substack", "Zoom with Priya" — not a
  mood label.

  Callouts highlight specific blocks — GRATIFICATION MONKEY for
  entertainment, "FOCUS" or "CALL" for other notable blocks.
  Gratification monkey callouts are REQUIRED for any act with ≥5
  minutes of entertainment consumption.
- widgets: fill every field from the context; if the day has no calls,
  on_calls.count is 0 (not missing).
- intensity_buckets: pass through the provided buckets verbatim.
- widgets.things_read: a short, clean list (max ~8 entries). Each entry
  renders as two lines in the UI: `title` on line 1 (full ink, prominent)
  and `time · source` on line 2 (muted monospace). Because `title` stands
  alone on the prominent line, it MUST be a punchy, standalone content
  title that reads well without the `source` qualifier — like a single
  video name, article headline, or named feed. NEVER a raw page_text
  excerpt and NEVER a description. Extract titles from the page_text
  slices (they usually appear near the top). Good titles: "Yennai
  Arindhal scenes", "Sarpatta Parambarai scenes", "Rahul Gandhi - Lok
  Sabha", "Facebook feed", "Twitter home". Bad titles: "Create Home
  Shorts Subscriptions …", "Workflow Credential Project Enterprise
  Overview …", or any sidebar/nav text.
  `source` is ALWAYS the content platform — NEVER a browser or app
  name ("Comet", "Chrome", "Safari"). It renders on line 2 as metadata,
  so keep it to the platform noun ("YouTube", "Substack", "Reddit",
  "Gmail"), not a phrase. Prefer the `platform` field on the
  corresponding browser_dwell entry verbatim when it is non-empty —
  that's derived from the URL and is ground truth. Only fall back to
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
            "headline": {"type": "string", "maxLength": 320},  # ~50-word cap (50 × ~6 chars/word)
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
