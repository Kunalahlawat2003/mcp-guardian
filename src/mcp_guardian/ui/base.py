"""The interface every approval UI implements."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from typing import Callable

from mcp_guardian.models import ApprovalRequest, Decision

# Called by the UI (from any thread) exactly once per request with the user's decision.
Resolver = Callable[[Decision], None]


class PromptUI(ABC):
    #: True if the UI must own the process main thread (Tk on macOS). The daemon
    #: then runs its asyncio loop on a worker thread and calls run_mainloop().
    needs_main_thread: bool = False

    @abstractmethod
    def show(self, req: ApprovalRequest, deadline: float | None, resolve: Resolver) -> None:
        """Display an approval prompt. Must be thread-safe and must not block."""

    @abstractmethod
    def dismiss(self, request_id: str, message: str) -> None:
        """Withdraw a prompt (timeout, cancellation, remembered rule). Thread-safe."""

    def notify(self, title: str, message: str) -> None:
        """Non-blocking informational notice (e.g. a new MCP connection). Thread-safe."""

    def run_mainloop(self, stop: threading.Event) -> None:
        """Block on the UI event loop until ``stop`` is set (main-thread UIs only)."""
        stop.wait()

    def close(self) -> None:
        """Release resources / leave the main loop. Thread-safe."""


class NullPromptUI(PromptUI):
    """Used when no interactive UI is available: everything that needs a human is blocked."""

    def show(self, req: ApprovalRequest, deadline: float | None, resolve: Resolver) -> None:
        resolve(Decision.block("no-ui", "no interactive approval UI is available on this machine"))

    def dismiss(self, request_id: str, message: str) -> None:
        pass
