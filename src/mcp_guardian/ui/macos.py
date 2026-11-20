"""Optional macOS integration via PyObjC (``pip install 'mcp-guardian[macos]'``).

Everything here is best effort: without PyObjC the popups still float above
other windows (Tk ``-topmost``); the app just keeps a Dock icon and the first
click may only activate the window.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def configure_background_app() -> None:
    """Run as an 'accessory' app: no Dock icon, no menu bar, but windows allowed."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory  # type: ignore[import-not-found]

        NSApplication.sharedApplication().setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    except Exception as exc:  # PyObjC missing or API changed
        log.debug("PyObjC unavailable, keeping default activation policy: %s", exc)


def bring_to_front() -> None:
    """Activate this process so a new popup receives the next click immediately."""
    if sys.platform != "darwin":
        return
    try:
        from AppKit import NSApplication  # type: ignore[import-not-found]

        NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
    except Exception as exc:
        log.debug("cannot activate app: %s", exc)
