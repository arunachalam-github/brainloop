"""
brainloop.config
~~~~~~~~~~~~~~~~
Central configuration for paths, timers, and app lists.
All other modules import from here — change values in one place.
"""

from pathlib import Path

# ── Storage ───────────────────────────────────────────────────────────────────
DB_DIR   = Path.home() / "Library" / "Application Support" / "brainloop"
DB_PATH  = DB_DIR / "activity.db"
LOG_PATH = DB_DIR / "daemon.log"

# ── Capture tuning ────────────────────────────────────────────────────────────
HEARTBEAT_SECS = 60.0    # write a record even when nothing changes (reading/watching)
DEBOUNCE_SECS  = 1.5     # ignore AX value-change events within this window (typing)
MAX_TEXT_CHARS = 3000    # cap on visible_text column (tail slice — last 3000 chars)

# ── Feature flags ────────────────────────────────────────────────────────────
USE_APPLESCRIPT_PAGE_TEXT  = True   # Set False to disable AppleScript page capture
APPLESCRIPT_MAX_CHARS      = 10000   # Cap on page_text column (viewport content)
DETECT_AUDIO               = True   # Set False to disable CoreAudio listeners entirely

# ── Analyzer (LLM day-summary generator) ─────────────────────────────────────
ANALYZER_INTERVAL_SECS     = 1800   # 30 min — one LLM call per tick (if not gated out)
ANALYZER_FIRST_DELAY_SECS  = 60     # let capture warm up before the first analyzer run
ANALYZER_MANUAL_POLL_SECS  = 5      # poll app_config for UI-triggered manual refresh requests
ANALYZER_IDLE_SKIP_MINS    = 120    # skip tick if last activity row is older than this
ANALYZER_MIN_REGEN_SECS    = 1200   # ignore a tick if we ran within the last 20 min
ANALYZER_MAX_DWELLS        = 20     # cap on browser dwells sent to LLM per call
ANALYZER_PAGE_TEXT_CHARS   = 800    # per-dwell page_text slice budget (head 400 + tail 400)
ANALYZER_KEYCHAIN_SERVICE  = "com.brainloop.ai"  # `security -s` label for the API key

# ── Chat (SQL tool-use powered Q&A over activity_log) ────────────────────────
CHAT_POLL_SECS             = 2      # how often the daemon looks for pending user turns
CHAT_MAX_TOOL_CALLS        = 6      # SQL queries per user turn before we force an answer
CHAT_MAX_HISTORY_TURNS     = 10     # prior user/assistant pairs replayed into the LLM
CHAT_MAX_ROWS_PER_SQL      = 200    # cap rows returned from a single run_sql call
CHAT_SQL_TIMEOUT_SECS      = 4      # per-query execution budget (sqlite interrupt)

# ── Browser bundle IDs (for URL extraction via AX address bar) ────────────────
BROWSER_BUNDLES: frozenset[str] = frozenset({
    # Mainstream
    "com.google.Chrome",
    "com.apple.Safari",
    "org.mozilla.firefox",
    "company.thebrowser.Browser",        # Arc
    "com.brave.Browser",
    "com.microsoft.edgemac",
    # AI / alt browsers
    "ai.perplexity.comet",               # Comet (Perplexity)
    "com.duckduckgo.macos.browser",      # DuckDuckGo
    "com.operasoftware.Opera",           # Opera
    "com.vivaldi.Vivaldi",               # Vivaldi
    "org.chromium.Chromium",             # Chromium
    # Pre-release channels
    "com.google.Chrome.canary",
    "com.google.Chrome.beta",
    "com.google.Chrome.dev",
    "com.apple.SafariTechnologyPreview",
    "com.microsoft.edgemac.Beta",
    "com.microsoft.edgemac.Dev",
    "com.microsoft.edgemac.Canary",
    "org.mozilla.nightly",
    "org.mozilla.firefoxdeveloperedition",
})
