# Brainloop Desktop App — MVP UI Design

**Status:** draft, pending user review
**Date:** 2026-04-17
**Scope:** UI shell, 4 screens (Today / Week / Ask / Settings), first-run onboarding, data layer, Ask backend.
**Out of scope for this spec (separate specs to follow):** Swift rewrite of the capture daemon, code signing / notarization / Sparkle / DMG packaging.

---

## 1. Problem

Brainloop already captures fine-grained activity into a local SQLite DB via a Python daemon. The daemon has no UI. Users today query their data by opening Claude Code inside the repo. That works for power users but is not a distributable product.

We want to ship a macOS desktop app that lets a non-technical user:

1. Glance at how their day / week moved — visually, editorially — without reading SQL.
2. Ask natural-language questions of their own activity via any OpenAI-compatible LLM endpoint.
3. Manage capture + permissions + data without a terminal.

The design language is locked from the four existing HTML prototypes (`apps/*.html`): warm cream surface `#f5f0e8`, deep brown ink `#2a2218`, editorial serif (Newsreader), slowly rotating wireframe ovals as the signature visual. Calm. Meditative. No bright accents.

## 2. Scope

### In MVP (phase 1)
- Single-window dock app, SwiftUI, macOS 13+
- Top tab bar: Today · Week · Ask · Settings
- First-run onboarding screen (welcome + permission checklist)
- Today screen — oval timeline + stat pillars + moments feed
- Week screen — 7 day-rows, each rendering its own oval timeline
- Ask screen — OpenAI-compatible endpoint (user-provided baseURL, model, API key)
- Settings screen — capture toggle, permission status, Ask config, delete data, about

### Deferred (phase 2)
- Hour drill-in screen (click a bucket to zoom)
- Menu bar extra for ambient glance
- Persistent Ask history across launches
- LLM-generated editorial summaries (today's "a quiet morning" headline is deterministic in MVP)
- Full re-onboarding flow if a permission is later revoked (MVP shows a banner instead)
- Dark mode
- In-app data export

### Separate specs that follow this one
- **Swift daemon rewrite** — port `daemon/` from Python/pyobjc to Swift, keep the same event-driven architecture (NSWorkspace + AXObserver + CoreAudio + CFRunLoopTimer), same schema, same DB path.
- **Packaging & distribution** — Developer ID signing, hardened runtime, notarization, Sparkle integration, DMG build script.

## 3. Architecture & process model

One signed `.app` bundle ships two executables:

```
Brainloop.app/
├── Contents/
│   ├── MacOS/
│   │   ├── Brainloop            ← SwiftUI app, user-facing
│   │   └── BrainloopDaemon      ← background helper, no UI
│   ├── Library/LaunchAgents/
│   │   └── com.brainloop.daemon.plist
│   └── Info.plist

Shared data (app group):
~/Library/Group Containers/group.com.brainloop.shared/
  └── activity.db
```

**Why two processes, one bundle.** Capture must run while the UI is closed. UI must not block on Accessibility API work. Shipping both under one Developer ID identity means granting Accessibility once covers both — TCC keys permissions on the code signature.

**App group container** replaces `~/Library/Application Support/brainloop/` from today's layout. App groups are Apple's sanctioned mechanism for shared file access between signed binaries of the same app; they sidestep the `EDEADLK` / Documents-TCC issues we hit during Python daemon development.

**IPC:** none. Both processes read/write the SQLite file. Daemon runs in WAL mode (already does); UI opens a read-only connection. No XPC service, no sockets, no Darwin notifications.

**Helper registration.** On finish of onboarding, the UI calls:

```swift
try SMAppService.agent(plistName: "com.brainloop.daemon.plist").register()
```

This triggers the system *"Brainloop wants to add a login item"* prompt. Once approved, `launchd` owns the daemon's lifecycle — runs at login, respawns on crash, stops on `.unregister()` or app uninstall.

`SMAppService.agent` (not `.daemon`) is intentional: capturing user activity runs as the user, doesn't need root, is simpler to manage.

**First-run sequence:**
1. User opens the `.app`.
2. `UserDefaults.hasCompletedOnboarding` is false → OnboardingView.
3. User grants Accessibility + Screen Recording via the checklist (live-polled).
4. On `→ begin capturing`, UI calls `register()`.
5. macOS prompt → user approves → daemon boots → first row lands in `activity.db` within ~1 second.
6. UI writes `hasCompletedOnboarding = true`, fades to Today.

## 4. Screens

All four screens share the same shell chrome: macOS traffic-light titlebar, 52px-tall tab strip below it with four text links in small-caps serif letter-spaced (`TODAY   WEEK   ASK   SETTINGS`), hairline underline on the active tab, 32px horizontal content padding. Default window size 1280×800, min 900×600, resizable. Light mode only.

### 4.1 Today (default landing)

**Purpose:** end-of-day glance. "How did my mind move today?"

**Layout top-to-bottom:**
1. **Editorial headline** — 48px serif italic, deterministic template-based (e.g. *"a quiet morning"*, *"a scattered afternoon"*). Template inputs: dominant activity state + time-of-day noun.
2. **Subtitle** — 14px IBM Plex Sans, muted: `thursday, april 17 · 189 switches · 3h focus`.
3. **Oval timeline** — visual direct port of `apps/timed_oval_design.html`. 28 buckets spanning 05:30 → now by default; 10-minute buckets. Each bucket is a rounded rectangle whose opacity encodes activity state (empty 7%, calm 30%, busy 60%, chaotic 88%). Inside each, 0-8 wireframe ovals rotating slowly; rotation speed scales with switch count.
4. **Stat pillars** — three columns, no box chrome:
   - `switches / 189`
   - `deepest focus / 47m in Code`
   - `most visited / github.com`
5. **Moments feed** — scrollable list of 5-15 rows. Left column: mono timestamp. Right column: italic-serif sentence describing the event. Sourced from app_switch events where dwell > 5 minutes.

**Interactions (MVP):** hover on a bucket → tooltip with count + top apps (reuse the existing tooltip design). Click-to-drill is phase 2.

**State:** `TodayViewModel { date, buckets: [Bucket], stats: Stats, moments: [Moment] }`. Pure function from `activity_log` rows in the day's time window. Recomputed on window focus, tab switch back to Today, or the 30-second poll.

### 4.2 Week

**Purpose:** 7-day macro view. "What did my past week look like?"

**Layout:**
- Heading: *"the past week"*, subtitle `april 11 – 17`.
- Seven rows stacked vertically, hairline divider between, 24px spacing:
  - Left 120px: date label, italic serif — *"monday, 14 april"*.
  - Center flex: that day's 28-bucket oval timeline (same renderer as Today, reduced row height).
  - Right 200px: one-line descriptive summary, muted italic serif — *"calm start, busy afternoon"*. Template-generated, same grammar family as Today's headline.
- Hover on a row tints it 4%. Click-to-drill is phase 2.

**State:** fires 7 per-day aggregate queries in parallel (`TaskGroup`) on tab mount. No live refresh — the past is stable.

### 4.3 Ask

**Purpose:** natural-language query over activity.

**Empty state (no API configured):** centered italic serif *"configure an openai-compatible endpoint in settings to ask brainloop about your day"* with a hairline-underlined `→ open settings` link.

**Configured state:**
- Centered single column, max 720px.
- Header: small italic serif *"ask brainloop"*.
- Input: single underlined line (no boxed input), placeholder *"what was i reading around 3pm?"*. Enter submits. `⌘↑ / ⌘↓` cycles through last 20 questions (in-memory only; resets on app quit in MVP).
- Three example questions below the input as muted italic serif links, separated by dots: *"who was my last call with · how long did i spend on github today · what did i work on most this morning"*. Clicking populates the input and submits.
- **Thinking state:** a single oval glyph (our motif) rotates slowly centered below the input.
- **Streaming state:** answer tokens appear one-by-one in italic serif, no typing sound, no animation per token. Cursor block blinks at the end until the stream closes.
- **Sources:** below the answer, a vertical stack of up to 12 citation cards. Each card: mono timestamp + app name + window title, hairline border, no icons. Expand to "show all N" if more.
- **Errors** (from the matrix in 7.8): single italic line under the input. No modals, no red.

**State:** `AskViewModel { input, history: [Question], current: Conversation? }` where `Conversation = { question, toolCalls, streamingAnswer, collectedRows, status }`.

### 4.4 Settings

Single scrollable column, no sub-tabs.

1. **Capture**
   - Toggle: `capture activity`. On flip, registers/unregisters the `SMAppService.agent`.
   - Live status line: `running since 9:12 am · 12,340 records today`.
2. **Permissions** — three auto-detected rows, same design as onboarding checklist:
   - Accessibility — status + `open settings` link
   - Screen Recording — same
   - Chrome page text (optional) — coach-mark linking to View → Developer → Allow JS
3. **Ask**
   - `base URL` — text input, placeholder `https://api.openai.com/v1`
   - `model` — text input, placeholder `gpt-4o-mini`
   - `API key` — secure input, stored in Keychain (service `com.brainloop.ask`). Never in UserDefaults.
   - Italic note: *"your activity data is sent to this endpoint when you ask a question. choose a provider you trust."*
   - `test connection` button — minimal `/chat/completions` call with `max_tokens: 1`. Shows ● reachable / *"rejected the key"* / *"couldn't reach"*.
4. **Data**
   - `delete all captured data` — confirm dialog → `DELETE FROM activity_log`. Keeps file + schema.
   - `reveal in Finder` — opens the app group container.
5. **About**
   - Version, *check for updates* (Sparkle, deferred to the packaging spec), privacy link, one-line *"all data stays on your mac unless you configure ask."*

## 5. Onboarding

### 5.1 Entry
Root view inspects `UserDefaults.hasCompletedOnboarding`. False → `OnboardingView`. True → `MainView` (tabs).

### 5.2 Welcome screen
Centered 600px column, ~35% top margin:
- *"hello."* in 48px serif italic.
- One 18px paragraph in muted ink describing what brainloop does and that all data is local.
- `→ begin` hairline-underlined text link, bottom-center.

### 5.3 Permission checklist screen
- Header: `A FEW GRANTS` in small-caps serif letter-spaced.
- Subtitle: italic serif explaining macOS requires these.
- Three rows with hairline dividers:

| Row | Label + italic subtext | State | Action |
|---|---|---|---|
| 1 | `accessibility` — *"so brainloop can read window titles and urls"* | ● / ○ | `open settings` |
| 2 | `screen recording` — *"for reading titles from protected apps"* | ● / ○ | `open settings` |
| 3 | `chrome page text` — *"optional — lets brainloop see what you're reading"* | ● / ○ / — | `open chrome` + coach-mark |

- Live ticker: *"1 of 2 required granted"*.
- `→ begin capturing` hairline-underlined. Disabled (50% opacity, no pointer) until both required are granted; optional Chrome row never blocks.

**Deep-links:**
- Accessibility: `x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`
- Screen Recording: `x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture`
- Chrome: opens the app; in-window tooltip explains the View → Developer → Allow-JS path (macOS cannot deep-link into Chrome's menus).

**Live refresh:** while on this screen, poll `AXIsProcessTrusted()` and `CGPreflightScreenCaptureAccess()` every 1 second. Row dot fills via 400ms ease-out when a grant flips. No sound, no confetti.

### 5.4 Finish
On `→ begin capturing`:
1. `SMAppService.agent(plistName:).register()`.
2. macOS prompts *"add login item"*.
3. Approve → set `hasCompletedOnboarding = true`, fade to Today.
4. Deny → stay on checklist, italic line *"brainloop can't run without a background helper. try again when you're ready."*, re-enable the button.

### 5.5 Automation permission
Not in the checklist. macOS prompts lazily the first time the daemon runs AppleScript against Chrome. Rely on that native prompt.

## 6. Data layer

### 6.1 Access pattern

```swift
// UI — read-only, per-view connection, thread-confined
let db = try Connection(
  dbPath,
  openFlags: [.readonly, .noMutex, .sharedCache]
)
```

Read-only flag is a hard safety rail: even if the Ask SQL guard (section 7.5) is bypassed, the connection itself cannot write.

### 6.2 Freshness
Pull, not push. Refresh triggers:
1. `NSApplication.willBecomeActive` (window gains focus)
2. Active tab changes
3. 30-second timer while window visible (cancelled when backgrounded)

No `sqlite3_update_hook` (doesn't fire cross-process), no file watchers, no Darwin notifications. Matches the calm tone.

### 6.3 Queries
Each view owns a small function in a `DataStore` actor:

| Screen | Query | Typical rows |
|---|---|---|
| Today | `WHERE ts >= start_of_day` → Swift aggregation | 3k–8k |
| Week | 7 parallel day-aggregate queries | ~30k total |
| Ask | dynamic SELECT via LLM tool, capped at 200 rows / 30k chars | variable |
| Settings status | single `COUNT(*) WHERE ts >= start_of_day` | 1 |

At ~100k rows (a month of heavy use), aggregations stay sub-100ms with the existing `idx_ts`. No caching layer in MVP.

### 6.4 Migrations
Daemon owns schema. On startup, runs a sequence of `ALTER TABLE ... ADD COLUMN` statements each wrapped in try/catch — same pattern as today's `daemon/db.py:58-71`. UI never migrates; reads only columns it knows, ignores unknown ones.

### 6.5 Backup
Time Machine picks up the file. No in-app export in MVP. `reveal in Finder` in Settings exposes the path for user-driven backup.

## 7. Ask backend

### 7.1 Config model
```swift
struct AskConfig {
  let baseURL: URL          // persisted: UserDefaults
  let model: String         // persisted: UserDefaults
  let apiKeyKeychainRef: Ref // persisted: Keychain, service = com.brainloop.ask
}
```

### 7.2 Test-connection semantics
Button in Settings → Ask. Sends a minimal `/chat/completions` with `max_tokens: 1`. Three outcomes shown inline next to the button: ● *reachable* / *"endpoint rejected the key"* / *"couldn't reach the endpoint"*.

### 7.3 Request shape
```http
POST {baseURL}/chat/completions
Authorization: Bearer {apiKey}
Content-Type: application/json

{ "model": "{model}",
  "stream": true,
  "temperature": 0.3,
  "messages": [
    { "role": "system", "content": "<see 7.4>" },
    { "role": "user",   "content": "<user question>" }
  ],
  "tools": [ <run_sql, see 7.5> ],
  "tool_choice": "auto" }
```

### 7.4 System prompt
```
You are Brainloop's reflective assistant. The user has recorded their own
computer activity into a local SQLite table `activity_log`. Use the `run_sql`
tool to answer their question with real data. Be gentle and honest — journal
tone, short sentences, no metrics-speak. Reference specific moments when
relevant. If the data doesn't support an answer, say so plainly.

SCHEMA: <the same column list shipped in CLAUDE.md>

Rules:
- Only SELECT statements. Never INSERT/UPDATE/DELETE/ALTER/DROP.
- Filter by `ts` (unix seconds) for time ranges; prefer BETWEEN.
- For "today": ts >= strftime('%s', date('now','localtime')).
- Keep queries small, <= 200 rows per call.
- Call run_sql as many times as needed before answering.
```

### 7.5 Tool definition & safety

```json
{ "type": "function",
  "function": {
    "name": "run_sql",
    "description": "Run a read-only SELECT on activity_log. Max 200 rows, 30000 chars per response.",
    "parameters": {
      "type": "object",
      "properties": { "query": { "type": "string" } },
      "required": ["query"]
    } } }
```

Three safety layers:
1. **Static check (regex):** reject if the first non-comment/whitespace keyword isn't `SELECT` or `WITH`.
2. **sqlite3 authorizer callback:** allow only `SQLITE_SELECT`, `SQLITE_READ`, `SQLITE_FUNCTION`, `SQLITE_RECURSIVE`. Any write attempt returns `SQLITE_DENY`.
3. **Read-only connection** (section 6.1).

Tool result JSON: `{ "rows": [...], "row_count": N, "truncated": bool }`. Cap 200 rows or 30k chars (whichever first); when truncated, the flag propagates so the LLM can caveat.

### 7.6 Streaming & multi-turn
- URLSession SSE reader → `AsyncThrowingStream<Frame>`. Each `delta.content` appends to the answer. Each `delta.tool_calls` accumulates a tool call; once complete, run it locally, append the `role:"tool"` message with the result, open a fresh completion with the updated history.
- Loop cap: **6 tool calls per question.** At 6, inject *"you've called run_sql 6 times, please answer with what you have."* and require the model to finalize.

### 7.7 Sources
Every row returned by any `run_sql` call lands in `collectedRows: Set<Row>`. After the answer completes, render up to 12 as citation cards. Not "best" rows — *"rows it actually read"* is more honest and simpler.

### 7.8 Error matrix

| Cause | User-facing copy |
|---|---|
| No config | *"configure an openai-compatible endpoint in settings to ask questions about your day"* |
| 401 / 403 | *"the endpoint didn't accept the key. check it in settings."* |
| Network / DNS / timeout | *"couldn't reach the endpoint. you're offline or the url is wrong."* |
| SQL rejected | *"the answer wanted to modify data. try rephrasing."* |
| > 6 tool calls | *"took too many lookups to answer. try a more specific question."* |
| Unexpected | *"something went wrong. try again, or check the model name."* |

All rendered as italic serif under the input. No red, no icons, no modal dialogs. MVP does not auto-retry on network errors — user retries manually.

## 8. Testing strategy

- **Unit:** pure functions — bucket aggregation, state classifier (`empty/calm/busy/chaotic`), headline/summary templates, SQL safety guard (static check + authorizer), SSE parser. Tests sit in a `BrainloopTests` Xcode target.
- **Integration:** DataStore against a temp-directory SQLite file populated with fixture rows. Each screen's query path has a test that asserts computed `TodayViewModel`, `WeekViewModel`, etc. match expectations.
- **Ask end-to-end:** tests run against a stub OpenAI-compatible HTTP server (small Swift server in the test target) to verify request shape, SSE decoding, tool-call loop, cap behavior, and the full error matrix.
- **UI:** SwiftUI previews for every major screen state (empty / loaded / error / streaming). No XCUITest in MVP — the brittleness cost isn't worth it for 4 screens.
- **Manual QA checklist** committed to the repo, walks through: fresh install → onboarding → grant each permission → daemon starts → row appears in Today → configure Ask → ask a question → delete data → re-grant revoked permission.

## 9. Open questions / risks

- **`SMAppService` + DMG distribution**. Apps moved from DMG to /Applications sometimes trip Gatekeeper translocation, which interferes with helper registration on first launch. Mitigation: the onboarding's `→ begin capturing` step handles the register-failure path by asking the user to move the app to /Applications first. To be verified during packaging.
- **Accessibility grant prompt on first run**. macOS typically needs the app to actually call an AX API before it shows up in the list. Onboarding calls `AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": true])` on the checklist screen's first appearance to prime the list.
- **Chrome AppleScript prompt** is lazy and shown the first time. If user denies, Ask over `page_text` questions degrades silently (the column stays NULL). We'll need a small note somewhere — phase 2 polish.
- **Large result sets in Ask**. Queries like *"what did I read this month?"* could return megabytes of `page_text`. The 30k-char cap protects the context window; the LLM will need to iterate with narrower queries. Watch for user frustration in testing.

## 10. What "done" looks like

- `make build` produces a `Brainloop.app` bundle with both binaries.
- Fresh install on a clean macOS user → onboarding completes → daemon begins capturing → Today renders live data within 60 seconds.
- All four screens render against real data without errors, and permission revocation shows the banner gracefully.
- Ask works against both `https://api.openai.com/v1` (gpt-4o-mini) and a local `http://localhost:11434/v1` (Ollama) with the same code path, no provider-specific branches.
- Settings → delete data → Today empties → Settings status reflects zero records.
- The manual QA checklist passes end to end.
