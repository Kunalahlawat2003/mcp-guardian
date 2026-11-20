"""Allow/block rules: static rules from config plus decisions the user chose to remember."""

from __future__ import annotations

import glob
import threading
from dataclasses import dataclass
from fnmatch import fnmatchcase

from mcp_guardian.models import ApprovalRequest

VALID_ACTIONS = {"allow", "block", "ask"}


def _match(pattern: str, value: str) -> bool:
    return pattern == "*" or fnmatchcase(value, pattern)


@dataclass(frozen=True)
class Rule:
    """Glob-based rule. ``tool`` matches the tool name, resource URI or prompt name."""

    action: str  # allow | block | ask
    server: str = "*"
    tool: str = "*"
    method: str = "*"
    transport: str = "*"  # stdio | http
    note: str = ""
    remembered: bool = False

    def matches(self, req: ApprovalRequest) -> bool:
        return (
            _match(self.server, req.session.server)
            and _match(self.method, req.method)
            and _match(self.tool, req.target or "")
            and _match(self.transport, req.session.transport)
        )

    def describe(self) -> str:
        parts = [f"{k}={v}" for k, v in (("server", self.server), ("method", self.method),
                                           ("tool", self.tool), ("transport", self.transport)) if v != "*"]  # fmt: skip
        scope = ", ".join(parts) or "everything"
        origin = "remembered decision" if self.remembered else "config rule"
        return f"{origin}: {self.action} {scope}" + (f" ({self.note})" if self.note else "")


class PolicyEngine:
    """First matching rule wins; remembered decisions take precedence over config rules."""

    def __init__(self, rules: tuple[Rule, ...] | list[Rule] = ()) -> None:
        self._rules = list(rules)
        self._remembered: list[Rule] = []
        self._lock = threading.Lock()

    def evaluate(self, req: ApprovalRequest) -> Rule | None:
        with self._lock:
            for rule in (*self._remembered, *self._rules):
                if rule.matches(req):
                    return rule
        return None

    def remember(self, req: ApprovalRequest, allowed: bool) -> Rule:
        """Remember a decision for this server + method + tool (until the daemon restarts)."""
        rule = Rule(
            action="allow" if allowed else "block",
            server=glob.escape(req.session.server),
            method=glob.escape(req.method),
            tool=glob.escape(req.target) if req.target else "*",
            transport=glob.escape(req.session.transport),
            remembered=True,
        )
        with self._lock:
            self._remembered.insert(0, rule)
        return rule

    def forget_all(self) -> int:
        with self._lock:
            count = len(self._remembered)
            self._remembered.clear()
        return count

    @property
    def remembered(self) -> list[Rule]:
        with self._lock:
            return list(self._remembered)

    @property
    def rules(self) -> list[Rule]:
        return list(self._rules)
