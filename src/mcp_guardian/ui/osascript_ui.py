"""macOS fallback when Tkinter is unavailable: native AppleScript dialogs.

Each prompt is an independent ``osascript`` process, so concurrent requests
get independent dialogs. The request text is passed as an argv item (never
interpolated into the script), so it cannot inject AppleScript.
"""

from __future__ import annotations

import subprocess
import threading
import time

from mcp_guardian.models import ApprovalRequest, Decision
from mcp_guardian.ui import format as fmt
from mcp_guardian.ui.base import PromptUI, Resolver

_DIALOG = """
on run argv
    set msg to item 1 of argv
    set waitSecs to (item 2 of argv) as integer
    tell me to activate
    if waitSecs > 0 then
        set r to display dialog msg with title "MCP Guardian" buttons {"Block", "Allow"} default button "Block" cancel button "Block" with icon caution giving up after waitSecs
    else
        set r to display dialog msg with title "MCP Guardian" buttons {"Block", "Allow"} default button "Block" cancel button "Block" with icon caution
    end if
    if gave up of r then return "timeout"
    return button returned of r
end run
"""

_NOTIFY = """
on run argv
    display notification (item 2 of argv) with title "MCP Guardian" subtitle (item 1 of argv)
end run
"""


class OsaScriptPromptUI(PromptUI):
    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen[str]] = {}
        self._dismissed: set[str] = set()
        self._lock = threading.Lock()

    def show(self, req: ApprovalRequest, deadline: float | None, resolve: Resolver) -> None:
        threading.Thread(target=self._run, args=(req, deadline, resolve), daemon=True).start()

    def _run(self, req: ApprovalRequest, deadline: float | None, resolve: Resolver) -> None:
        wait = max(1, int(deadline - time.time())) if deadline else 0
        try:
            proc = subprocess.Popen(
                ["osascript", "-e", _DIALOG, fmt.plain_text(req), str(wait)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )  # fmt: skip
        except OSError as exc:
            resolve(Decision.block("no-ui", f"osascript failed: {exc}"))
            return
        with self._lock:
            self._procs[req.request_id] = proc
            withdrawn_early = req.request_id in self._dismissed
        if withdrawn_early:  # dismiss() raced ahead of the dialog being registered
            proc.terminate()
        out, _err = proc.communicate()
        with self._lock:
            self._procs.pop(req.request_id, None)
            if req.request_id in self._dismissed:
                self._dismissed.discard(req.request_id)
                return
        answer = (out or "").strip()
        if answer == "timeout":
            return  # the broker's own timer blocks the request
        allowed = answer == "Allow"
        resolve(Decision(allowed, "user", f"{'allowed' if allowed else 'blocked'} in the macOS dialog"))

    def dismiss(self, request_id: str, message: str) -> None:
        with self._lock:
            self._dismissed.add(request_id)  # remembered even if the dialog isn't up yet
            proc = self._procs.get(request_id)
        if proc is not None:
            proc.terminate()

    def notify(self, title: str, message: str) -> None:
        try:
            subprocess.Popen(["osascript", "-e", _NOTIFY, title, message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
        except OSError:
            pass

    def close(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            proc.terminate()
