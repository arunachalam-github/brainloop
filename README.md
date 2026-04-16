# Brainloop

A macOS background daemon that silently records your computer activity into a private local SQLite database. Ask Claude Code anything about your day — what you worked on, who you called, what you read.

All data stays on your Mac. Nothing is sent anywhere.

---

## What it captures

- Active app and window title (every switch)
- Browser URL and page content (what you're reading)
- Audio and microphone state (are you on a call?)
- Slack channels, Zoom meetings, Google Docs you're in
- App switches, focus changes, and a heartbeat every 60 seconds

---

## Requirements

- macOS 12 or later
- Python 3.10 or later — [download from python.org](https://www.python.org/downloads/) if needed
- [Claude Code](https://claude.ai/code) — for querying your activity in natural language

---

## Install

**1. Clone the repo**
```bash
git clone https://github.com/yourusername/brainloop
cd brainloop
```

**2. Run the installer (one command)**
```bash
bash install.sh install
```

This automatically:
- Finds your Python installation
- Installs required dependencies
- Configures and starts the background daemon

**3. Grant permissions** (see below — required for full capture)

---

## Permissions

Brainloop needs two permissions to capture your activity. Grant them once after installing.

### Accessibility (required)

Without this, brainloop cannot read window titles, URLs, or text content.

> **System Settings → Privacy & Security → Accessibility**
> Click the `+` button and add `python3`

### Allow JavaScript from Apple Events in Chrome (required for page content)

Without this, brainloop can see *which* URL you're on but not *what's on the page*.

> **In Chrome: View menu → Developer → Allow JavaScript from Apple Events**

This is what enables answers like "what was I reading at 3pm?" or "what was I watching on YouTube?"

### Screen Recording (if prompted)

macOS may ask for Screen Recording permission the first time brainloop reads content from certain apps. Approve when prompted.

---

## Ask questions with Claude Code

Open the brainloop folder in Claude Code and ask anything:

```bash
claude
```

Then just type naturally:

```
"Give me a summary of my day"
"What did I do in the last 30 minutes?"
"Was I on any calls today? Who with?"
"What was I reading around 3pm?"
"What was I surfing on YouTube today?"
"Am I on a call right now?"
"What did I work on most this morning?"
"What websites did I visit today?"
"When did I take a break?"
"What Slack channels was I active in?"
```

Claude reads `CLAUDE.md` on startup and knows exactly where your data is and how to query it.

---

## Managing the daemon

The daemon starts automatically on login and restarts if it crashes.

| Command | What it does |
|---|---|
| `bash install.sh install` | Install and start |
| `bash install.sh uninstall` | Stop and remove (preserves your data) |
| `bash install.sh restart` | Restart after changes |
| `bash install.sh status` | Check if running + recent log lines |
| `bash install.sh logs` | Stream live activity log |

---

## Privacy

- All data is stored locally at `~/Library/Application Support/brainloop/activity.db`
- Nothing is uploaded, synced, or sent anywhere
- To delete all captured data: `rm ~/Library/Application\ Support/brainloop/activity.db`
- To stop permanently: `bash install.sh uninstall`

---

## Troubleshooting

**Daemon not running**
```bash
bash install.sh status
```
Check the log output for errors.

**Window titles or URLs are missing**
Accessibility permission was not granted. Go to System Settings → Privacy & Security → Accessibility and add `python3`.

**Page content not captured (what I'm reading/watching)**
Chrome "Allow JavaScript from Apple Events" is not enabled. In Chrome: View → Developer → Allow JavaScript from Apple Events.

**`python3` not found during install**
Install Python 3.10+ from [python.org](https://www.python.org/downloads/) and re-run `bash install.sh install`.

---

## License

MIT — see [LICENSE](LICENSE)
