# Brainloop
Brainloop tracks how your attention moves across apps and time.
At the end of the day, it reflects your day back as a story - not data, not a productivity score.

## Why Brainloop? 
It's Monday. 9:00 AM. You had a plan.<br/>
Four minutes later, you're reading about a geopolitical conflict in a country you'll never visit. 

You know how it happened. A notification. A headline. A "quick check." <br/>
One click led to another, and now the morning is gone.

Your brain? Somewhere else entirely.<br/>
Here's the thing: your brain isn't broken. It did exactly what brains do.

### The Two Characters Inside Your Brain

Psychologists call it the **Monkey Mind**: restless, wandering part of your brain that jumps from one thought to another, never settling. Tim Urban called it the [Instant Gratification Monkey](https://www.youtube.com/watch?v=arj7oStGLkU). 

The other character is rarer. **Flow**: total absorption, the zone, the state where hours pass like minutes and you actually make the thing you set out to make.

Both live in you. They take turns running your day. <br/>
**Until now, you had no idea which one was winning.**

---
# What Brainloop Is
Brainloop is a macOS app that tracks how your attention shifts across apps, windows, hours and reflects it back to you as a story. Not as data. Not as a productivity score. As a story.

> A building day. Started by reading about AI making us dumber, then drifted into Karpathy’s second brain on Substack. Spent the morning building Brainloop with Claude. The gratification monkey began with reels on politics and movie BGMs, and by evening, it had evolved into shopping for fridge magnets and armchairs.

That's your day. Honest. Unfiltered. In plain English.<br/>
You've heard of building a **Second Brain**.<br/> 
**Brainloop shows you what the first one was actually doing**.
<br/>

|Feature|Description|
|--------|------------|
|**Day in Three Acts**|Your day broken into natural chapters, not hours. Morning momentum. Afternoon drift. Evening escape|
|**Context Switches**|Number of times your attention shifted, visualized as a seismograph-style chart|
|**The Gratification Monkey**|Brainloop spots when the monkey took over: what triggered it, how long it lasted, where it took you.|
|**Longest Focus**|Your best block of uninterrupted work. The one thing you can build on tomorrow.|
|**Doom-Scroll**|Accumulated time on passive, drift-mode content. No judgment. Just the mirror.|
**Things You Read**|Every article, post, or page you actually spent time with. Timestamped.|
**The Narrative Summary**|One paragraph. Your whole day. Written by the machine that watched it.|

Attached Image at the bottom. 

## Requirements

- macOS 12 or later
- Python 3.10 or later — [download from python.org](https://www.python.org/downloads/) if needed
- [Claude Code](https://claude.ai/code) — for querying your activity in natural language and generating the report.


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
**3. Grant permissions** (see below — required for full capture)

## Permissions

Brainloop needs two permissions to capture your activity. Grant them once after installing.

```
1. Accessibility (required)
Without this, brainloop cannot read window titles, URLs, or text content.

→ In MAC: Go to → System Settings → Privacy & Security → Accessibility
→ Click the `+` button and add `python3`
```

```
2. Allow JavaScript from Apple Events in Chrome (required for page content)
Without this, brainloop can see *which* URL you're on but not what's on the page.
This is what enables answers like "what was I reading at 3pm?"

→ In Chrome: Go to → View menu → Developer → Allow JavaScript from Apple Events
```
---

## Ask questions with Claude Code

Open the brainloop folder in Claude Code and ask anything:

```bash
claude
```

Then just type naturally:

```
"Prepare the day report"
"Give me a summary of my day"
"What did I do in the last 30 minutes?"
"What was I reading today"
"What was I surfing on YouTube today?"
"What did I work on most this morning?"
"What websites did I visit today?"
"When did I take a break?"
"What Slack channels was I active in?"
```

Claude reads `CLAUDE.md` on startup and knows exactly where your data is and how to query it.

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

## How it Works?

Brainloop is a macOS background daemon that silently records your computer activity into a local SQLite database. All data stays on your Mac. Nothing is sent anywhere.
What it captures

- Active app and window title (every switch)
- Browser URL and page content (what you're reading) 
- Audio and microphone state (are you on a call?)
- Slack channels, Zoom meetings, Google Docs you're in
- App switches, focus changes, and a heartbeat every 60 seconds

## Privacy

- All data is stored locally at `~/Library/Application Support/brainloop/activity.db`
- Nothing is uploaded, synced, or sent anywhere
- To delete all captured data: `rm ~/Library/Application\ Support/brainloop/activity.db`
- To stop permanently: `bash install.sh uninstall`
- Passwords are never read or captured.
- Keystores are not captured
- No Screen Recording 

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
