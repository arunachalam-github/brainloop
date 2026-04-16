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

# ── Browser bundle IDs (for URL extraction via AX address bar) ────────────────
BROWSER_BUNDLES: frozenset[str] = frozenset({
    "com.google.Chrome",
    "com.apple.Safari",
    "org.mozilla.firefox",
    "company.thebrowser.Browser",   # Arc
    "com.brave.Browser",
    "com.microsoft.edgemac",
})
