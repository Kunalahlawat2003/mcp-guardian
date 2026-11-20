"""Text rendering shared by all approval UIs."""

from __future__ import annotations

import datetime as _dt
import json
import shlex
from typing import Any

from mcp_guardian.models import ApprovalRequest

RISK_LABELS = {
    "destructive": "Destructive (server-declared)",
    "write": "Modifies data (server-declared)",
    "read": "Read-only (server-declared)",
    "unknown": "Unknown: the server declares no annotations",
}


def action_phrase(req: ApprovalRequest) -> str:
    target = req.target or "?"
    if req.method == "tools/call":
        return f"call tool “{target}”"
    if req.method == "resources/read":
        return f"read resource {target}"
    if req.method == "prompts/get":
        return f"get prompt “{target}”"
    return f"send {req.method}" + (f" ({target})" if req.target else "")


def headline(req: ApprovalRequest) -> str:
    return f"{req.session.source_label} wants to {action_phrase(req)}"


def server_line(req: ApprovalRequest) -> str:
    s = req.session
    line = s.server_label
    if s.command:
        line += "\n" + _shorten(shlex.join(s.command), 160)
    elif s.upstream_url:
        line += "\n" + s.upstream_url
    return line


def detail_rows(req: ApprovalRequest) -> list[tuple[str, str]]:
    s = req.session
    rows = [
        ("Requested by", f"{s.source_label}  ·  {s.client_label}  ·  {s.transport}"),
        ("MCP server", server_line(req)),
        ("Request", f"{req.method}" + (f"  →  {req.target}" if req.target else "")),
    ]
    tool = req.tool or {}
    about = tool.get("title") or ""
    if tool.get("description"):
        about = f"{about}: {tool['description']}" if about else tool["description"]
    if about:
        rows.append(("Description", _shorten(" ".join(about.split()), 300)))
    if req.method == "tools/call":
        rows.append(("Risk", RISK_LABELS.get(req.risk, req.risk)))
    if s.peer:
        rows.append(("Remote peer", f"{s.peer}" + (f"  ({_shorten(s.user_agent, 60)})" if s.user_agent else "")))
    received = _dt.datetime.fromtimestamp(req.received_at).strftime("%H:%M:%S")
    rows.append(("Received", f"{received}   (JSON-RPC id {json.dumps(req.jsonrpc_id)})"))
    return rows


def pretty_arguments(req: ApprovalRequest, limit: int = 100_000) -> str:
    if req.arguments in (None, {}, []):
        return "(no arguments)"
    try:
        text = json.dumps(req.arguments, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = repr(req.arguments)
    if len(text) > limit:
        text = text[:limit] + f"\n… truncated ({len(text) - limit:,} more characters)"
    return text


def plain_text(req: ApprovalRequest, arg_limit: int = 1200) -> str:
    """Compact multi-line description for native dialogs and the console."""
    lines = [headline(req), ""]
    lines += [f"{k}: {v}" for k, v in detail_rows(req)]
    lines += ["", "Arguments:", pretty_arguments(req, arg_limit)]
    return "\n".join(lines)


def one_line(req: ApprovalRequest) -> str:
    args = json.dumps(req.arguments, ensure_ascii=False, default=str) if req.arguments else ""
    return f"{req.session.source_label} -> {req.session.server}: {req.method} {req.target or ''} {_shorten(args, 120)}".rstrip()


def _shorten(text: str | None, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def remaining(deadline: float | None, now: float) -> str:
    if deadline is None:
        return "Waiting for your decision"
    left = max(0, int(deadline - now))
    return f"Auto-blocks in {left // 60}:{left % 60:02d}"


def safe_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(value)
