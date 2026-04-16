"""
brainloop.capture.workspace
~~~~~~~~~~~~~~~~~~~~~~~~~~~
NSWorkspace observer — fires on every app switch.

Uses an Objective-C subclass (NSObject) to receive the
NSWorkspaceDidActivateApplicationNotification posted by the system.
Each activation writes an 'app_switch' snapshot and then reinstalls
the AXObserver on the newly active process.
"""

import logging
import objc
import AppKit

from .. import db as _db_module
from . import observer as _observer_module

log = logging.getLogger("brainloop.capture.workspace")


class _WorkspaceObserver(AppKit.NSObject):
    """Objective-C object that receives app-activation notifications."""

    def appActivated_(self, notification) -> None:
        info = notification.userInfo()
        if not info:
            return
        app = info.get("NSWorkspaceApplicationKey")
        if not app:
            return

        pid = int(app.processIdentifier())
        log.debug("App switch → %s (pid %s)", app.localizedName(), pid)

        # Write first so the snapshot captures the *new* app, then swap observer.
        _db_module.write_snapshot("app_switch")
        _observer_module.install(pid)


def setup() -> _WorkspaceObserver:
    """
    Register for app-activation notifications.

    Returns the observer object — the caller must hold a reference to it
    for the lifetime of the run loop, or it will be garbage-collected.
    """
    observer = _WorkspaceObserver.alloc().init()
    nc = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
    nc.addObserver_selector_name_object_(
        observer,
        objc.selector(observer.appActivated_, signature=b"v@:@"),
        AppKit.NSWorkspaceDidActivateApplicationNotification,
        None,
    )
    return observer
