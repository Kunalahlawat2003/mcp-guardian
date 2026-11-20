"""Audit trail (JSON Lines) and the in-process event hub used by `status`/`monitor`."""

from __future__ import annotations

import asyncio
import collections
import datetime as _dt
import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SENSITIVE_KEY = re.compile(
    r"(pass(word|wd|phrase)?|secret|token|api[-_]?key|apikey|auth|credential|private[-_]?key|cookie|session[-_]?id)",
    re.IGNORECASE,
)
_MAX_STRING = 512


def redact(value: Any, mode: str = "redacted", _depth: int = 0) -> Any:
    """Prepare request arguments for the audit log.

    ``full`` keeps everything, ``none`` drops arguments, ``redacted`` masks values
    whose key looks sensitive and truncates long strings.
    """
    if mode == "full":
        return value
    if mode == "none":
        return "[omitted]"
    if _depth > 12:
        return "[nested too deep]"
    if isinstance(value, dict):
        return {
            k: ("[REDACTED]" if isinstance(k, str) and _SENSITIVE_KEY.search(k) else redact(v, mode, _depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, mode, _depth + 1) for v in value[:100]] + (["…"] if len(value) > 100 else [])
    if isinstance(value, str) and len(value) > _MAX_STRING:
        return value[:_MAX_STRING] + f"…(+{len(value) - _MAX_STRING} chars)"
    return value


class AuditLog:
    """Append-only JSONL file (mode 0600) with simple size-based rotation. Thread-safe."""

    def __init__(self, path: Path, max_bytes: int = 10 * 1024 * 1024, backups: int = 5) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backups = backups
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        data = line.encode("utf-8")
        with self._lock:
            try:
                self._rotate_if_needed(len(data))
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                try:
                    os.write(fd, data)
                finally:
                    os.close(fd)
            except OSError as exc:  # never let auditing take the daemon down
                log.warning("audit log write failed: %s", exc)

    def _rotate_if_needed(self, incoming: int) -> None:
        try:
            size = self.path.stat().st_size
        except FileNotFoundError:
            return
        if size + incoming <= self.max_bytes:
            return
        for i in range(self.backups - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{i}")
            if src.exists():
                os.replace(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
        os.replace(self.path, self.path.with_name(f"{self.path.name}.1"))


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


class EventHub:
    """Fan-out of daemon events to the audit log, a recent-events ring and live subscribers.

    Only called from the daemon's asyncio thread.
    """

    def __init__(self, audit: AuditLog | None, arguments_mode: str = "redacted", keep: int = 200) -> None:
        self.audit = audit
        self.arguments_mode = arguments_mode
        self.recent: collections.deque[dict[str, Any]] = collections.deque(maxlen=keep)
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        if "arguments" in fields:
            fields["arguments"] = redact(fields["arguments"], self.arguments_mode)
        record = {"ts": _now_iso(), "event": event, **fields}
        if self.audit is not None:
            self.audit.write(record)
        self.recent.append(record)
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()  # drop the oldest event for slow subscribers
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(record)
        return record

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)
