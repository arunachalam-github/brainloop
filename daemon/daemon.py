"""
brainloop.daemon
~~~~~~~~~~~~~~~~
Entry point for the brainloop activity-capture daemon.

Run via:  python3 -m brainloop.daemon
Or via LaunchAgent:  see com.brainloop.agent.plist

Architecture: event-driven, zero polling.
  • NSWorkspace notification → app_switch writes
  • AXObserver callbacks     → window/focus/value/title writes
  • CoreAudio listeners      → audio_start/stop, mic_start/stop writes
  • CFRunLoopTimer (60s)     → heartbeat writes
All four live on the same CFRunLoop; the main thread simply blocks in
CFRunLoopRun() waiting for events.
"""

import signal
import sys
import logging
from pathlib import Path

import AppKit
import ApplicationServices as AS
import CoreFoundation as CF

from .config import HEARTBEAT_SECS, LOG_PATH, DB_PATH
from . import db as _db
from .capture.ax import get_active_app
from .capture import observer as _observer
from .capture import workspace as _workspace
from .capture import audio as _audio

# ── Logging ───────────────────────────────────────────────────────────────────
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

_handlers: list[logging.Handler] = [logging.FileHandler(LOG_PATH)]
if sys.stdout.isatty():
    _handlers.append(logging.StreamHandler(sys.stdout))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    handlers=_handlers,
)
log = logging.getLogger("brainloop.daemon")


# ── Heartbeat timer callback ──────────────────────────────────────────────────

def _heartbeat_cb(timer, info) -> None:
    _db.write_snapshot("heartbeat")
    log.info("heartbeat | total records: %d", _db.total_records())


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("brainloop daemon starting")
    log.info("DB: %s", DB_PATH)

    # Check Accessibility permission (prompt if missing)
    ax_ok = AS.AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True})
    if ax_ok:
        log.info("Accessibility permission: granted")
    else:
        log.warning("Accessibility NOT granted — window titles and URLs will be null")

    # Open DB and register the connection with the db module
    conn = _db.open_db()
    _db.init(conn)

    # Cocoa run loop (required for NSWorkspace notifications and CFRunLoopTimer)
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyProhibited)  # no Dock icon

    # NSWorkspace observer (app switches)
    _ws_observer = _workspace.setup()   # hold reference — prevents GC

    # AX observer for the current frontmost app
    _, _, pid = get_active_app()
    _observer.install(pid)

    # CoreAudio listeners for audio playback and mic activity
    # These integrate with CFRunLoop automatically — no extra threads needed
    _audio.setup()

    # Startup snapshot
    _db.write_snapshot("startup")
    log.info("Startup snapshot written. Listening for events…")

    # Heartbeat timer via CFRunLoop (fires every HEARTBEAT_SECS)
    heartbeat_timer = CF.CFRunLoopTimerCreate(
        None,                                                   # allocator
        CF.CFAbsoluteTimeGetCurrent() + HEARTBEAT_SECS,        # first fire
        HEARTBEAT_SECS,                                         # repeat interval
        0,                                                      # flags
        0,                                                      # order
        _heartbeat_cb,                                          # callback
        None,                                                   # info
    )
    CF.CFRunLoopAddTimer(
        CF.CFRunLoopGetCurrent(), heartbeat_timer, CF.kCFRunLoopDefaultMode
    )

    # Graceful shutdown on SIGTERM / SIGINT
    rl = CF.CFRunLoopGetCurrent()

    def _stop(sig, _frame) -> None:
        log.info("Signal %s — stopping", sig)
        CF.CFRunLoopStop(rl)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT,  _stop)

    # Block here — all work happens in the callbacks above
    CF.CFRunLoopRun()

    # Teardown
    _audio.teardown()
    _observer.remove()
    conn.close()
    log.info("Daemon stopped cleanly. Total records written: %d", _db.total_records())


if __name__ == "__main__":
    main()
