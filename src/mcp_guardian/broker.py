"""The decision point: rules first, then the user, with timeouts, limits and cancellation."""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from mcp_guardian.audit import EventHub
from mcp_guardian.config import Settings
from mcp_guardian.models import ApprovalRequest, Decision
from mcp_guardian.policy import PolicyEngine, Rule
from mcp_guardian.ui.base import PromptUI

log = logging.getLogger(__name__)


@dataclass
class _Pending:
    req: ApprovalRequest
    future: asyncio.Future[Decision]
    deadline: float | None


def _arguments_key(arguments: Any) -> str:
    try:
        return json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(arguments)


def request_summary(req: ApprovalRequest) -> dict[str, Any]:
    s = req.session
    return {
        "request_id": req.request_id,
        "session_id": s.session_id,
        "source": s.source_label,
        "client": s.client_label,
        "transport": s.transport,
        "server": s.server,
        "method": req.method,
        "target": req.target,
        "jsonrpc_id": req.jsonrpc_id,
        "risk": req.risk,
    }


class ApprovalBroker:
    """Owns all pending approvals. Lives on the daemon's asyncio loop.

    The UI runs on another thread (Tk must own the main thread on macOS), so it
    reports decisions through :meth:`_resolve_threadsafe`, which hops back onto
    the loop with ``call_soon_threadsafe``.
    """

    def __init__(self, settings: Settings, policy: PolicyEngine, events: EventHub, ui: PromptUI) -> None:
        self.settings = settings
        self.policy = policy
        self.events = events
        self.ui = ui
        self._pending: dict[str, _Pending] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------ public API

    async def evaluate(self, req: ApprovalRequest) -> Decision:
        """Decide on one request. Cancelling the calling task withdraws the popup."""
        self._loop = asyncio.get_running_loop()
        self.events.emit("request", **request_summary(req), arguments=req.arguments)

        rule = self.policy.evaluate(req)
        action = rule.action if rule else self.settings.default_action
        if action in ("allow", "block"):
            reason = "remembered" if rule is not None and rule.remembered else "rule"
            detail = rule.describe() if rule else f"default_action = {action}"
            return self._finish(req, Decision(action == "allow", reason, detail))

        session_pending = sum(1 for p in self._pending.values() if p.req.session.session_id == req.session.session_id)
        if len(self._pending) >= self.settings.max_pending or session_pending >= self.settings.max_pending_per_session:
            return self._finish(req, Decision.block("limit", "too many requests are already waiting for approval"))

        timeout = self.settings.approval_timeout
        deadline = time.time() + timeout if timeout > 0 else None
        future: asyncio.Future[Decision] = self._loop.create_future()
        self._pending[req.request_id] = _Pending(req, future, deadline)
        try:
            self.ui.show(req, deadline, functools.partial(self._resolve_threadsafe, req.request_id))
        except Exception as exc:  # a broken UI must never turn into an implicit allow
            log.exception("UI failed to show approval prompt")
            self._pending.pop(req.request_id, None)
            return self._finish(req, Decision.block("no-ui", f"approval UI failed: {exc}"))

        try:
            decision = await (asyncio.wait_for(future, timeout) if timeout > 0 else future)
        except TimeoutError:
            decision = Decision.block("timeout", f"no decision within {timeout:g} s")
            self.ui.dismiss(req.request_id, "Timed out: request blocked")
        except asyncio.CancelledError:
            self.ui.dismiss(req.request_id, "Withdrawn: the client cancelled or disconnected")
            self.events.emit("cancelled", **request_summary(req))
            raise
        finally:
            self._pending.pop(req.request_id, None)

        if decision.remember and decision.reason == "user":
            remembered = self.policy.remember(req, decision.allowed)
            self.events.emit("rule_remembered", rule=remembered.describe(), **request_summary(req))
            self._resolve_matching(remembered, req)
        return self._finish(req, decision)

    def pending(self) -> list[dict[str, Any]]:
        now = time.time()
        return [
            {**request_summary(p.req), "waiting_s": round(now - p.req.received_at, 1),
             "deadline_in_s": round(p.deadline - now, 1) if p.deadline else None}  # fmt: skip
            for p in self._pending.values()
        ]

    # ------------------------------------------------------------------ internals

    def _finish(self, req: ApprovalRequest, decision: Decision) -> Decision:
        self.events.emit(
            "decision",
            **request_summary(req),
            action=decision.action,
            reason=decision.reason,
            detail=decision.detail,
            remember=decision.remember,
            latency_ms=int((decision.decided_at - req.received_at) * 1000),
        )
        return decision

    def _resolve_threadsafe(self, request_id: str, decision: Decision) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._resolve, request_id, decision)
        except RuntimeError:  # loop shutting down
            pass

    def _resolve(self, request_id: str, decision: Decision) -> None:
        pending = self._pending.get(request_id)
        if pending is not None and not pending.future.done():
            pending.future.set_result(decision)

    def _resolve_matching(self, rule: Rule, decided: ApprovalRequest) -> None:
        """A just-remembered decision also settles *identical* requests still waiting.

        Identical means same server, method, tool and arguments: approving
        ``run_shell_command("ls")`` with Remember must not silently approve a
        pending ``run_shell_command("rm -rf ~")``. (Future calls follow the rule.)
        """
        fingerprint = _arguments_key(decided.arguments)
        for pending in list(self._pending.values()):
            if pending.future.done() or not rule.matches(pending.req):
                continue
            if _arguments_key(pending.req.arguments) != fingerprint:
                continue
            allowed = rule.action == "allow"
            pending.future.set_result(Decision(allowed, "remembered", rule.describe()))
            self.ui.dismiss(pending.req.request_id, f"{'Allowed' if allowed else 'Blocked'} by your remembered decision")
