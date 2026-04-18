# Brainloop

It's Monday. 9:00 AM. You had a plan.
Write a report.

Four minutes later, you're reading about the geopolitical conflict of a country you'll never visit.
It started with a notification. Or maybe a headline. Or maybe you just needed to "quickly check one thing" before you began.

The exact trigger doesn't matter, because the result is always the same: one click became three, three became a search, then seventeen tabs, and now you're 20 minutes deep into something that has absolutely nothing to do with your morning, your report, or your job.

Your brain?
Somewhere else entirely.

The human brain is wired to wonder — which means it's naturally built to be curious, explore, ask questions. But when it's time to focus, that same wandering becomes your biggest distraction.

You know what you did today.
But do you know…

## How your mind actually worked while doing it?

Brainloop is a macOS background daemon + desktop app that silently records your computer activity into a private local SQLite database, then asks an LLM of your choice to write you a calm editorial summary of the day — every 30 minutes, as you work.

All data stays on your Mac. Nothing is uploaded unless you point it at a provider yourself.

---

## What you see

- **Today** — a generated headline, a seismograph of context switches across the day, the day in acts, a Gratification Monkey call-out when you doom-scrolled, plus widgets (longest focus, hours by app, calls, waiting-on-AI).
- **Chat** — a quiet place to ask your laptop what it saw (stub in v1).
- **Settings** — AI provider (Anthropic / OpenAI / Gemini), API key, permissions, daemon status.

## What it captures

- Active app and window title (every switch)
- Browser URL and page body content (what you're reading)
- Audio + microphone state (are you on a call?)
- Slack channels, Zoom meetings, docs you're in
- Heartbeat every 60 seconds

---

## Requirements

- macOS 13+ (tested on Sequoia)
- Homebrew (`/opt/homebrew/bin/brew` on Apple Silicon)
- Python 3.13 (Python 3.14 has a venv/launchd deadlock bug — don't use it)
- Rust toolchain (`rustup` — https://rustup.rs)
- Xcode.app (full Xcode, not just Command Line Tools — required for `cargo tauri dev`)
- An API key for one of Gemini / Anthropic / OpenAI

Install the toolchain prerequisites:

```bash
brew install python@3.13 sqlite
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
cargo install tauri-cli --version '^2' --locked
```

Then accept the Xcode license (one-time):

```bash
sudo xcode-select -s /Applications/Xcode.app/Contents/Developer
sudo xcodebuild -license accept
```

---

## Quick start

```bash
# 1. Clone + install the background daemon
git clone https://github.com/arunachalam-github/brainloop
cd brainloop
make install
```

`make install` creates a Python venv at `~/Library/Application Support/brainloop/venv`, syncs the `daemon/` source there (Sequoia's TCC won't let launchd read files in `~/Documents`), and loads the LaunchAgent. The daemon starts capturing immediately, and a second timer inside it runs the LLM analyzer every 30 minutes.

```bash
# 2. Grant Accessibility permission (required for window titles + URLs)
#    System Settings → Privacy & Security → Accessibility → + → add the venv python
#    make install prints the exact path you need to add.
```

Optional but recommended — Chrome's "Allow JavaScript from Apple Events" lets Brainloop read the page body text, not just the URL:

```
Chrome → View → Developer → Allow JavaScript from Apple Events
```

```bash
# 3. Configure your LLM provider. Pick one:
make config-gemini      # default, cheapest, works great
# or
make config-anthropic
# or
make config-openai
```

The `config-*` target only writes the provider name + model + base URL to the shared `app_config` table. Your key is **never** stored there — put it in macOS Keychain so nothing logs it:

```bash
security add-generic-password -s com.brainloop.ai -a gemini -w
# (prompts for the key — paste it and hit Enter)
```

```bash
# 4. Force the first analyzer run (normally it waits ~60s after daemon start, but
#    this bypasses the regen gate so you don't have to wait 20 min between tries).
make analyze-now

# 5. Confirm the row landed
make show-summary
```

You should see something like:

```
  date: 2026-04-18
  generated_at: 2026-04-18 15:46:30
  model: gemini-2.5-flash
  activity_rows: 14765
  tokens_in/out: 15453 / 5886
  headline: A morning of anime and YouTube, the monkey took the early hours...
```

```bash
# 6. Launch the desktop app
make ui
```

First launch compiles the Tauri/Rust tree (~2 minutes). After that the window opens, Today hydrates with your real day, the waveform starts flowing, hover tooltips show the apps you used in each 10-minute bucket. Hot reload picks up JS/CSS changes instantly.

---

## Day-to-day commands

| Command | What it does |
|---|---|
| `make ui` | Run the desktop app (dev mode, hot reload) |
| `make analyze-now` | Regenerate today's summary right now |
| `make show-summary` | Print today's headline + metadata |
| `make status` | Is the daemon running? Show last 20 log lines |
| `make logs` | Stream the daemon log |
| `make reinstall` | Sync changes in `daemon/` and restart the agent |
| `make restart` | Just restart without re-syncing |
| `make uninstall` | Stop the agent (preserves DB) |
| `make help` | Show every target |

---

## Cost

At the default `gemini-2.5-flash` cadence (30-min ticks during your active hours):

- ~15k input tokens + ~6k output tokens per tick
- ~32 ticks/day
- **~$0.10 – $0.30/day**, depending on how many ticks actually run (idle-gate + no-new-rows gate skip many).

Anthropic (Claude Sonnet) is ~10× more expensive. OpenAI GPT-4o-mini is close to Gemini. The daemon logs `tokens=in→out` on every successful tick so you can audit.

---

## Ask questions with Claude Code

Separate from the built-in Chat tab, you can open the repo in Claude Code and ask your data anything via SQL — `CLAUDE.md` teaches it the schema on startup:

```bash
claude
```

Then:

```
"Give me a summary of my day"
"What did I do in the last 30 minutes?"
"Was I on any calls today? Who with?"
"What was I reading around 3pm?"
"When did I take a break?"
```

---

## Privacy

- All capture data lives in `~/Library/Application Support/brainloop/activity.db` (SQLite, WAL mode)
- API keys live in macOS Keychain under service `com.brainloop.ai` — never in the DB, never in the repo
- The analyzer sends aggregated activity (no raw `page_text` larger than 800 chars per dwell) **only** to the endpoint you configured
- To delete all captured data: `rm ~/Library/Application\ Support/brainloop/activity.db` (the daemon recreates the schema on next start)
- To stop permanently: `make uninstall && make clean-all`

---

## Troubleshooting

**`make install` succeeds but the daemon isn't writing rows**
Check `make status`. If the log says *"Accessibility NOT granted"*, the System Settings permission step was skipped — add the venv python the install output pointed to.

**`make analyze-now` says "api key not in Keychain"**
You ran `make config-*` but didn't add the actual key. Run `security add-generic-password -s com.brainloop.ai -a <provider> -w` and paste it.

**`make analyze-now` says "ai config not set in app_config"**
Run one of the `make config-*` targets first.

**`cargo tauri dev` says tool 'xcodebuild' requires Xcode**
You have Command Line Tools only. Install Xcode.app from the App Store, then `sudo xcode-select -s /Applications/Xcode.app/Contents/Developer` and `sudo xcodebuild -license accept`.

**UI shows "Brainloop is still listening" forever**
`today_summary()` is returning null — the analyzer hasn't written a row for today yet. Run `make analyze-now` and switch tabs to re-fetch.

**Analyzer errors with "invalid JSON in content" or truncated output**
Your model ran out of output budget before finishing the JSON. The default `gemini-2.5-flash` + 16k max-tokens + `reasoning_effort: low` handles this, but larger reasoning traces on other models can still trip it. Switch to a non-thinking model (`gemini-2.0-flash`, `gpt-4o-mini`) via `make config-*` + edit the model in the DB directly if needed.

**Daemon crashes on launchd start with EDEADLK**
You modified `daemon/` but didn't `make sync` / `make reinstall`. Or your source lives in `~/Documents` and Sequoia TCC is blocking import. `make reinstall` copies to Application Support which isn't gated.

---

## License

MIT — see [LICENSE](LICENSE)
