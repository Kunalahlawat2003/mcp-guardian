"""IPC client used by interceptors (stdio shim, HTTP gateway) to ask the daemon for decisions.

Reliability rules:
* Connects lazily, starts the daemon on demand (``auto_start_daemon``) and
  reconnects after the daemon restarts.
* Many approvals can be in flight on one connection (multiplexed by ``rid``).
* If the daemon is unreachable or the connection drops while a request is
  waiting, the request resolves according to ``fail_mode`` (default: blocked).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from mcp_guardian import ipc
from mcp_guardian.config import Settings
from mcp_guardian.lifecycle import spawn_daemon
from mcp_guardian.models import ApprovalRequest, Decision, SessionInfo

log = logging.getLogger(__name__)


class GuardianClient:
    def __init__(
        self,
        home: Path,
        settings: Settings,
        role: str,
        *,
        connect_timeout: float = 10.0,
        hello_extra: dict[str, Any] | None = None,
    ) -> None:
        self.home = home
        self.settings = settings
        self.role = role
        self.connect_timeout = connect_timeout
        self.hello_extra = hello_extra or {}
        self.remote_settings: dict[str, Any] = {}
        self._channel: ipc.Channel | None = None
        self._reader_task: asyncio.Task[None] | None = None
        # rid -> (future, channel the request was sent on)
        self._pending: dict[str, tuple[asyncio.Future[Decision], ipc.Channel]] = {}
        self._sessions: dict[str, SessionInfo] = {}
        self._lock = asyncio.Lock()
        self._spawned_at = 0.0
        self._last_failure = 0.0
        self._closed = False

    # ------------------------------------------------------------------ settings

    @property
    def gated_methods(self) -> frozenset[str]:
        methods = self.remote_settings.get("gated_methods") or self.settings.gated_methods
        return frozenset(m.strip().lower() for m in methods)

    @property
    def block_style(self) -> str:
        return str(self.remote_settings.get("block_style") or self.settings.block_style)

    @property
    def connected(self) -> bool:
        return self._channel is not None and not self._channel.closed

    def _unavailable(self, why: str) -> Decision:
        allowed = self.settings.fail_mode == "open"
        detail = f"MCP Guardian daemon unavailable ({why}); fail_mode={self.settings.fail_mode}"
        return Decision(allowed, "unavailable", detail)

    # ------------------------------------------------------------------ connection

    async def connect(self) -> bool:
        """Ensure a live, authenticated connection. Returns False if impossible right now."""
        if self.connected:
            return True
        async with self._lock:
            if self.connected:
                return True
            if self._closed:
                return False
            # Give a freshly spawned daemon time to come up; after a recent failure (or
            # when we may not start it ourselves) fail fast instead of stalling every request.
            recently_failed = time.monotonic() - self._last_failure < 30
            budget = self.connect_timeout if self.settings.auto_start_daemon and not recently_failed else 1.5
            deadline = time.monotonic() + budget
            delay = 0.05
            hello = {"role": self.role, "pid": os.getpid(), **self.hello_extra}
            while True:
                try:
                    channel, welcome = await ipc.connect_authenticated(self.home, hello, timeout=2.0)
                    break
                except ipc.AuthError as exc:
                    log.error("MCP Guardian daemon rejected authentication: %s", exc)
                    self._last_failure = time.monotonic()
                    return False
                except (OSError, asyncio.TimeoutError, ipc.IpcError) as exc:
                    if self.settings.auto_start_daemon and time.monotonic() - self._spawned_at > 15:
                        log.info("MCP Guardian daemon not reachable (%s); starting it", exc)
                        try:
                            spawn_daemon(self.home)
                        except OSError as spawn_exc:
                            log.error("could not start the daemon: %s", spawn_exc)
                        self._spawned_at = time.monotonic()
                    if time.monotonic() + delay > deadline:
                        log.warning("MCP Guardian daemon unreachable: %s", exc)
                        self._last_failure = time.monotonic()
                        return False
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 0.5)
            self._channel = channel
            self.remote_settings = welcome.get("settings") or {}
            self._reader_task = asyncio.create_task(self._read_loop(channel))
            for session in self._sessions.values():  # re-announce after a daemon restart
                await channel.send({"type": "session", "session": session.to_dict()})
            log.info("connected to MCP Guardian daemon (pid %s)", welcome.get("daemon_pid"))
            return True

    async def _read_loop(self, channel: ipc.Channel) -> None:
        try:
            while (msg := await channel.recv()) is not None:
                if msg.get("type") == "decision":
                    entry = self._pending.get(str(msg.get("rid")))
                    future = entry[0] if entry is not None and entry[1] is channel else None
                    if future is not None and not future.done():
                        try:
                            future.set_result(Decision.from_dict(msg.get("decision") or {}))
                        except TypeError:
                            future.set_result(Decision.block("invalid", "malformed decision from daemon"))
        except ipc.IpcError as exc:
            log.warning("IPC error: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("IPC reader crashed")
        finally:
            if self._channel is channel:
                self._channel = None
            # Only requests sent on *this* connection are affected; resolve them before
            # awaiting anything so a request already re-sent on a new connection is untouched.
            for future, sent_on in list(self._pending.values()):
                if sent_on is channel and not future.done():
                    future.set_result(self._unavailable("connection lost while waiting for a decision"))
            await channel.close()

    async def _send(self, msg: dict[str, Any], channel: ipc.Channel | None = None) -> bool:
        channel = channel or self._channel
        if channel is None or channel.closed:
            return False
        try:
            await channel.send(msg)
            return True
        except (ConnectionError, OSError):
            return False
        except Exception:  # e.g. unserialisable payload: treat as a failed send (fail closed)
            log.exception("could not send IPC message")
            return False

    # ------------------------------------------------------------------ API

    async def request_decision(self, req: ApprovalRequest) -> Decision:
        """Ask the daemon. Cancel the calling task to withdraw the request (popup closes)."""
        if not await self.connect():
            return self._unavailable("not running and could not be started")
        channel = self._channel
        if channel is None:
            return self._unavailable("connection lost")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Decision] = loop.create_future()
        self._pending[req.request_id] = (future, channel)
        try:
            if not await self._send({"type": "approve", "rid": req.request_id, "request": req.to_dict()}, channel):
                return self._unavailable("connection lost")
            return await future
        except asyncio.CancelledError:
            await self._send({"type": "cancel", "rid": req.request_id}, channel)
            raise
        finally:
            self._pending.pop(req.request_id, None)

    async def update_session(self, session: SessionInfo) -> None:
        self._sessions[session.session_id] = session
        await self._send({"type": "session", "session": session.to_dict()})

    async def end_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        await self._send({"type": "session_end", "session_id": session_id})

    async def event(self, name: str, **fields: Any) -> None:
        """Best-effort audit event (dropped if the daemon is not connected)."""
        await self._send({"type": "event", "event": name, "fields": fields})

    async def close(self) -> None:
        self._closed = True
        for session_id in list(self._sessions):
            await self.end_session(session_id)
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._channel is not None:
            await self._channel.close()
            self._channel = None
