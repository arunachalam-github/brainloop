# Brainloop — Claude Code Guide

brainloop is a macOS background daemon that silently records computer activity into a local SQLite database. All data stays on the user's Mac — nothing is sent anywhere.

## Database

- **Location:** `~/Library/Application Support/brainloop/activity.db`
- **Table:** `activity_log`
- **Query with Bash tool:** `sqlite3 ~/Library/Application\ Support/brainloop/activity.db "SELECT ..."`

## Key columns

| Column | Description |
|---|---|
| `ts` | Unix timestamp (float) |
| `ts_iso` | ISO 8601 string e.g. `2026-04-16T14:32:01` |
| `trigger` | What caused this row: `app_switch`, `title_changed`, `window_changed`, `focus_changed`, `value_changed`, `heartbeat`, `audio_start`, `audio_stop`, `mic_start`, `mic_stop`, `startup` |
| `app_name` | Frontmost application name (e.g. `Google Chrome`, `Code`, `Slack`, `zoom.us`) |
| `bundle_id` | macOS bundle ID (e.g. `com.google.Chrome`) |
| `window_title` | Focused window title — includes tab/document name |
| `browser_url` | URL from browser address bar (browsers only, requires Accessibility permission) |
| `ax_element_text` | Text of the focused UI element (e.g. selected text, input field value) |
| `page_text` | Visible text on the current browser page via JS injection (requires Chrome "Allow JavaScript from Apple Events") |
| `visible_text` | Text from the AX tree of the frontmost window |
| `audio_playing` | `1` if audio was playing at capture time |
| `mic_active` | `1` if microphone was open (indicates a call) |
| `audio_device` | Name of the output audio device |
| `mic_device` | Name of the input device |

## How to answer activity questions

Use the Bash tool to run `sqlite3` queries. Always filter by `ts` (unix timestamp) for time ranges.

```bash
# Last 30 minutes
sqlite3 ~/Library/Application\ Support/brainloop/activity.db \
  "SELECT datetime(ts,'unixepoch','localtime'), app_name, window_title
   FROM activity_log
   WHERE ts > strftime('%s', datetime('now', '-30 minutes'))
   ORDER BY ts;"

# Am I on a call right now?
sqlite3 ~/Library/Application\ Support/brainloop/activity.db \
  "SELECT mic_active, audio_playing, app_name FROM activity_log ORDER BY ts DESC LIMIT 1;"

# What sites did I visit today?
sqlite3 ~/Library/Application\ Support/brainloop/activity.db \
  "SELECT DISTINCT browser_url, window_title FROM activity_log
   WHERE ts > strftime('%s', date('now')) AND browser_url IS NOT NULL
   ORDER BY ts;"

# What was I reading at a specific time? (use page_text)
sqlite3 ~/Library/Application\ Support/brainloop/activity.db \
  "SELECT datetime(ts,'unixepoch','localtime'), window_title, page_text
   FROM activity_log
   WHERE ts BETWEEN strftime('%s','2026-04-16 15:00:00') AND strftime('%s','2026-04-16 15:30:00')
   AND page_text IS NOT NULL
   ORDER BY ts LIMIT 5;"
```

## Tips

- Use `trigger = 'app_switch'` rows to see app transitions — these are the clearest signal for "what was I doing when"
- Use `mic_active = 1` rows to identify calls and who they were with (window_title shows meeting name in Zoom/Slack)
- `page_text` captures what was visible on screen in the browser — useful for "what was I reading"
- `window_title` for Slack includes the channel or DM name — great for reconstructing conversations
- Heartbeat fires every 60 seconds — useful for "how long was I in X" (count heartbeat rows)
- Screen lock shows as `app_name = 'loginwindow'`

## Example questions users ask

- "Give me a summary of my day"
- "What did I do in the last 30 minutes?"
- "Was I on any calls today? Who with?"
- "What was I reading/watching around 3pm?"
- "What was I surfing on YouTube today?"
- "Am I on a call right now?"
- "What did I work on most this morning?"
- "What websites did I visit today?"
- "When did I take a break?"
- "What Slack channels was I active in?"
