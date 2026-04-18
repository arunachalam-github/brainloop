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
- When YouTube / social / video streaming dominates an "act", call it out as
  "the monkey" (lowercase, a running joke with the user). Examples:
  "The monkey arrived before the work did." · "33 minutes of Tamil cinema
  before the day started." · "Gratification Monkey".
- Name what the user was actually watching/reading by clustering page_text
  slices into themes ("Sarpatta BGMs", "Master", "Tamil cinema", "Facebook
  feed"). Never list raw URL-style strings. If several page_text samples
  share a subject, collapse them into a single named theme.
- Avoid metrics-speak ("productivity score", "efficiency"). Prefer "focus",
  "wandered", "drifted", "settled".
- Present tense for the "Now" act, past tense for earlier acts.

Structure:
- headline: one or two sentences, editorial, specific to today's events.
- subtitle: lowercase "{weekday}, {month} {day} · {N} switches · {H}h{M} focus"
- acts: 2-4 entries. Each has title (Early morning / Mid-morning / Afternoon
  / Evening / Now — pick whatever fits), a time_range, one_liner, narrative
  (2-3 sentences), and 0-2 callouts. Callouts highlight a specific block of
  the act (gratification monkey, focus block, call, etc.).
- widgets: fill every field from the context; if the day has no calls,
  on_calls.count is 0 (not missing).
- intensity_buckets: pass through the provided buckets verbatim.

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
            "headline": {"type": "string"},
            "subtitle": {"type": "string"},
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
                "minItems": 1,
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
                        "title": {"type": "string"},
                        "time_range": {"type": "string"},
                        "one_liner": {"type": "string"},
                        "narrative": {"type": "string"},
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
                                    "label": {"type": "string"},
                                    "time": {"type": "string"},
                                    "duration_min": {"type": "integer"},
                                    "body": {"type": "string"},
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
                            "label": {"type": "string"},
                            "range": {"type": "string"},
                        },
                    },
                    "doom_scroll": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["minutes", "detail", "worst_range"],
                        "properties": {
                            "minutes": {"type": "integer"},
                            "detail": {"type": "string"},
                            "worst_range": {"type": "string"},
                        },
                    },
                    "hours_by_app": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["app", "minutes"],
                            "properties": {
                                "app": {"type": "string"},
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
                            "detail": {"type": "string"},
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
                                "start": {"type": "string"},
                                "end": {"type": "string"},
                                "minutes": {"type": "integer"},
                            },
                        },
                    },
                    "things_read": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["time", "title", "source"],
                            "properties": {
                                "time": {"type": "string"},
                                "title": {"type": "string"},
                                "source": {"type": "string"},
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
