"""Transport-independent MCP inspection, shared by the stdio shim and the HTTP gateway.

``SessionTracker`` follows one MCP session: it learns who the client and
server are (``initialize`` handshake or the 2026-07-28 per-request ``_meta``
envelope), caches tool metadata from ``tools/list`` so popups can show tool
descriptions and risk annotations, correlates responses with requests for the
audit log, and recognises continuations of already-approved multi-round tool
calls (``input_required`` results, MCP 2026-07-28).
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from dataclasses import dataclass
from typing import Any

from mcp_guardian.jsonrpc import (
    GUARDIAN_BLOCKED,
    Kind,
    classify,
    dumps,
    error_response,
    id_key,
    normalize_method,
    params_of,
)
from mcp_guardian.models import ApprovalRequest, Decision, SessionInfo, new_id

CONTINUATION_TTL = 15 * 60
MAX_INFLIGHT = 10_000
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"

_BLOCK_REASONS = {
    "user": "the user denied it",
    "remembered": "the user previously chose to always block it",
    "rule": "a policy rule denies it",
    "timeout": "nobody approved it in time",
    "unavailable": "the approval service is not running (fail-closed)",
    "limit": "too many requests are already waiting for approval",
    "no-ui": "no approval UI is available",
    "error": "an internal error occurred (fail-closed)",
    "invalid": "the request could not be evaluated",
}


@dataclass
class _Inflight:
    method: str
    target: str | None
    started: float
    approved: bool
    guardian_id: str | None
    fingerprint: str | None


def extract_target(method: str, params: dict[str, Any]) -> tuple[str | None, Any]:
    """(tool name / resource URI / prompt name, arguments) for display and rules."""
    if method in ("tools/call", "prompts/get"):
        name = params.get("name")
        return (name if isinstance(name, str) else None), params.get("arguments")
    if method == "resources/read":
        uri = params.get("uri")
        return (uri if isinstance(uri, str) else None), None
    rest = {k: v for k, v in params.items() if k != "_meta"}
    return None, rest or None


def _fingerprint(name: Any, arguments: Any) -> str:
    return hashlib.sha256(dumps([name, arguments]).encode()).hexdigest()


def blocked_response(msg: dict[str, Any], decision: Decision, style: str) -> dict[str, Any]:
    """The JSON-RPC answer for a blocked request (it never reaches the server)."""
    method = msg.get("method", "?")
    text = f"MCP Guardian blocked this {method} request: {_BLOCK_REASONS.get(decision.reason, decision.reason)}."
    if decision.reason in ("user", "remembered", "rule"):
        text += " The action was NOT executed. Do not retry it unless the user explicitly asks you to."
    if style == "result" and method == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": msg.get("id"),
            "result": {"content": [{"type": "text", "text": text}], "isError": True},
        }
    data = {"guardian": "mcp-guardian", "reason": decision.reason, "detail": decision.detail}
    return error_response(msg.get("id"), GUARDIAN_BLOCKED, text, data)


class SessionTracker:
    def __init__(self, session: SessionInfo, tools: dict[str, dict[str, Any]] | None = None) -> None:
        self.session = session
        # Tool metadata for popups; the gateway shares one catalogue per upstream so
        # stateless requests still get descriptions and risk hints.
        self.tools: dict[str, dict[str, Any]] = tools if tools is not None else {}
        self._inflight: dict[str, _Inflight] = {}
        # Approved calls that answered "input_required": (requestState, fingerprint, expires).
        self._continuations: list[tuple[str | None, str | None, float]] = []

    # ------------------------------------------------------------ client -> server

    def observe_client_request(self, msg: dict[str, Any]) -> bool:
        """Learn client identity. Returns True if the session description changed."""
        params = params_of(msg)
        changed = False
        if msg.get("method") == "initialize":
            changed |= self._set_client(params.get("clientInfo"))
            changed |= self._set("protocol_version", params.get("protocolVersion"))
        meta = params.get("_meta")
        if isinstance(meta, dict):
            changed |= self._set_client(meta.get(META_CLIENT_INFO))
            changed |= self._set("protocol_version", meta.get(META_PROTOCOL_VERSION))
        return changed

    @staticmethod
    def is_gated(method: str, gated_methods: frozenset[str]) -> bool:
        return normalize_method(method) in gated_methods

    def build_request(self, msg: dict[str, Any]) -> ApprovalRequest:
        method = msg["method"]
        target, arguments = extract_target(method, params_of(msg))
        tool = self.tools.get(target) if method == "tools/call" and target else None
        return ApprovalRequest(
            request_id=new_id(),
            method=method,
            target=target,
            arguments=arguments,
            session=dataclasses.replace(self.session),
            jsonrpc_id=msg.get("id"),
            tool=tool,
        )

    def is_approved_continuation(self, msg: dict[str, Any]) -> bool:
        """True for the follow-up round of a tools/call the user already approved.

        MCP 2026-07-28 lets a server answer ``tools/call`` with ``resultType:
        "input_required"``; the client then re-sends the call with
        ``inputResponses``/``requestState``. Re-prompting would ask twice for the
        same action, so the follow-up is matched against what we approved: the
        tool name and arguments must be identical, and when the server issued a
        ``requestState`` the follow-up must echo it. Each approval is single use.
        """
        if msg.get("method") != "tools/call":
            return False
        params = params_of(msg)
        if "inputResponses" not in params and "requestState" not in params:
            return False
        now = time.monotonic()
        self._continuations = [c for c in self._continuations if c[2] >= now]
        state = params.get("requestState")
        fingerprint = _fingerprint(params.get("name"), params.get("arguments"))
        for entry in self._continuations:
            expected_state, expected_fp, _ = entry
            if expected_fp == fingerprint and (expected_state is None or expected_state == state):
                self._continuations.remove(entry)
                return True
        return False

    def track_forwarded(self, msg: dict[str, Any], *, approved: bool, guardian_id: str | None = None) -> None:
        if len(self._inflight) >= MAX_INFLIGHT:  # client never got answers; drop the oldest
            self._inflight.pop(next(iter(self._inflight)))
        method = msg["method"]
        params = params_of(msg)
        target, _ = extract_target(method, params)
        fingerprint = _fingerprint(params.get("name"), params.get("arguments")) if method == "tools/call" else None
        self._inflight[id_key(msg["id"])] = _Inflight(method, target, time.monotonic(), approved, guardian_id, fingerprint)

    # ------------------------------------------------------------ server -> client

    def observe_server_message(self, msg: Any) -> tuple[dict[str, Any] | None, bool]:
        """Inspect a server message. Returns (completion record or None, session changed)."""
        if classify(msg) is not Kind.RESPONSE:
            return None, False
        inflight = self._inflight.pop(id_key(msg.get("id")), None)
        if inflight is None:
            return None, False
        result = msg.get("result")
        changed = False
        if isinstance(result, dict):
            if inflight.method in ("initialize", "server/discover"):
                info = result.get("serverInfo")
                if isinstance(info, dict):
                    changed |= self._set("server_name", info.get("name"))
                    changed |= self._set("server_version", info.get("version"))
                changed |= self._set("protocol_version", result.get("protocolVersion"))
            elif inflight.method == "tools/list":
                for tool in result.get("tools") or []:
                    if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                        self.tools[tool["name"]] = {
                            "title": tool.get("title") or (tool.get("annotations") or {}).get("title"),
                            "description": tool.get("description"),
                            "annotations": tool.get("annotations") or {},
                        }
            elif inflight.method == "tools/call" and result.get("resultType") == "input_required" and inflight.approved:
                state = result.get("requestState")
                self._continuations.append(
                    (state if isinstance(state, str) else None, inflight.fingerprint, time.monotonic() + CONTINUATION_TTL)
                )
                del self._continuations[:-100]
        if "error" in msg:
            outcome = "error"
        elif isinstance(result, dict) and result.get("resultType") == "input_required":
            outcome = "input_required"
        elif isinstance(result, dict) and result.get("isError"):
            outcome = "tool_error"
        else:
            outcome = "ok"
        record = {
            "request_id": inflight.guardian_id,
            "session_id": self.session.session_id,
            "server": self.session.server,
            "method": inflight.method,
            "target": inflight.target,
            "outcome": outcome,
            "duration_ms": int((time.monotonic() - inflight.started) * 1000),
        }
        return record, changed

    # ------------------------------------------------------------ helpers

    def _set(self, attr: str, value: Any) -> bool:
        if isinstance(value, str) and value and getattr(self.session, attr) != value:
            setattr(self.session, attr, value[:200])
            return True
        return False

    def _set_client(self, info: Any) -> bool:
        if not isinstance(info, dict):
            return False
        return self._set("client_name", info.get("name")) | self._set("client_version", info.get("version"))
