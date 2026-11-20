"""Approval UIs. ``create_ui`` picks the best one available on this machine."""

from __future__ import annotations

import logging
import os
import shutil
import sys

from mcp_guardian.ui.base import NullPromptUI, PromptUI

log = logging.getLogger(__name__)


def tk_available() -> bool:
    try:
        import tkinter  # noqa: F401
    except ImportError:
        return False
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    return True


def create_ui(kind: str = "auto", arm_delay: float = 0.8) -> PromptUI:
    """Order for ``auto``: Tk popup -> macOS AppleScript dialog -> console (if a TTY) -> none."""
    if kind in ("auto", "tk"):
        if tk_available():
            from mcp_guardian.ui.tk_ui import TkPromptUI

            return TkPromptUI(arm_delay=arm_delay)
        if kind == "tk":
            raise RuntimeError(
                "Tkinter is not available for this Python. On macOS use the python.org installer "
                "or `brew install python-tk@3.13`; on Linux install python3-tk."
            )
        log.warning("Tkinter unavailable; falling back to another approval UI")
    if kind in ("auto", "osascript") and sys.platform == "darwin" and shutil.which("osascript"):
        from mcp_guardian.ui.osascript_ui import OsaScriptPromptUI

        return OsaScriptPromptUI()
    if kind == "console" or (kind == "auto" and sys.stdin is not None and sys.stdin.isatty()):
        from mcp_guardian.ui.console_ui import ConsolePromptUI

        return ConsolePromptUI()
    log.warning("no interactive approval UI available: every gated request will be BLOCKED")
    return NullPromptUI()


__all__ = ["PromptUI", "NullPromptUI", "create_ui", "tk_available"]
