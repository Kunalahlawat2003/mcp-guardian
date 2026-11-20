"""Data model shared by interceptors (shim, gateway), the daemon and the UI."""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from typing import Any


def new_id() -> str:
    return uuid.uuid4().hex


def _from_dict(cls: type, data: dict[str, Any]) -> Any:
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in names})


# clientInfo.name values that identify Anthropic's MCP clients.
_CLAUDE_CLIENT_NAMES = {"claude-ai", "claude", "claude-desktop", "claude desktop", "anthropic/claudeai"}


@dataclass
class SessionInfo:
    """One MCP connection between a client (Claude) and a server, as seen by an interceptor."""

    session_id: str
    transport: str  # "stdio" (Claude Desktop local server) | "http" (remote connector via gateway)
    server: str  # name from claude_desktop_config.json or gateway upstream name
    client_name: str | None = None
    client_version: str | None = None
    server_name: str | None = None
    server_version: str | None = None
    protocol_version: str | None = None
    command: list[str] | None = None  # stdio: the real server command line
    upstream_url: str | None = None  # http: the real server URL
    peer: str | None = None  # http: remote address as seen by the gateway
    user_agent: str | None = None
    interceptor_pid: int | None = None
    started_at: float = field(default_factory=time.time)

    @property
    def source_label(self) -> str:
        """Human-friendly description of who initiated the request."""
        name = (self.client_name or "").strip()
        lowered = name.lower()
        looks_like_claude = lowered in _CLAUDE_CLIENT_NAMES or "claude" in lowered or "anthropic" in lowered
        if self.transport == "stdio":
            if looks_like_claude:
                return "Claude Desktop"
            return f"{name} (local stdio client)" if name else "Local stdio client"
        ua = (self.user_agent or "").lower()
        if looks_like_claude or "claude" in ua or "anthropic" in ua:
            return "Claude (claude.ai / remote connector)"
        return f"{name} (remote HTTP client)" if name else "Remote HTTP client"

    @property
    def client_label(self) -> str:
        if not self.client_name:
            return "unknown client"
        return f"{self.client_name} {self.client_version or ''}".strip()

    @property
    def server_label(self) -> str:
        if self.server_name and self.server_name != self.server:
            version = f" {self.server_version}" if self.server_version else ""
            return f"{self.server} ({self.server_name}{version})"
        return self.server

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionInfo:
        return _from_dict(cls, data)


@dataclass
class ApprovalRequest:
    """A held MCP request awaiting an allow/block decision."""

    request_id: str  # guardian-generated, unique
    method: str  # e.g. "tools/call"
    target: str | None  # tool name / resource URI / prompt name
    arguments: Any  # tool or prompt arguments (may be large)
    session: SessionInfo
    jsonrpc_id: Any = None  # the client's JSON-RPC id (for display/audit)
    tool: dict[str, Any] | None = None  # title/description/annotations from tools/list
    received_at: float = field(default_factory=time.time)

    @property
    def risk(self) -> str:
        """Classify using server-declared tool annotations (hints, not guarantees).

        MCP defaults: readOnlyHint=false, destructiveHint=true when not read-only.
        """
        if self.method != "tools/call":
            return "read"
        ann = (self.tool or {}).get("annotations") or {}
        if not ann:
            return "unknown"
        if ann.get("readOnlyHint") is True:
            return "read"
        if ann.get("destructiveHint", True):
            return "destructive"
        return "write"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["session"] = self.session.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApprovalRequest:
        data = dict(data)
        data["session"] = SessionInfo.from_dict(data.get("session") or {"session_id": "?", "transport": "?", "server": "?"})
        return _from_dict(cls, data)


@dataclass
class Decision:
    allowed: bool
    # user | rule | remembered | timeout | unavailable | limit | no-ui | continuation | cancelled
    reason: str
    detail: str = ""
    remember: bool = False
    decided_at: float = field(default_factory=time.time)

    @property
    def action(self) -> str:
        return "allow" if self.allowed else "block"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Decision:
        return _from_dict(cls, data)

    @classmethod
    def allow(cls, reason: str, detail: str = "", remember: bool = False) -> Decision:
        return cls(True, reason, detail, remember)

    @classmethod
    def block(cls, reason: str, detail: str = "", remember: bool = False) -> Decision:
        return cls(False, reason, detail, remember)
