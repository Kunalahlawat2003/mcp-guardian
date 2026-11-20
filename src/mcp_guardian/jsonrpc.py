"""JSON-RPC 2.0 helpers for MCP traffic.

Security note: the proxy must forward *exactly* what it inspected. A lenient
parser on our side and a different parser on the server side could disagree on
what a message means (duplicate keys, NaN, odd id types...). We therefore parse
strictly, refuse anything ambiguous, and forward the re-serialised object we
inspected rather than the raw bytes we received.
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603
# Implementation-defined server error range is -32000..-32099.
GUARDIAN_BLOCKED = -32050
GUARDIAN_CANCELLED = -32051


class Kind(str, Enum):
    REQUEST = "request"
    NOTIFICATION = "notification"
    RESPONSE = "response"
    INVALID = "invalid"


class StrictJSONError(ValueError):
    """Raised for JSON that is syntactically valid but ambiguous or non-standard."""


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    obj: dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise StrictJSONError(f"duplicate key {key!r}")
        obj[key] = value
    return obj


def _reject_constant(name: str) -> Any:
    raise StrictJSONError(f"non-standard JSON constant {name}")


def _finite_float(text: str) -> float:
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):  # e.g. 1e400 overflows to inf
        raise StrictJSONError(f"number out of range: {text[:40]}")
    return value


def loads_strict(data: bytes | bytearray | str) -> Any:
    """Parse JSON, rejecting anything we could not forward faithfully. Raises ValueError.

    Rejected: invalid UTF-8, duplicate keys, NaN/Infinity, numbers that overflow
    to infinity, and lone surrogates (``"\\ud800"``), i.e. everything that would
    either be ambiguous for the server or impossible to re-serialise.
    """
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8")  # UnicodeDecodeError is a ValueError
    obj = json.loads(data, object_pairs_hook=_no_duplicate_keys, parse_constant=_reject_constant, parse_float=_finite_float)
    try:
        dumps(obj).encode("utf-8")
    except UnicodeEncodeError as exc:
        raise StrictJSONError("string contains an unpaired surrogate") from exc
    return obj


def loads_lenient(data: bytes | bytearray | str) -> Any:
    """Parse JSON for observation only (never used to decide what to forward)."""
    if isinstance(data, (bytes, bytearray)):
        data = bytes(data).decode("utf-8", errors="replace")
    return json.loads(data)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def encode_line(obj: Any) -> bytes:
    """Serialise one message for newline-delimited transports (stdio, IPC)."""
    return (dumps(obj) + "\n").encode("utf-8")


def is_valid_request_id(value: Any) -> bool:
    # MCP: request ids MUST be a string or an integer and MUST NOT be null.
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def classify(msg: Any) -> Kind:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != JSONRPC_VERSION:
        return Kind.INVALID
    if "method" in msg:
        if not isinstance(msg["method"], str):
            return Kind.INVALID
        if "params" in msg and not isinstance(msg["params"], (dict, list)):
            return Kind.INVALID
        if "id" in msg:
            return Kind.REQUEST if is_valid_request_id(msg["id"]) else Kind.INVALID
        return Kind.NOTIFICATION
    if "id" in msg and (("result" in msg) != ("error" in msg)):
        return Kind.RESPONSE
    return Kind.INVALID


def normalize_method(method: str) -> str:
    """Canonical form used for gating decisions (defence against case/whitespace tricks)."""
    return method.strip().lower()


def id_key(request_id: Any) -> str:
    """Hashable key that keeps ``1`` and ``"1"`` distinct."""
    return dumps(request_id)


def error_response(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def params_of(msg: dict[str, Any]) -> dict[str, Any]:
    params = msg.get("params")
    return params if isinstance(params, dict) else {}
