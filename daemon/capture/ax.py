"""
brainloop.capture.ax
~~~~~~~~~~~~~~~~~~~~
Low-level macOS Accessibility (AX) and workspace helpers.

All functions are pure read operations; no state is stored here.
"""

import logging
import subprocess
import ApplicationServices as AS
import AppKit
import Quartz

from ..config import BROWSER_BUNDLES, MAX_TEXT_CHARS

log = logging.getLogger("brainloop.capture.ax")


# ── Active application ────────────────────────────────────────────────────────

def get_active_app() -> tuple[str | None, str | None, int | None]:
    """Return (app_name, bundle_id, pid) of the frontmost application."""
    ws  = AppKit.NSWorkspace.sharedWorkspace()
    app = ws.frontmostApplication()
    if not app:
        return None, None, None
    return (
        str(app.localizedName()),
        str(app.bundleIdentifier()),
        int(app.processIdentifier()),
    )


# ── Window title ──────────────────────────────────────────────────────────────

def get_window_title(pid: int | None) -> str | None:
    """Return the title of the focused window for the given process."""
    if pid is None:
        return None

    if AS.AXIsProcessTrusted():
        app_elem = AS.AXUIElementCreateApplication(pid)
        err, win = AS.AXUIElementCopyAttributeValue(
            app_elem, AS.kAXFocusedWindowAttribute, None
        )
        if err == AS.kAXErrorSuccess and win:
            err2, title = AS.AXUIElementCopyAttributeValue(
                win, AS.kAXTitleAttribute, None
            )
            if err2 == AS.kAXErrorSuccess and title:
                return str(title)

    # Fallback: CGWindowList (no AX permission required)
    wl = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements,
        Quartz.kCGNullWindowID,
    )
    if wl:
        for w in wl:
            if w.get("kCGWindowOwnerPID") == pid and w.get("kCGWindowLayer") == 0:
                name = w.get("kCGWindowName")
                if name:
                    return str(name)
    return None


# ── Browser URL ───────────────────────────────────────────────────────────────

def get_browser_url(pid: int, bundle_id: str) -> str | None:
    """Return the URL from the browser address bar, or None if not a browser.

    Strategy: AX tree walk first (free, ~instant) for browsers whose address
    bar is a real AXTextField (Chrome, Safari, Arc, Edge…). For Chromium-derived
    browsers that ship a custom React/Electron URL bar (Comet is the canonical
    case — its address bar isn't an AXTextField at all), fall back to the
    Chrome AppleScript dictionary, which Comet inherits verbatim. Costs ~50ms
    per browser heartbeat but only when the AX walk has already failed.
    """
    if bundle_id not in BROWSER_BUNDLES:
        return None
    # AX walk needs AX trust — try it first when we have it (free, instant
    # for Chrome/Safari/Arc/Edge whose address bar IS an AXTextField).
    if AS.AXIsProcessTrusted():
        app_elem = AS.AXUIElementCreateApplication(pid)
        err, win = AS.AXUIElementCopyAttributeValue(
            app_elem, AS.kAXFocusedWindowAttribute, None
        )
        if err == AS.kAXErrorSuccess and win:
            url = _find_url(win, depth=0)
            if url:
                return url
    # AppleScript fallback does NOT need AX — only the per-app "Allow
    # JavaScript from Apple Events" permission, which the user almost
    # certainly already enabled to make page_text work. So Comet (and any
    # other Chromium fork without an AXTextField address bar) gets a URL
    # even when brainloopd has zero TCC grants.
    return _applescript_url(bundle_id)


def _applescript_url(bundle_id: str) -> str | None:
    """Fetch the front tab's URL via the Chrome AppleScript dictionary.

    Only browsers in `_APPLESCRIPT_APP_NAMES` implement the Chrome `URL of
    active tab of front window` accessor. Returns None for everything else
    (Firefox, Opera, DuckDuckGo) or when osascript fails.
    """
    app_name = _APPLESCRIPT_APP_NAMES.get(bundle_id)
    if not app_name:
        return None
    script = (
        f'tell application "{app_name}" to '
        f'get URL of active tab of front window'
    )
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=2,
        )
        url = result.stdout.strip()
        # Match the AX walker's filter: only real web URLs are useful for
        # platform classification. chrome://newtab/, about:blank, file:// etc.
        # carry no signal for the day report.
        if url.startswith(("http://", "https://", "www.")):
            return url
        return None
    except Exception:
        return None


def _find_url(element, depth: int) -> str | None:
    """Recursively walk the AX tree looking for a URL-valued text field."""
    if depth > 6:
        return None

    err, role = AS.AXUIElementCopyAttributeValue(element, AS.kAXRoleAttribute, None)
    if err == AS.kAXErrorSuccess and role in ("AXTextField", "AXComboBox"):
        err2, val = AS.AXUIElementCopyAttributeValue(element, AS.kAXValueAttribute, None)
        if err2 == AS.kAXErrorSuccess and val:
            v = str(val)
            if v.startswith(("http://", "https://", "www.")):
                return v

    err3, children = AS.AXUIElementCopyAttributeValue(
        element, AS.kAXChildrenAttribute, None
    )
    if err3 != AS.kAXErrorSuccess or not children:
        return None
    for child in children:
        result = _find_url(child, depth + 1)
        if result:
            return result
    return None


# ── Browser page text (AppleScript JS injection) ──────────────────────────────

# AppleScript-compatible app names for each supported browser bundle ID.
# Only browsers that implement Chrome/Safari-style `execute javascript` are
# listed here. Firefox (and its forks), Opera, and DuckDuckGo are intentionally
# omitted — they capture URLs but not page_text.
_APPLESCRIPT_APP_NAMES: dict[str, str] = {
    # Mainstream
    "com.google.Chrome":                   "Google Chrome",
    "com.apple.Safari":                    "Safari",
    "com.brave.Browser":                   "Brave Browser",
    "com.microsoft.edgemac":               "Microsoft Edge",
    "company.thebrowser.Browser":          "Arc",
    # AI / alt Chromium forks
    "ai.perplexity.comet":                 "Comet",
    "com.vivaldi.Vivaldi":                 "Vivaldi",
    "org.chromium.Chromium":               "Chromium",
    # Pre-release channels
    "com.google.Chrome.canary":            "Google Chrome Canary",
    "com.google.Chrome.beta":              "Google Chrome Beta",
    "com.google.Chrome.dev":               "Google Chrome Dev",
    "com.apple.SafariTechnologyPreview":   "Safari Technology Preview",
    "com.microsoft.edgemac.Beta":          "Microsoft Edge Beta",
    "com.microsoft.edgemac.Dev":           "Microsoft Edge Dev",
    "com.microsoft.edgemac.Canary":        "Microsoft Edge Canary",
}


def get_page_text(bundle_id: str | None) -> str | None:
    """
    Execute JS in the frontmost browser tab via AppleScript to get page body text.

    Controlled by USE_APPLESCRIPT_PAGE_TEXT in config.py.
    Returns None immediately (zero cost) when the flag is off, the app is not
    a supported browser, or AppleScript fails for any reason.
    """
    from ..config import USE_APPLESCRIPT_PAGE_TEXT, APPLESCRIPT_MAX_CHARS

    if not USE_APPLESCRIPT_PAGE_TEXT:
        return None
    if bundle_id not in BROWSER_BUNDLES:
        return None

    app_name = _APPLESCRIPT_APP_NAMES.get(bundle_id)
    if not app_name:
        return None  # Firefox — no AppleScript JS support

    js = (
        f"(function() {{"
        f"  var MAX = {APPLESCRIPT_MAX_CHARS};"
        f"  var gdDoc = document;"
        f"  var frames = document.querySelectorAll('iframe');"
        f"  for (var fi = 0; fi < frames.length; fi++) {{"
        f"    try {{"
        f"      var fd = frames[fi].contentDocument || frames[fi].contentWindow.document;"
        f"      if (fd && fd.querySelectorAll('.kix-wordhtmlgenerator-word-node').length > 0) {{ gdDoc = fd; break; }}"
        f"    }} catch(e) {{}}"
        f"  }}"
        f"  var gdWords = gdDoc.querySelectorAll('.kix-wordhtmlgenerator-word-node');"
        f"  if (gdWords.length > 0) {{"
        f"    var words = [];"
        f"    gdWords.forEach(function(w) {{"
        f"      var t = (w.innerText || w.textContent || '');"
        f"      if (t.length > 0) words.push(t);"
        f"    }});"
        f"    var r = words.join('').substring(0, MAX);"
        f"    if (r.length > 0) return r;"
        f"  }}"
        f"  function findScroller() {{"
        f"    var el = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);"
        f"    while (el && el !== document.body) {{"
        f"      if (el.scrollHeight > el.clientHeight + 10 && el.scrollTop > 0) return el;"
        f"      el = el.parentElement;"
        f"    }}"
        f"    return null;"
        f"  }}"
        f"  var scroller = findScroller();"
        f"  var scrollerRect = scroller ? scroller.getBoundingClientRect() : null;"
        f"  var viewH = window.innerHeight;"
        f"  var buf = 100;"
        f"  var vt = [];"
        f"  var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);"
        f"  var node;"
        f"  while (node = walker.nextNode()) {{"
        f"    var t = node.nodeValue.trim();"
        f"    if (t.length < 3) continue;"
        f"    var p = node.parentElement;"
        f"    if (!p) continue;"
        f"    var tag = p.tagName;"
        f"    if (tag === 'STYLE' || tag === 'SCRIPT' || tag === 'NOSCRIPT') continue;"
        f"    var s = window.getComputedStyle(p);"
        f"    if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') continue;"
        f"    var r = p.getBoundingClientRect();"
        f"    if (scrollerRect) {{"
        f"      if (r.bottom < scrollerRect.top - buf || r.top > scrollerRect.bottom + buf) continue;"
        f"    }} else {{"
        f"      if (r.bottom < -buf || r.top > viewH + buf) continue;"
        f"    }}"
        f"    vt.push(t);"
        f"  }}"
        f"  var result = vt.join(' ').substring(0, MAX);"
        f"  return result.length > 0 ? result : 'empty';"
        f"}})()"
    )
    script = (
        f'tell application "{app_name}"\n'
        f'    execute front window\'s active tab javascript "{js}"\n'
        f'end tell'
    )

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3,
        )
        text = result.stdout.strip()
        return text if text else None
    except Exception:
        return None


# ── Focused element ───────────────────────────────────────────────────────────

def get_focused_element(pid: int | None) -> tuple[str | None, str | None]:
    """Return (text, role) of the currently focused UI element."""
    if not pid or not AS.AXIsProcessTrusted():
        return None, None

    app_elem = AS.AXUIElementCreateApplication(pid)
    err, focused = AS.AXUIElementCopyAttributeValue(
        app_elem, AS.kAXFocusedUIElementAttribute, None
    )
    if err != AS.kAXErrorSuccess or not focused:
        return None, None

    err_r, role = AS.AXUIElementCopyAttributeValue(focused, AS.kAXRoleAttribute, None)
    role_str = str(role) if err_r == AS.kAXErrorSuccess and role else None

    for attr in (AS.kAXValueAttribute, AS.kAXSelectedTextAttribute, AS.kAXTitleAttribute):
        err_v, val = AS.AXUIElementCopyAttributeValue(focused, attr, None)
        if err_v == AS.kAXErrorSuccess and val:
            return str(val)[:500], role_str

    return None, role_str


# ── Visible text ──────────────────────────────────────────────────────────────

def get_visible_text(pid: int | None) -> str | None:
    """Walk the AX tree of the frontmost window and collect visible text."""
    if not pid or not AS.AXIsProcessTrusted():
        return None

    app_elem = AS.AXUIElementCreateApplication(pid)
    err, wins = AS.AXUIElementCopyAttributeValue(
        app_elem, AS.kAXWindowsAttribute, None
    )
    if err != AS.kAXErrorSuccess or not wins:
        return None

    collected: list[str] = []
    _walk_tree(wins[0], collected, depth=0, max_depth=30)
    text = "\n".join(collected)
    return text[-MAX_TEXT_CHARS:] if text.strip() else None


def _walk_tree(element, collected: list[str], depth: int, max_depth: int) -> None:
    """Depth-first walk of the AX tree; appends unique non-trivial text."""
    if depth > max_depth:
        return

    for attr in (AS.kAXValueAttribute, AS.kAXTitleAttribute, "AXDescription"):
        err, val = AS.AXUIElementCopyAttributeValue(element, attr, None)
        if err == AS.kAXErrorSuccess and val:
            text = str(val).strip()
            if len(text) > 2 and text not in collected:
                collected.append(text)
                break   # only one text value per element

    err_c, children = AS.AXUIElementCopyAttributeValue(
        element, AS.kAXChildrenAttribute, None
    )
    if err_c == AS.kAXErrorSuccess and children:
        for child in children:
            _walk_tree(child, collected, depth + 1, max_depth)
