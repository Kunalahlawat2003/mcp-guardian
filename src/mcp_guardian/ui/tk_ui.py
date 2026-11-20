"""Always-on-top Tkinter approval popups.

Threading model: Tk is not thread-safe and, on macOS, must run on the main
thread. The daemon's asyncio loop runs on a worker thread and talks to this
class only through ``self._queue``; ``_poll`` drains it every 50 ms on the Tk
thread. Decisions travel back through the broker's thread-safe resolver.

Safety details:
* Allow/Block are disabled for ``arm_delay`` seconds so a popup that appears
  under the cursor (or while the user is typing) cannot be approved by accident.
* There is no keyboard shortcut for Allow. Escape and the window close button
  both mean Block.
"""

from __future__ import annotations

import logging
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from mcp_guardian.models import ApprovalRequest, Decision
from mcp_guardian.ui import format as fmt
from mcp_guardian.ui import macos
from mcp_guardian.ui.base import PromptUI, Resolver

log = logging.getLogger(__name__)

POLL_MS = 50
WIDTH = 600
RISK_COLORS = {
    "destructive": "#B3261E",
    "write": "#A15C00",
    "unknown": "#7A5A00",
    "read": "#1E6B3A",
}
RISK_BADGES = {
    "destructive": "DESTRUCTIVE",
    "write": "MODIFIES DATA",
    "unknown": "UNKNOWN RISK",
    "read": "READ-ONLY",
}


class TkPromptUI(PromptUI):
    needs_main_thread = True

    def __init__(self, arm_delay: float = 0.8) -> None:
        self.arm_delay = arm_delay
        self._queue: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._windows: dict[str, ApprovalWindow] = {}
        self._toasts: list[tk.Toplevel] = []
        self._root: tk.Tk | None = None
        self._stop: threading.Event | None = None

    # ------------------------------------------------------------ thread-safe API

    def show(self, req: ApprovalRequest, deadline: float | None, resolve: Resolver) -> None:
        self._queue.put(("show", req, deadline, resolve))

    def dismiss(self, request_id: str, message: str) -> None:
        self._queue.put(("dismiss", request_id, message))

    def notify(self, title: str, message: str) -> None:
        self._queue.put(("notify", title, message))

    def close(self) -> None:
        self._queue.put(("quit",))

    def call_soon(self, fn: Callable[[], Any]) -> None:
        """Run ``fn`` on the Tk thread (used by self-tests and diagnostics)."""
        self._queue.put(("call", fn))

    # ------------------------------------------------------------ Tk thread

    def run_mainloop(self, stop: threading.Event) -> None:
        self._stop = stop
        root = tk.Tk()
        self._root = root
        root.withdraw()
        root.title("MCP Guardian")
        macos.configure_background_app()
        style = ttk.Style(root)
        if sys.platform == "darwin" and "aqua" in style.theme_names():
            style.theme_use("aqua")
        style.configure("Key.TLabel", foreground="#555555")
        style.configure("Status.TLabel", foreground="#555555")
        root.after(POLL_MS, self._poll)
        try:
            root.mainloop()
        finally:
            try:
                root.destroy()
            except tk.TclError:
                pass

    def _poll(self) -> None:
        assert self._root is not None and self._stop is not None
        try:
            for _ in range(200):
                try:
                    item = self._queue.get_nowait()
                except queue.Empty:
                    break
                self._handle(item)
        except Exception:  # never let one bad prompt kill the UI thread
            log.exception("UI error")
        if self._stop.is_set():
            self._root.quit()
            return
        self._root.after(POLL_MS, self._poll)

    def _handle(self, item: tuple[Any, ...]) -> None:
        kind = item[0]
        if kind == "show":
            _, req, deadline, resolve = item
            self._open(req, deadline, resolve)
        elif kind == "dismiss":
            window = self._windows.get(item[1])
            if window is not None:
                window.dismiss(item[2])
        elif kind == "notify":
            self._toast(item[1], item[2])
        elif kind == "call":
            item[1]()
        elif kind == "quit":
            assert self._stop is not None
            self._stop.set()

    def _open(self, req: ApprovalRequest, deadline: float | None, resolve: Resolver) -> None:
        assert self._root is not None
        used = {w.slot for w in self._windows.values()}
        slot = next(i for i in range(len(used) + 1) if i not in used)
        try:
            window = ApprovalWindow(self._root, req, deadline, resolve, slot, self.arm_delay, self._on_closed)
        except Exception as exc:
            log.exception("cannot open approval window")
            resolve(Decision.block("no-ui", f"approval window failed: {exc}"))
            return
        self._windows[req.request_id] = window
        self._refresh_positions()
        macos.bring_to_front()
        window.raise_()

    def _on_closed(self, request_id: str) -> None:
        self._windows.pop(request_id, None)
        self._refresh_positions()

    def _refresh_positions(self) -> None:
        ordered = sorted(self._windows.values(), key=lambda w: w.req.received_at)
        for index, window in enumerate(ordered, start=1):
            window.set_queue_position(index, len(ordered))

    def _toast(self, title: str, message: str) -> None:
        assert self._root is not None
        self._toasts = [t for t in self._toasts if t.winfo_exists()]
        toast = tk.Toplevel(self._root)
        toast.overrideredirect(True)
        toast.attributes("-topmost", True)
        frame = tk.Frame(toast, bg="#1F2937", padx=14, pady=10, highlightthickness=1, highlightbackground="#374151")
        frame.pack(fill="both", expand=True)
        tk.Label(frame, text=f"MCP Guardian · {title}", fg="#FFFFFF", bg="#1F2937",
                 font=("TkDefaultFont", 12, "bold"), anchor="w").pack(fill="x")  # fmt: skip
        tk.Label(frame, text=message, fg="#D1D5DB", bg="#1F2937", wraplength=340, justify="left", anchor="w").pack(fill="x")
        toast.update_idletasks()
        w, h = toast.winfo_reqwidth(), toast.winfo_reqheight()
        x = toast.winfo_screenwidth() - w - 24
        y = toast.winfo_screenheight() - h - 90 - sum(t.winfo_height() + 8 for t in self._toasts)
        toast.geometry(f"+{x}+{max(40, y)}")
        self._toasts.append(toast)
        toast.after(5000, toast.destroy)


class ApprovalWindow:
    """One popup per pending request; several can be open at once (cascaded)."""

    def __init__(
        self,
        root: tk.Tk,
        req: ApprovalRequest,
        deadline: float | None,
        resolve: Resolver,
        slot: int,
        arm_delay: float,
        on_closed: Callable[[str], None],
    ) -> None:
        self.req = req
        self.deadline = deadline
        self.slot = slot
        self._resolve = resolve
        self._on_closed = on_closed
        self.done = False
        self.armed = False

        color = RISK_COLORS.get(req.risk, RISK_COLORS["unknown"])
        top = tk.Toplevel(root)
        self.top = top
        top.title(f"MCP Guardian: approve {req.method}?")
        top.attributes("-topmost", True)
        top.protocol("WM_DELETE_WINDOW", lambda: self._decide(False, "window closed"))
        top.bind("<Escape>", lambda _e: self._decide(False, "Escape key"))
        top.minsize(480, 360)

        # Header: colour encodes risk, badge shows queue position.
        header = tk.Frame(top, bg=color)
        header.pack(fill="x")
        title_row = tk.Frame(header, bg=color)
        title_row.pack(fill="x", padx=16, pady=(12, 0))
        tk.Label(title_row, text="MCP request needs your approval", bg=color, fg="white",
                 font=("TkDefaultFont", 15, "bold")).pack(side="left")  # fmt: skip
        self._badge = tk.Label(title_row, text="", bg=color, fg="white", font=("TkDefaultFont", 11))
        self._badge.pack(side="right")
        tk.Label(header, text=fmt.headline(req), bg=color, fg="white", font=("TkDefaultFont", 12),
                 wraplength=WIDTH - 40, justify="left", anchor="w").pack(fill="x", padx=16, pady=(4, 0))  # fmt: skip
        badge_text = RISK_BADGES.get(req.risk, "") if req.method == "tools/call" else req.method.upper()
        tk.Label(header, text=badge_text, bg="white", fg=color, font=("TkDefaultFont", 10, "bold"),
                 padx=6).pack(anchor="w", padx=16, pady=(6, 12))  # fmt: skip

        body = ttk.Frame(top, padding=(16, 12, 16, 4))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)
        row = 0
        for key, value in fmt.detail_rows(req):
            ttk.Label(body, text=key, style="Key.TLabel").grid(row=row, column=0, sticky="nw", padx=(0, 12), pady=2)
            ttk.Label(body, text=value, wraplength=WIDTH - 170, justify="left").grid(row=row, column=1, sticky="w", pady=2)
            row += 1

        ttk.Label(body, text="Arguments", style="Key.TLabel").grid(row=row, column=0, sticky="nw", pady=(8, 2))
        text_frame = ttk.Frame(body)
        text_frame.grid(row=row, column=1, sticky="nsew", pady=(8, 2))
        body.rowconfigure(row, weight=1)
        args_text = fmt.pretty_arguments(req)
        lines = args_text.count("\n") + 1
        text = tk.Text(text_frame, height=min(14, max(3, lines)), wrap="word", font="TkFixedFont",
                       relief="solid", borderwidth=1, padx=6, pady=4)  # fmt: skip
        scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.insert("1.0", args_text)
        text.configure(state="disabled")
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        row += 1

        self.remember = tk.BooleanVar(value=False)
        target = req.target or req.method
        ttk.Checkbutton(
            body,
            text=f"Remember my choice for “{target}” on “{req.session.server}” until Guardian restarts",
            variable=self.remember,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 0))

        footer = ttk.Frame(top, padding=(16, 8, 16, 14))
        footer.pack(fill="x")
        self._status = ttk.Label(footer, text="", style="Status.TLabel")
        self._status.pack(side="left")
        self._allow = ttk.Button(footer, text="Allow", command=lambda: self._decide(True, "Allow button"), state="disabled")
        self._block = ttk.Button(footer, text="Block", command=lambda: self._decide(False, "Block button"), state="disabled")
        self._allow.pack(side="right")
        self._block.pack(side="right", padx=(0, 8))

        top.after(int(arm_delay * 1000), self._arm)
        self._tick()

        top.update_idletasks()
        width = max(WIDTH, top.winfo_reqwidth())
        height = min(top.winfo_reqheight(), top.winfo_screenheight() - 120)
        x = max(0, top.winfo_screenwidth() - width - 40 - 28 * slot)
        y = 50 + 28 * slot
        top.geometry(f"{width}x{height}+{x}+{y}")

    # ------------------------------------------------------------ behaviour

    def raise_(self) -> None:
        if self.done:
            return
        self.top.deiconify()
        self.top.lift()
        self.top.attributes("-topmost", True)
        try:
            self.top.focus_force()
        except tk.TclError:
            pass

    def set_queue_position(self, index: int, total: int) -> None:
        if not self.done:
            self._badge.configure(text=f"{index} of {total} pending" if total > 1 else "")

    def _arm(self) -> None:
        if not self.done:
            self.armed = True
            self._allow.configure(state="normal")
            self._block.configure(state="normal")

    def _tick(self) -> None:
        if self.done:
            return
        self._status.configure(text=fmt.remaining(self.deadline, time.time()))
        self.top.after(1000, self._tick)

    def _decide(self, allowed: bool, via: str) -> None:
        if self.done:
            return
        if allowed and not self.armed:
            return  # Allow is never accepted before the arm delay has elapsed
        self.done = True
        detail = f"{'allowed' if allowed else 'blocked'} by the user ({via})"
        try:
            self._resolve(Decision(allowed, "user", detail, remember=bool(self.remember.get())))
        finally:
            self._close()

    def dismiss(self, message: str) -> None:
        """Withdrawn by the broker (timeout, cancellation, remembered rule)."""
        if self.done:
            return
        self.done = True
        for button in (self._allow, self._block):
            button.configure(state="disabled")
        self._status.configure(text=message)
        self._badge.configure(text="")
        self.top.after(1800, self._close)

    def _close(self) -> None:
        try:
            self.top.destroy()
        except tk.TclError:
            pass
        self._on_closed(self.req.request_id)
