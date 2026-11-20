"""Terminal approval prompts, for headless machines or SSH sessions.

Commands typed into the daemon's terminal:
    a [N]    allow request #N (default: the oldest pending one)
    b [N]    block request #N
    a! [N] / b! [N]   same, and remember the choice
    l        list pending requests
"""

from __future__ import annotations

import sys
import threading
from typing import TextIO

from mcp_guardian.models import ApprovalRequest, Decision
from mcp_guardian.ui import format as fmt
from mcp_guardian.ui.base import PromptUI, Resolver


class ConsolePromptUI(PromptUI):
    def __init__(self, stdin: TextIO | None = None, out: TextIO | None = None) -> None:
        self._in = stdin or sys.stdin
        self._out = out or sys.stderr
        self._lock = threading.Lock()
        self._pending: dict[int, tuple[ApprovalRequest, Resolver]] = {}
        self._numbers: dict[str, int] = {}
        self._counter = 0
        threading.Thread(target=self._read_loop, name="console-ui", daemon=True).start()

    def _print(self, text: str) -> None:
        with self._lock:
            print(text, file=self._out, flush=True)

    def show(self, req: ApprovalRequest, deadline: float | None, resolve: Resolver) -> None:
        with self._lock:
            self._counter += 1
            number = self._counter
            self._pending[number] = (req, resolve)
            self._numbers[req.request_id] = number
        details = "\n".join(f"    {k:<13} {v}".replace("\n", "\n" + " " * 18) for k, v in fmt.detail_rows(req))
        args = fmt.pretty_arguments(req, 2000).replace("\n", "\n    ")
        self._print(
            f"\n=== [#{number}] {fmt.headline(req)}\n{details}\n    Arguments:\n    {args}\n"
            f"    -> type 'a {number}' to ALLOW or 'b {number}' to BLOCK ('a! {number}' remembers)"
        )

    def dismiss(self, request_id: str, message: str) -> None:
        with self._lock:
            number = self._numbers.pop(request_id, None)
            if number is None:
                return
            self._pending.pop(number, None)
        self._print(f"=== [#{number}] {message}")

    def notify(self, title: str, message: str) -> None:
        self._print(f"* {title}: {message}")

    def _read_loop(self) -> None:
        for raw in self._in:
            parts = raw.strip().split()
            if not parts:
                continue
            cmd = parts[0].lower()
            if cmd in ("l", "list"):
                with self._lock:
                    items = [f"  #{n}: {fmt.one_line(r)}" for n, (r, _) in sorted(self._pending.items())]
                self._print("\n".join(items) or "  (nothing pending)")
                continue
            if cmd.rstrip("!") not in ("a", "allow", "b", "block"):
                self._print("  commands: a [N] | b [N] | a! [N] | b! [N] | l")
                continue
            with self._lock:
                if len(parts) > 1 and parts[1].lstrip("#").isdigit():
                    number = int(parts[1].lstrip("#"))
                else:
                    number = min(self._pending) if self._pending else -1
                entry = self._pending.pop(number, None)
                if entry is not None:
                    self._numbers.pop(entry[0].request_id, None)
            if entry is None:
                self._print("  no such pending request")
                continue
            allowed = cmd.rstrip("!") in ("a", "allow")
            entry[1](Decision(allowed, "user", f"{'allowed' if allowed else 'blocked'} in the console", remember=cmd.endswith("!")))
            self._print(f"=== [#{number}] {'ALLOWED' if allowed else 'BLOCKED'}")
