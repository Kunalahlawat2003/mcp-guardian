"""The Guardian daemon: the single decision point for every interceptor.

* Accepts authenticated IPC connections from stdio shims (Claude Desktop),
  the HTTP gateway (claude.ai connectors) and the CLI.
* Runs the :class:`ApprovalBroker` (rules -> popup -> timeout).
* Publishes events to the audit log and to ``mcp-guardian monitor``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_guardian import __version__, ipc, paths
from mcp_guardian.audit import AuditLog, EventHub
from mcp_guardian.broker import ApprovalBroker
from mcp_guardian.config import Settings
from mcp_guardian.lifecycle import SingleInstanceLock
from mcp_guardian.models import ApprovalRequest, Decision, SessionInfo, new_id
from mcp_guardian.policy import PolicyEngine
from mcp_guardian.ui import PromptUI, create_ui

log = logging.getLogger(__name__)


@dataclass
class _Connection:
    conn_id: str
    role: str
    pid: int | None
    channel: ipc.Channel
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    sessions: set[str] = field(default_factory=set)
    connected_at: float = field(default_factory=time.time)


class GuardianDaemon:
    def __init__(self, settings: Settings, home: Path, ui: PromptUI, *, audit: bool = True) -> None:
        self.settings = settings
        self.home = home
        self.ui = ui
        audit_log = AuditLog(paths.audit_log_path(home)) if audit else None
        self.events = EventHub(audit_log, settings.audit_arguments)
        self.policy = PolicyEngine(settings.rules)
        self.broker = ApprovalBroker(settings, self.policy, self.events, ui)
        self.connections: dict[str, _Connection] = {}
        self.sessions: dict[str, SessionInfo] = {}
        self.started_at = time.time()
        self.ready = threading.Event()
        self._notified: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._stop_requested = False
        self._token = b""

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        if self._stop_requested:
            return
        paths.ensure_private_dir(self.home)
        self._token = ipc.load_or_create_token(self.home)
        server = await ipc.start_server(self.home, self._on_connect)
        self.events.emit("daemon_started", pid=os.getpid(), version=__version__, ui=type(self.ui).__name__)
        log.info("MCP Guardian daemon %s listening (pid %s, ui=%s)", __version__, os.getpid(), type(self.ui).__name__)
        self.ready.set()
        try:
            await self._stop.wait()
        finally:
            server.close()
            for conn in list(self.connections.values()):
                for task in conn.tasks.values():
                    task.cancel()
                await conn.channel.close()
            ipc.cleanup_endpoint(self.home)
            self.events.emit("daemon_stopped", pid=os.getpid())
            log.info("daemon stopped")

    def request_stop(self) -> None:
        """Thread-safe and signal-safe."""
        self._stop_requested = True
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass

    # ------------------------------------------------------------------ connections

    async def _on_connect(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        channel = ipc.Channel(reader, writer)
        welcome = {"settings": self.settings.public_view(), "daemon_pid": os.getpid(), "version": __version__}
        try:
            hello = await ipc.server_handshake(channel, self._token, welcome)
        except (ipc.IpcError, asyncio.TimeoutError, ConnectionError, OSError) as exc:
            log.warning("rejected IPC client: %s", exc)
            self.events.emit("ipc_rejected", error=str(exc))
            await channel.close()
            return

        conn = _Connection(new_id(), str(hello.get("role", "?")), hello.get("pid"), channel)
        self.connections[conn.conn_id] = conn
        try:
            if conn.role in ("shim", "gateway"):
                await self._serve_interceptor(conn)
            elif conn.role == "cli":
                await self._serve_cli(conn, hello)
            else:
                await channel.send({"type": "error", "error": f"unknown role {conn.role!r}"})
        except ipc.IpcError as exc:
            log.warning("IPC protocol error from %s pid %s: %s", conn.role, conn.pid, exc)
        except (ConnectionError, OSError):
            pass
        finally:
            for task in list(conn.tasks.values()):
                task.cancel()  # withdraws the popups of a crashed/closed interceptor
            for session_id in conn.sessions:
                session = self.sessions.pop(session_id, None)
                if session is not None:
                    self.events.emit("session_ended", session_id=session_id, server=session.server, transport=session.transport)
            self.connections.pop(conn.conn_id, None)
            await channel.close()

    async def _serve_interceptor(self, conn: _Connection) -> None:
        while (msg := await conn.channel.recv()) is not None:
            kind = msg["type"]
            if kind == "approve":
                rid = str(msg.get("rid") or new_id())
                try:
                    req = ApprovalRequest.from_dict(msg.get("request") or {})
                except TypeError as exc:
                    await conn.channel.send({"type": "decision", "rid": rid,
                                             "decision": Decision.block("invalid", f"bad request: {exc}").to_dict()})  # fmt: skip
                    continue
                req.request_id = rid
                task = asyncio.create_task(self._decide(conn, rid, req))
                conn.tasks[rid] = task
                task.add_done_callback(lambda _t, rid=rid: conn.tasks.pop(rid, None))
            elif kind == "cancel":
                task = conn.tasks.get(str(msg.get("rid")))
                if task is not None:
                    task.cancel()
            elif kind == "session":
                self._upsert_session(conn, msg.get("session") or {})
            elif kind == "session_end":
                session_id = str(msg.get("session_id"))
                conn.sessions.discard(session_id)
                session = self.sessions.pop(session_id, None)
                if session is not None:
                    self.events.emit("session_ended", session_id=session_id, server=session.server, transport=session.transport)
            elif kind == "event":
                fields = {k: v for k, v in (msg.get("fields") or {}).items() if k not in ("ts", "event")}
                self.events.emit(str(msg.get("event") or "interceptor_event"), **fields)
            elif kind == "ping":
                await conn.channel.send({"type": "pong"})

    async def _decide(self, conn: _Connection, rid: str, req: ApprovalRequest) -> None:
        try:
            decision = await self.broker.evaluate(req)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # fail closed on internal errors
            log.exception("error while evaluating %s", rid)
            decision = Decision.block("error", f"internal error in MCP Guardian: {exc}")
        try:
            await conn.channel.send({"type": "decision", "rid": rid, "decision": decision.to_dict()})
        except (ConnectionError, OSError):
            pass

    def _upsert_session(self, conn: _Connection, data: dict[str, Any]) -> None:
        try:
            info = SessionInfo.from_dict(data)
        except TypeError:
            return
        is_new = info.session_id not in self.sessions
        self.sessions[info.session_id] = info
        conn.sessions.add(info.session_id)
        if is_new:
            self.events.emit("session_started", session=info.to_dict(), source=info.source_label)
        if self.settings.notify_new_sessions and info.client_name and info.session_id not in self._notified:
            self._notified.add(info.session_id)
            self.events.emit("session_identified", session_id=info.session_id, source=info.source_label,
                             client=info.client_label, server=info.server)  # fmt: skip
            self.ui.notify("New MCP connection", f"{info.source_label} → {info.server_label} ({info.transport})")

    # ------------------------------------------------------------------ CLI

    async def _serve_cli(self, conn: _Connection, hello: dict[str, Any]) -> None:
        command = hello.get("command")
        if command == "status":
            await conn.channel.send({"type": "status", **self.status()})
        elif command == "shutdown":
            await conn.channel.send({"type": "ok"})
            self.request_stop()
        elif command == "forget":
            await conn.channel.send({"type": "ok", "forgotten": self.policy.forget_all()})
        elif command == "subscribe":
            queue = self.events.subscribe()
            closed = asyncio.create_task(conn.channel.recv())
            try:
                for event in list(self.events.recent)[-int(hello.get("history", 20)):]:
                    await conn.channel.send({"type": "event", "event": event})
                while not closed.done():
                    getter = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait({getter, closed}, return_when=asyncio.FIRST_COMPLETED)
                    if getter in done:
                        await conn.channel.send({"type": "event", "event": getter.result()})
                    else:
                        getter.cancel()
            finally:
                closed.cancel()
                self.events.unsubscribe(queue)
        else:
            await conn.channel.send({"type": "error", "error": f"unknown command {command!r}"})

    def status(self) -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "version": __version__,
            "uptime_s": round(time.time() - self.started_at, 1),
            "ui": type(self.ui).__name__,
            "settings": self.settings.public_view(),
            "connections": [
                {"role": c.role, "pid": c.pid, "sessions": len(c.sessions), "pending": len(c.tasks)}
                for c in self.connections.values()
                if c.role != "cli"
            ],
            "sessions": [
                {**s.to_dict(), "source": s.source_label} for s in self.sessions.values()
            ],
            "pending": self.broker.pending(),
            "remembered": [r.describe() for r in self.policy.remembered],
            "rules": [r.describe() for r in self.policy.rules],
            "recent": list(self.events.recent)[-25:],
        }


def run_daemon(settings: Settings, home: Path, ui_kind: str | None = None) -> int:
    """Run the daemon in the current process until SIGINT/SIGTERM or `mcp-guardian stop`."""
    paths.ensure_private_dir(home)
    paths.log_dir(home).mkdir(parents=True, exist_ok=True)
    lock = SingleInstanceLock(paths.lock_path(home))
    if not lock.acquire():
        log.info("another MCP Guardian daemon is already running for %s", home)
        return 0
    try:
        ui = create_ui(ui_kind or settings.ui, arm_delay=settings.arm_delay)
        daemon = GuardianDaemon(settings, home, ui)
        paths.pid_path(home).write_text(str(os.getpid()))

        def _on_signal(signum: int, _frame: Any) -> None:
            log.info("received signal %s, shutting down", signum)
            daemon.request_stop()

        signal.signal(signal.SIGINT, _on_signal)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, _on_signal)

        if ui.needs_main_thread:
            # Tk owns the main thread; asyncio runs beside it.
            stop = threading.Event()

            def _asyncio_thread() -> None:
                try:
                    asyncio.run(daemon.run())
                except Exception:
                    log.exception("daemon loop crashed")
                finally:
                    stop.set()

            worker = threading.Thread(target=_asyncio_thread, name="guardian-asyncio", daemon=True)
            worker.start()
            try:
                ui.run_mainloop(stop)
            except Exception:
                log.exception("UI crashed; stopping daemon (requests will fail closed)")
            daemon.request_stop()
            worker.join(timeout=5)
        else:
            try:
                asyncio.run(daemon.run())
            finally:
                ui.close()
        return 0
    finally:
        try:
            if paths.pid_path(home).read_text().strip() == str(os.getpid()):
                paths.pid_path(home).unlink()
        except OSError:
            pass
        lock.release()
