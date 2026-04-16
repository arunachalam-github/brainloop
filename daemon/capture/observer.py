"""
brainloop.capture.observer
~~~~~~~~~~~~~~~~~~~~~~~~~~
AXObserver — listens for Accessibility notifications from the frontmost app.

Key fix vs. the old desktoptracker:
    AXObserverCreate() requires a *PyObjC closure*, not a plain Python function.
    The @objc.callbackFor(AS.AXObserverCreate) decorator wraps the function
    correctly so PyObjC can hand it to the C API as a retained callback.

The callback must be at module level (not inside a function) to prevent GC.
"""

import logging
import objc
import ApplicationServices as AS
import CoreFoundation as CF

from .. import db as _db_module

log = logging.getLogger("brainloop.capture.observer")

# ── Notifications to watch ────────────────────────────────────────────────────
_NOTIFICATIONS = (
    AS.kAXFocusedWindowChangedNotification,
    AS.kAXFocusedUIElementChangedNotification,
    AS.kAXValueChangedNotification,
    AS.kAXTitleChangedNotification,
)

_TRIGGER_MAP: dict[str, str] = {
    AS.kAXFocusedWindowChangedNotification:    "window_changed",
    AS.kAXFocusedUIElementChangedNotification: "focus_changed",
    AS.kAXValueChangedNotification:            "value_changed",
    AS.kAXTitleChangedNotification:            "title_changed",
}

# ── Module-level state ────────────────────────────────────────────────────────
_observer:     object | None = None   # AXObserverRef
_observer_pid: int    | None = None


# ── Callback (must be at module level, decorated as a PyObjC closure) ─────────

@objc.callbackFor(AS.AXObserverCreate)
def _ax_notification_cb(observer, element, notification, refcon) -> None:
    """
    Fires on the CFRunLoop when any watched AX notification arrives.
    Converts the notification name to a trigger string and calls write_snapshot.
    """
    notif_name = str(notification)
    is_value   = notif_name == AS.kAXValueChangedNotification
    trigger    = _TRIGGER_MAP.get(notif_name, notif_name)
    _db_module.write_snapshot(trigger, debounce=is_value)


# ── Install / remove ──────────────────────────────────────────────────────────

def install(pid: int | None) -> None:
    """
    Set up an AXObserver for *pid*, tearing down any previous one first.
    Silently does nothing if AX permission is not granted or pid is None.
    """
    global _observer, _observer_pid

    remove()

    if not pid or not AS.AXIsProcessTrusted():
        return

    err, observer = AS.AXObserverCreate(pid, _ax_notification_cb, None)
    if err != AS.kAXErrorSuccess:
        log.debug("AXObserverCreate failed for pid %s (err=%s)", pid, err)
        return

    app_elem = AS.AXUIElementCreateApplication(pid)
    for notif in _NOTIFICATIONS:
        AS.AXObserverAddNotification(observer, app_elem, notif, None)

    CF.CFRunLoopAddSource(
        CF.CFRunLoopGetCurrent(),
        AS.AXObserverGetRunLoopSource(observer),
        CF.kCFRunLoopDefaultMode,
    )

    _observer     = observer
    _observer_pid = pid
    log.debug("AXObserver installed for pid %s", pid)


def remove() -> None:
    """Remove and discard the current AXObserver, if any."""
    global _observer, _observer_pid

    if _observer is not None:
        try:
            CF.CFRunLoopRemoveSource(
                CF.CFRunLoopGetCurrent(),
                AS.AXObserverGetRunLoopSource(_observer),
                CF.kCFRunLoopDefaultMode,
            )
        except Exception:
            pass
        _observer     = None
        _observer_pid = None
