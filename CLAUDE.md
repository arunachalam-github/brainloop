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

---

## Day Report — HTML generation

When the user says "prepare the day report", "create the report", or "generate today's report", produce a standalone HTML file using the locked design template.

### Files

| Path | Purpose |
|---|---|
| `apps/brainloop_day_template.html` | Locked design base — never modify |
| `apps/brainloop_YYYY-MM-DD.html` | Output — one file per day |

### How the bundle works

The HTML is a self-contained bundle. All assets live inside a `<script type="__bundler/manifest">` tag as a JSON map of UUID → `{ mime, compressed, data }` (gzip+base64). A separate `<script type="__bundler/template">` tag holds the HTML shell as a JSON string.

To generate a report you only swap **one UUID** — the data asset:

```
DATA_UUID = 'c175b7cb-adee-42a1-bda7-b1807b666c3e'   ← window.BRAINLOOP_DATA
JSX_UUID  = '5bb706df-8459-417a-b602-22ae0a3aa1d8'   ← React components (read-only)
```

### Build pipeline (Python)

```python
import json, gzip, base64

DATA_UUID = 'c175b7cb-adee-42a1-bda7-b1807b666c3e'
TEMPLATE  = 'apps/brainloop_day_template.html'
OUTPUT    = 'apps/brainloop_YYYY-MM-DD.html'   # set the date

with open(TEMPLATE) as f:
    html = f.read()

# Parse manifest
TAG = '<script type="__bundler/manifest">'
ms  = html.find(TAG)
me  = html.find('</script>', ms)
manifest = json.loads(html[ms + len(TAG):me])

# Encode new data
new_js    = '// ...\nwindow.BRAINLOOP_DATA = { ... };'   # see shape below
compressed = gzip.compress(new_js.encode('utf-8'))
manifest[DATA_UUID] = {
    'mime': 'application/javascript',
    'compressed': True,
    'data': base64.b64encode(compressed).decode('ascii'),
}

# Rebuild and write
new_html = html[:ms + len(TAG)] + json.dumps(manifest, separators=(',',':')) + html[me:]
with open(OUTPUT, 'w') as f:
    f.write(new_html)
```

**CRITICAL:** Always read from `brainloop_day_template.html` as the base — never from a previous day's report.

### window.BRAINLOOP_DATA shape

```js
window.BRAINLOOP_DATA = {
  meta: {
    date:          "Friday, April 18 2026",   // long form
    dateShort:     "Fri Apr 18",              // used in hero eyebrow
    dayInAPhrase:  "...",                     // see dayInAPhrase rules below
    windowStartMs: 1776450600000,             // midnight local time as unix ms
    bucketSecs:    30 * 60,                   // 30-min buckets
  },
  switches: {
    buckets: [{ idx, count, apps }],  // one entry per 30-min slot from midnight
    labels:  [{ label, idx_f }],      // x-axis labels: "12AM", "2AM", "4AM" …
    total:   224,                     // total app_switch rows for the day
  },
  periods: [   // 3–5 acts depending on day length and content (see synthesis rules)
    {
      id:          "morning",
      label:       "Early morning",       // display label
      range:       "05:30 – 06:06",       // use – (en dash), not hyphen
      openedWith:  "...",                 // period-line2: muted secondary sentence
      productive: {
        headline:           "...",        // period-line1: main sentence, full ink
        focusBlockMinutes:  36,
        focusBlockRange:    "05:30 – 06:06",
        strands: [{ app, detail, minutes }],
      },
      monkey: {                           // omit key entirely if no monkey
        arrivedAt: "05:30",
        stayedFor: 36,                    // minutes
        title:     "...",
        story:     "...",
        trail: [{ time, what, where }],   // where = "YouTube", "Facebook", etc.
      },
      calls: [],                          // [{ time, title, minutes }]
    },
    // … 2 more periods
  ],
  widgets: {
    focusStreak:  { minutes, range, label, context },
    appHours:     [{ app, minutes, pct }],   // pct 0–1, drives the bar width
    calls:        { count, totalMinutes, items: [{ time, title, minutes }] },
    reading:      [{ title, where, when }],
    waitingOnAI:  { minutes, context, sessions },
    breaks:       [{ time, kind, minutes }],
    doomScroll:   { minutes, moments, worstWindow, note },
  },
};
```

**windowStartMs** — must equal the epoch used in the bucket SQL query so bar positions align with the x-axis labels. Use local midnight derived from the auto-detected timezone:
```python
import sqlite3
from datetime import datetime, timezone, timedelta

# Auto-detect local timezone from the DB using TWO independent methods — both must agree
conn = sqlite3.connect(os.path.expanduser('~/Library/Application Support/brainloop/activity.db'))

# Method 1: ts_iso (local time stored by daemon) vs ts (UTC unix)
row = conn.execute("SELECT ts, ts_iso FROM activity_log ORDER BY ts DESC LIMIT 1").fetchone()
ts_unix, ts_iso = row
utc_dt = datetime.fromtimestamp(int(ts_unix), tz=timezone.utc).replace(tzinfo=None)
tz_method1 = int((datetime.fromisoformat(ts_iso) - utc_dt).total_seconds())

# Method 2: SQLite's own localtime conversion vs utc
row2 = conn.execute("SELECT datetime(ts,'unixepoch') as utc, datetime(ts,'unixepoch','localtime') as local FROM activity_log ORDER BY ts DESC LIMIT 1").fetchone()
tz_method2 = int((datetime.fromisoformat(row2[1]) - datetime.fromisoformat(row2[0])).total_seconds())

assert tz_method1 == tz_method2, f"Timezone mismatch: method1={tz_method1} method2={tz_method2} — do not proceed"
TZ_OFFSET = tz_method1
# e.g. IST → 19800, EST → -18000, PST → -28800

LOCAL_TZ = timezone(timedelta(seconds=TZ_OFFSET))
midnight = datetime(YYYY, MM, DD, 0, 0, 0, tzinfo=LOCAL_TZ)
midnight_epoch = int(midnight.timestamp())
windowStartMs = midnight_epoch * 1000
```

**CRITICAL — bucket epoch must match windowStartMs:** The bucket `idx` SQL must use the same midnight epoch. Use the unix timestamp of local midnight, not `strftime('%s','YYYY-MM-DD 00:00:00')` (which is UTC midnight). See query 2 below.

### DB queries to populate the data

Run ALL of the following queries. Every query contributes to the final report — skipping any will produce an incomplete picture.

Note: `ts + TZ_OFFSET` converts UTC unix to local time. `TZ_OFFSET` is auto-detected from the DB (see windowStartMs section above) — do not hardcode `19800` or any fixed value. Replace `YYYY-MM-DD` with the report date.

---

#### 1. Day boundaries — first and last activity

```sql
-- What was on screen when the day started
SELECT datetime(ts+TZ_OFFSET,'unixepoch') as local_time, app_name, window_title
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH
ORDER BY ts LIMIT 5;

-- Latest activity (data cutoff)
SELECT datetime(ts+TZ_OFFSET,'unixepoch') as local_time, app_name, window_title
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH
ORDER BY ts DESC LIMIT 3;
```

#### 2. Total switches + 30-min buckets (seismo chart)

**IMPORTANT:** Use the unix timestamp of local midnight as the bucket epoch — NOT `strftime('%s','YYYY-MM-DD 00:00:00')` (that is UTC midnight, which is off by your local offset). This epoch must match `windowStartMs` exactly, or the chart bars will appear at the wrong time of day.

```python
# Compute local midnight epoch once (TZ_OFFSET auto-detected from DB — see windowStartMs section)
LOCAL_TZ = timezone(timedelta(seconds=TZ_OFFSET))
midnight_epoch = int(datetime(YYYY, MM, DD, 0, 0, 0, tzinfo=LOCAL_TZ).timestamp())
```

```sql
-- Total (replace MIDNIGHT_EPOCH with computed value, e.g. 1776537000)
SELECT COUNT(*) FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400 AND trigger='app_switch';

-- Per 30-min bucket — idx 0 = midnight local, idx 14 = 07:00 IST, etc.
SELECT
  CAST((ts - MIDNIGHT_EPOCH) / 1800 AS INTEGER) as idx,
  COUNT(*) as count,
  GROUP_CONCAT(DISTINCT app_name) as apps
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400 AND trigger='app_switch'
GROUP BY idx ORDER BY idx;
```

Build the full 48-slot array (idx 0–47) with zeros for empty slots — the chart renders all 48 bars:

**IMPORTANT:** `apps` must be a Python list, not the raw `GROUP_CONCAT` string. Split the DB value before storing, or the chart tooltip will throw `b.apps.join is not a function`.

```python
raw = { row['idx']: row for row in bucket_query_results }
buckets = [{"idx": i, "count": raw.get(i, {}).get("count", 0), "apps": raw.get(i, {}).get("apps", "").split(",") if raw.get(i, {}).get("apps") else []} for i in range(48)]
```

#### 3. Time spent per app (heartbeats × 60s)

```sql
SELECT app_name, COUNT(*) as heartbeats, COUNT(*)*60 as seconds
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400 AND trigger='heartbeat'
GROUP BY app_name ORDER BY heartbeats DESC;
```

#### 4. ALL Chrome activity — every distinct tab visited

This is the primary source for reading, social media, email, docs, shopping, and video. Do NOT rely on audio_playing alone — most reading has no audio.

```sql
SELECT
  datetime(MIN(ts)+TZ_OFFSET,'unixepoch') as first_seen,
  window_title,
  browser_url
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND app_name = 'Google Chrome'
AND trigger = 'title_changed'
AND window_title NOT IN ('','New Tab','New tab','Untitled','Extensions',
  'Recent download history','Screenshot - GoFullPage')
AND window_title NOT LIKE 'Extensions -%'
GROUP BY window_title
ORDER BY MIN(ts);
```

After fetching, classify each row by domain/title pattern:

| Pattern | Category |
|---|---|
| `Gmail`, `Inbox`, email subject lines | **Email** |
| `substack.com`, newsletter domains, article titles | **Article / Substack** |
| `YouTube`, `youtu.be` | **Video (YouTube)** |
| `Facebook`, `Instagram`, `X –`, `Twitter` | **Social media** |
| `github.com`, repo names | **GitHub** |
| `Google Docs`, `Google Sheets`, `Google Slides` | **Writing / Docs** |
| `amazon.`, `flipkart.`, `myntra.`, shopping domains | **Ecommerce** |
| `Product Hunt`, `Hacker News`, `Reddit` | **Discovery / community** |
| `WhatsApp`, `Telegram`, `Slack`, `Messenger` | **Messaging** |
| `Paytm`, `bank`, `finance` domains | **Finance** |
| `zoom.us`, `meet.google.com`, `teams.microsoft.com` | **Calls** |
| Anything else with a real title | **Browsing** |

#### 5. Audio and video content (what was actively playing)

```sql
SELECT
  datetime(MIN(ts)+TZ_OFFSET,'unixepoch') as first_seen,
  window_title
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND audio_playing = 1
AND trigger = 'title_changed'
AND window_title NOT IN ('','Untitled')
GROUP BY window_title
ORDER BY MIN(ts);
```

Use this to populate `monkey.trail` entries — these are the actual pieces of content the user consumed.

#### 6. Calls (mic active)

```sql
SELECT datetime(ts+TZ_OFFSET,'unixepoch') as local_time, app_name, window_title
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400 AND mic_active=1
ORDER BY ts;
```

#### 7. All non-Chrome app activity (full macOS picture)

Do not ignore non-browser apps. Code editor, Slack, Notion, Figma, Terminal, Xcode, etc. all appear here.

```sql
-- Distinct windows per non-browser app
SELECT
  datetime(MIN(ts)+TZ_OFFSET,'unixepoch') as first_seen,
  app_name,
  window_title,
  COUNT(*) as events
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND app_name NOT IN ('Google Chrome','Safari','Firefox','Arc','loginwindow','Finder',
  'CoreServicesUIAgent','SecurityAgent','SystemUIServer')
AND trigger IN ('app_switch','title_changed','heartbeat')
GROUP BY app_name, window_title
ORDER BY MIN(ts);
```

#### 8. Focused element / visible text (what was being typed or read in non-browser apps)

```sql
SELECT datetime(ts+TZ_OFFSET,'unixepoch') as local_time, app_name, ax_element_text, visible_text
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND (ax_element_text IS NOT NULL OR visible_text IS NOT NULL)
AND app_name NOT IN ('Google Chrome','loginwindow')
ORDER BY ts LIMIT 50;
```

#### 9. AI platform usage — browser-based (Claude.ai, ChatGPT, Gemini, Perplexity, etc.)

Window titles and page_text reveal what the user was doing on AI platforms in the browser.

```sql
-- AI platform tab visits (titles + page content)
SELECT
  datetime(MIN(ts)+TZ_OFFSET,'unixepoch') as first_seen,
  window_title,
  browser_url,
  MAX(page_text) as page_content
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND (
  window_title LIKE '%Claude%'
  OR window_title LIKE '%ChatGPT%'
  OR window_title LIKE '%Gemini%'
  OR window_title LIKE '%Perplexity%'
  OR window_title LIKE '%Copilot%'
  OR window_title LIKE '%Grok%'
  OR window_title LIKE '%Mistral%'
  OR browser_url LIKE '%claude.ai%'
  OR browser_url LIKE '%chatgpt.com%'
  OR browser_url LIKE '%chat.openai.com%'
  OR browser_url LIKE '%gemini.google.com%'
  OR browser_url LIKE '%perplexity.ai%'
  OR browser_url LIKE '%copilot.microsoft.com%'
  OR browser_url LIKE '%grok.x.com%'
)
GROUP BY window_title
ORDER BY MIN(ts);
```

AI platform classification:

| App / URL | Platform |
|---|---|
| `claude.ai`, app_name = `Claude` | Anthropic Claude |
| `chatgpt.com`, `chat.openai.com` | OpenAI ChatGPT |
| `gemini.google.com` | Google Gemini |
| `perplexity.ai` | Perplexity |
| `copilot.microsoft.com` | Microsoft Copilot |
| `cursor.sh`, app_name = `Cursor` | Cursor (AI IDE) |
| `grok.x.com` | xAI Grok |
| app_name = `Claude` (desktop app) | Claude Code / Claude desktop |

For **Claude desktop app** specifically, use `visible_text` to understand what was being discussed:

```sql
SELECT datetime(ts+TZ_OFFSET,'unixepoch') as local_time, visible_text
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND app_name = 'Claude'
AND visible_text IS NOT NULL
ORDER BY ts LIMIT 20;
```

#### 10. IDE and coding platform usage — what was being built

VS Code (`Code`), Cursor, Xcode, IntelliJ, WebStorm, PyCharm, etc. Window titles expose the file name, project name, and often the git branch.

```sql
-- All IDE window titles — file + project context
SELECT
  datetime(MIN(ts)+TZ_OFFSET,'unixepoch') as first_seen,
  app_name,
  window_title,
  COUNT(*) as heartbeats
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND app_name IN ('Code','Cursor','Xcode','IntelliJ IDEA','WebStorm','PyCharm',
  'Android Studio','RubyMine','CLion','GoLand','Rider','Nova','Sublime Text',
  'TextMate','BBEdit','Zed')
AND trigger = 'heartbeat'
GROUP BY app_name, window_title
ORDER BY MIN(ts);
```

VS Code window title format: `filename — project_name` or `● filename — project_name` (● = unsaved).
Use `window_title` to extract:
- **File being edited** — everything before ` — `
- **Project name** — everything after ` — `
- **Unsaved state** — leading `●`

Also pull visible text and AX element text to understand what was typed:

```sql
SELECT datetime(ts+TZ_OFFSET,'unixepoch') as local_time, app_name, window_title,
       ax_element_text, visible_text
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND app_name IN ('Code','Cursor','Xcode','IntelliJ IDEA','WebStorm','PyCharm')
AND (ax_element_text IS NOT NULL OR visible_text IS NOT NULL)
ORDER BY ts LIMIT 30;
```

#### 11. Terminal / shell activity

```sql
SELECT
  datetime(MIN(ts)+TZ_OFFSET,'unixepoch') as first_seen,
  window_title,
  COUNT(*) as heartbeats
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND app_name IN ('Terminal','iTerm2','Warp','Hyper','Alacritty')
AND trigger = 'heartbeat'
GROUP BY window_title
ORDER BY MIN(ts);
```

Terminal window titles show the shell session name, working directory, and active command (e.g. `brainloop — python3 daemon.py`).

#### 12. page_text — actual content of what was read in Chrome (HIGHEST VALUE — always run this)

`page_text` is JS-injected body text from Chrome. It contains the real article text, email body, Substack content, AI conversation topic, etc. 2800+ rows are captured on a typical day. Use this to confirm and enrich `widgets.reading` entries — don't just use the tab title.

```sql
SELECT
  datetime(MIN(ts)+TZ_OFFSET,'unixepoch') as first_seen,
  window_title,
  browser_url,
  SUBSTR(MAX(page_text), 1, 500) as content_snippet
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND page_text IS NOT NULL AND page_text != 'empty'
AND app_name = 'Google Chrome'
GROUP BY window_title
ORDER BY MIN(ts);
```

Use `content_snippet` to:
- Confirm what an article was actually about (not just the tab title)
- Extract the email subject/body for Gmail rows
- Identify AI platform conversation topics (Claude.ai, ChatGPT window titles are generic — page_text has the actual prompt/response)
- Understand what was written in Google Docs (once Google Docs fix is applied)

#### 13. browser_url — domain-level grouping for categorisation

`browser_url` gives the authoritative domain — more reliable than `window_title` for classification. Use this to confirm categories from query 4 (e.g. `aibyaakash.com` = Substack, `github.com` = GitHub) and to deduplicate tabs that share a title.

```sql
SELECT
  SUBSTR(browser_url, 1, INSTR(browser_url||'/', '/', 9) - 1) as domain,
  COUNT(DISTINCT window_title) as pages_visited,
  MIN(datetime(ts+TZ_OFFSET,'unixepoch')) as first_seen
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND browser_url IS NOT NULL AND browser_url != ''
AND app_name = 'Google Chrome'
GROUP BY domain
ORDER BY pages_visited DESC;
```

#### 14. value_changed — typing activity (confirms writing vs reading)

`value_changed` fires every time the user types in any text field (debounced 1.5s). ~300 rows on a typical day. Combined with `app_name` + `ax_element_text`, this distinguishes:
- Typing in Code/Cursor → active coding, not just reading
- Typing in Claude/ChatGPT → prompting AI
- Typing in Gmail/Docs → writing email or document
- Typing in Terminal → running commands

```sql
SELECT
  app_name,
  COUNT(*) as typing_events,
  GROUP_CONCAT(DISTINCT window_title) as windows,
  GROUP_CONCAT(DISTINCT SUBSTR(ax_element_text, 1, 80)) as sample_text
FROM activity_log
WHERE ts > MIDNIGHT_EPOCH AND ts < MIDNIGHT_EPOCH + 86400
AND trigger = 'value_changed'
GROUP BY app_name
ORDER BY typing_events DESC;
```

---

### How to synthesise all queries into the report

1. **Periods** — divide the day into acts based on activity clusters. Use app_switch timeline + heartbeat counts to find natural breaks (long `loginwindow` gaps = away from desk). Default is 3 acts. Use 4–5 acts if the day spans more than 6 active hours and there is enough distinct content to justify more granularity. Never exceed 5 acts.
2. **productive.headline** — what the user spent the most focused time doing. Check queries 3, 7, 10 — Code/Cursor/Xcode heartbeats, Docs/Sheets, AI sessions. Confirm with query 14 (typing events) — typing in Code = coding, typing in Claude = prompting. If any period had multiple distinct activities, write 2–4 lines to cover them — applies to all periods, not just morning. Do not compress everything into one sentence. Only expand when the content actually fills it.
3. **openedWith** — the very first thing in that period from the app_switch timeline (query 1). Can be 2–4 lines across any period if there's enough to note.
4. **monkey.trail** — query 5 (audio playing) + query 4 social/video + ecommerce categories. These are distraction moments.

   **Trail rule:** Include ALL titles from query 5 (audio_playing=1, title_changed) — even brief skips of a few seconds. The trail is the complete record of what the monkey touched; no dwell-time filter. `what` = content title, `where` = platform name (YouTube, Facebook, Spotify, etc.).

   **monkey.story writing rules:** Write in **third-person deadpan observer** voice. Always say **"The gratification monkey"**, never just "The monkey". Never mention time, duration, or "in the background". Never name the app/platform in `story` — platform names live in `trail[].where` only. Anchor on **focused content** (most heartbeats with audio_playing=1 AND frontmost window). Dwell-time audio belongs in trail but NOT as the story anchor. Name 1–2 focused items then trail off with "and more".
   - Good: "The gratification monkey went from Wagon R to Sathuranga Vettai and more."
   - Good: "The gratification monkey started with Yennai Arindhal and never really stopped."
   - Bad: "The monkey went from Wagon R to Sathuranga Vettai and more." (missing "gratification")
   - Bad: "Wagon R restoration, LKG comedy, Avvai Shanmugi and more on YouTube." (names platform)
   - Bad: "Sathuranga Vettai ran in the background for 74 minutes from 08:16 to 09:30."
5. **widgets.reading** — query 4 rows classified as Article, Email, GitHub, Docs, Discovery + query 9 AI platform titles. **Enrich with query 12 `content_snippet`** — use actual page text to describe what was read, not just the tab title.
6. **widgets.appHours** — query 3 heartbeats → minutes, top 4–5 apps.
7. **widgets.doomScroll** — Social + Video + Ecommerce minutes from queries 4+5, count distinct moments, worst window.
8. **AI usage** — queries 9 + 12: which AI platforms were used, how long, and **what topic** (from `page_text` snippet — tab titles like "Claude" or "ChatGPT" are generic; page_text has the actual conversation subject).
9. **Coding context** — query 10: distinct files + projects edited (parse VS Code title `filename — project`). Use query 14 typing events to confirm active coding vs passive reading. Surface in `productive.strands`.
10. **Categorisation confidence** — use query 13 domain grouping to resolve ambiguous `window_title` rows from query 4. `aibyaakash.com` = Substack, `github.com` = GitHub, `docs.google.com` = Docs, etc.

### dayInAPhrase rules

Write **3–4 sentences**. **Never use em dashes ( — ).**

Structure:
1. **Label the day** — one short sentence: "A building day.", "A slow morning.", "A reading session.", "A scattered one."
2. **What was read/consumed** — actual content titles or topics, flowing sequence. Name the content, not the app. Focused attention only (heartbeats while frontmost, or deliberate multi-tab reading). Skip dwell-time background apps (Spotify pinned tab, YouTube audio continuing while doing other work).
3. **What was built/worked on** — main productive output, one clause.
4. **Gratification monkey line** — always say "gratification monkey", never just "monkey". Content names only, no platform names, narrative arc + "and more" tease.

**Focused attention vs dwell time:** Focused = heartbeats in that window while frontmost. Dwell = audio/app running in background while user is elsewhere. Dwell-time content belongs in monkey trail only, not in sentences 2–3.

**Good (approved canonical example):**
> "A building day. Read about AI making you dumber, then Karpathy's second brain on Substack. Built brainloop all morning. The gratification monkey went from Wagon R to Sathuranga Vettai and more."

**Bad — too zoomed out, loses the day's identity:**
> "A building day. Brainloop took shape, inbox got cleared. The monkey kept Sathuranga Vettai warm the whole time."

**Bad — task log, names apps not content:**
> "Opened the day with yesterday's report still on screen. Read Karpathy's second brain on Substack and got to inbox zero. Built brainloop with Claude Code all morning..."

**Always query the DB for the actual first activity before writing.** The first non-loginwindow heartbeat row tells you what was open when the screen woke up.

### Section label wording (exact)

`How your Brain attention moved — {N} switches`  ← capital B in Brain

### Theme and design tokens

Background: `#f3ede2` (cream). Text: `#221a12` (dark ink).

```css
--cream:       #f3ede2;
--ink:         #221a12;
--monkey:      oklch(62% 0.14 38);        /* warm terracotta */
--monkey-soft: oklch(62% 0.14 38 / 0.12);
--display:     "Instrument Serif", Georgia, serif;
--body:        "Inter Tight", "Inter", -apple-system, sans-serif;
--mono:        "JetBrains Mono", "SF Mono", ui-monospace, monospace;
```

Seismo chart fill colors (context-switch density):
- empty: `rgba(34,26,18, 0.06)`
- calm  (<10 switches): `rgba(34,26,18, 0.26)`
- busy  (10–22):        `rgba(34,26,18, 0.58)`
- chaotic (22+):        `rgba(34,26,18, 0.90)`

Default viz mode: `seismo` (set by `TWEAK_DEFAULTS` in the template).

### Period summary text typography

`.period-line1` and `.period-line2` use `var(--body)` (Inter Tight) — same as the rest of the body text. No italics.

### Hero phrase typography

`.hero-phrase` (renders `meta.dayInAPhrase`) uses the native template CSS — no override needed:

```css
.hero-phrase {
  font-family: var(--display);           /* Instrument Serif */
  font-size: clamp(22px, 2.8vw, 42px);  /* responsive */
  line-height: 1.12;
  letter-spacing: -0.02em;
  max-width: 60vw;
}
.hero-phrase em {
  font-style: italic;
  color: var(--monkey);                  /* terracotta — wraps the word "monkey" */
}
```

`max-width` is overridden to `60vw` via a `DOMContentLoaded` script in the template (the bundled JS injects its own stylesheet at runtime, so a post-mount script is needed to win specificity).

**No italics anywhere in period text.** The monkey sentence (`.monkey-sentence`) is `font-style: normal`.

### Monkey sentence rendering

The monkey block renders two lines:

1. **`.monkey-sentence`** — shows `monkey.story` if present; falls back to `monkeySentence()` if not. Always populate `monkey.story` — the auto-generated fallback lists all trail items and becomes unreadable at > 4 items.

2. **`.monkey-trail-summary`** — first 3 trail `what` titles joined by `, ` + `+ N more` count. Rendered in monospace terracotta at 11px.

### Monkey sentence logic (auto-generated fallback only)

The `monkeySentence()` function in the JSX groups `trail` entries by `where` platform and produces one sentence per period:

> "Watched Yennai Arindhal – Sathyadev scenes, Theeran – movie scenes on YouTube, Facebook Reels – Max Alexander on Facebook."

Fill `trail[].where` with the platform name (`"YouTube"`, `"Facebook"`, `"Instagram"`, etc.) so the sentence renders correctly. Items where `where === '-'` or `what === 'Call ended'` are filtered out.

**No em dashes in the monkey sentence** — platform groups are joined with `, ` (comma-space), not ` — `.
