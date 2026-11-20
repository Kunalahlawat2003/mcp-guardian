"""stdio interception for Claude Desktop (and any other stdio MCP client).

Claude Desktop starts every local MCP server as a child process and speaks
newline-delimited JSON-RPC over its stdin/stdout. ``mcp-guardian install``
rewrites each entry of ``claude_desktop_config.json`` so Claude Desktop starts
this shim instead, with the real command after ``--``::

    Claude Desktop ──stdin/stdout──▶ shim ──stdin/stdout──▶ real MCP server
                                      │
                                      └── IPC ──▶ Guardian daemon (popup)

The shim forwards everything immediately except gated requests
(``tools/call`` etc.), which are held until the daemon answers. Held requests
are independent asyncio tasks, so several can wait at once while other
traffic (``tools/list``, pings, progress) keeps flowing.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import shutil
import signal
import sys
import threading
import queue as thread_queue
from pathlib import Path
from typing import Any, Callable

from mcp_guardian.client import GuardianClient
from mcp_guardian.config import Settings
from mcp_guardian.interceptor import SessionTracker, blocked_response
from mcp_guardian.jsonrpc import (
    INVALID_REQUEST,
    PARSE_ERROR,
    Kind,
    classify,
    encode_line,
    error_response,
    id_key,
    is_valid_request_id,
    loads_lenient,
    loads_strict,
    params_of,
)
from mcp_guardian.models import Decision, SessionInfo, new_id

log = logging.getLogger("mcp_guardian.shim")

READ_LIMIT = 8 * 1024 * 1024
_EXTRA_PATH_DIRS = ["/opt/homebrew/bin", "/usr/local/bin", "~/.local/bin", "~/.cargo/bin", "~/.bun/bin", "~/.volta/bin"]


def resolve_command(command: str, env: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Find the executable the way a shell would, with fallbacks for GUI-launched apps.

    Apps started from the Dock may get a minimal PATH. If the command is only
    found in a common tool directory, that directory is added to the child's
    PATH too (so e.g. ``npx`` can find ``node`` next to it).
    """
    if os.path.dirname(command):
        return command, env
    path = env.get("PATH", os.defpath)
    found = shutil.which(command, path=path)
    if found:
        return found, env
    extra = [os.path.expanduser(d) for d in _EXTRA_PATH_DIRS]
    extra += sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")), reverse=True)
    found = shutil.which(command, path=os.pathsep.join([path, *extra]))
    if not found:
        return command, env
    env = dict(env, PATH=os.pathsep.join([os.path.dirname(found), path, *extra]))
    return found, env


async def read_line(reader: asyncio.StreamReader) -> bytes:
    """readline() without a hard size limit (MCP messages can carry large files/images)."""
    chunks: list[bytes] = []
    while True:
        try:
            chunks.append(await reader.readuntil(b"\n"))
            return b"".join(chunks)
        except asyncio.IncompleteReadError as exc:  # EOF
            chunks.append(exc.partial)
            return b"".join(chunks)
        except asyncio.LimitOverrunError as exc:
            chunks.append(await reader.readexactly(exc.consumed))


class _StdinReader(threading.Thread):
    """Blocking reads of our stdin on a thread (portable, including Windows pipes)."""

    def __init__(self, loop: asyncio.AbstractEventLoop, out: asyncio.Queue[bytes | None]) -> None:
        super().__init__(name="shim-stdin", daemon=True)
        self._loop = loop
        self._out = out

    def _push(self, item: bytes | None) -> None:
        try:
            self._loop.call_soon_threadsafe(self._out.put_nowait, item)
        except RuntimeError:  # loop closed
            pass

    def run(self) -> None:
        # Raw os.read() instead of sys.stdin.buffer: a daemon thread blocked inside a
        # BufferedReader holds its lock and makes CPython abort at interpreter exit.
        fd = sys.stdin.fileno()
        pending = b""
        try:
            while chunk := os.read(fd, 1 << 16):
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    self._push(line + b"\n")
            if pending:
                self._push(pending)
        except (OSError, ValueError) as exc:
            log.debug("stdin closed: %s", exc)
        finally:
            self._push(None)


class _StdoutWriter(threading.Thread):
    """Serialises all writes to our stdout (one message per line, never interleaved)."""

    def __init__(self, on_broken: Callable[[], None]) -> None:
        super().__init__(name="shim-stdout", daemon=True)
        self._queue: thread_queue.Queue[bytes | None] = thread_queue.Queue()
        self._on_broken = on_broken

    def write(self, data: bytes) -> None:
        self._queue.put(data)

    def run(self) -> None:
        out = sys.stdout.buffer
        while (data := self._queue.get()) is not None:
            try:
                out.write(data)
                out.flush()
            except (OSError, ValueError):
                self._on_broken()
                return

    def close(self, timeout: float = 2.0) -> None:
        self._queue.put(None)
        self.join(timeout)


class StdioShim:
    def __init__(self, server_name: str, command: list[str], home: Path, settings: Settings) -> None:
        if not command:
            raise ValueError("no MCP server command given")
        self.command = command
        self.session = SessionInfo(
            session_id=new_id(), transport="stdio", server=server_name, command=command, interceptor_pid=os.getpid()
        )
        self.tracker = SessionTracker(self.session)
        self.client = GuardianClient(home, settings, "shim", hello_extra={"server": server_name})
        self._held: dict[str, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[Any]] = set()
        self._proc: asyncio.subprocess.Process | None = None
        self._writer: _StdoutWriter | None = None
        self._stopping: asyncio.Event | None = None

    # ------------------------------------------------------------------ main

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        self._stopping = asyncio.Event()
        exe, env = resolve_command(self.command[0], dict(os.environ))
        try:
            self._proc = await asyncio.create_subprocess_exec(
                exe, *self.command[1:], stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=None, env=env, limit=READ_LIMIT,
            )  # fmt: skip
        except OSError as exc:
            log.error("cannot start MCP server %r: %s", self.command, exc)
            return 127
        log.info("guarding MCP server %r (pid %s): %s", self.session.server, self._proc.pid, " ".join(self.command))

        stopping = self._stopping
        self._writer = _StdoutWriter(on_broken=lambda: loop.call_soon_threadsafe(stopping.set))
        self._writer.start()
        stdin_queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        _StdinReader(loop, stdin_queue).start()
        if os.name != "nt":
            for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                loop.add_signal_handler(sig, stopping.set)

        self._spawn(self._announce())
        client_side = asyncio.create_task(self._client_to_server(stdin_queue))
        server_side = asyncio.create_task(self._server_to_client())
        child_exit = asyncio.create_task(self._proc.wait())
        stop_wait = asyncio.create_task(stopping.wait())
        await asyncio.wait({client_side, child_exit, stop_wait}, return_when=asyncio.FIRST_COMPLETED)

        # ---- shutdown: stop accepting requests, withdraw popups, let the server exit
        # cleanly, then drain whatever it still writes.
        client_side.cancel()
        stop_wait.cancel()
        for task in list(self._held.values()):
            task.cancel()
        if not child_exit.done():
            await self._stop_child(child_exit)
        try:
            await asyncio.wait_for(server_side, 5)
        except (asyncio.TimeoutError, Exception):
            server_side.cancel()
        self._writer.close()
        await self.client.close()
        for task in list(self._background):
            task.cancel()
        code = self._proc.returncode or 0
        return code if code >= 0 else 128 - code

    async def _stop_child(self, child_exit: asyncio.Task[int]) -> None:
        """MCP stdio shutdown: close stdin, then SIGTERM, then SIGKILL."""
        assert self._proc is not None
        try:
            if self._proc.stdin is not None:
                self._proc.stdin.close()
            await asyncio.wait_for(asyncio.shield(child_exit), 5)
            return
        except asyncio.TimeoutError:
            pass
        except Exception:
            pass
        try:
            self._proc.terminate()
            await asyncio.wait_for(asyncio.shield(child_exit), 3)
        except ProcessLookupError:
            pass
        except asyncio.TimeoutError:
            self._proc.kill()
            await child_exit

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _announce(self) -> None:
        if await self.client.connect():
            await self.client.update_session(self.session)

    # ------------------------------------------------------------------ client -> server

    async def _client_to_server(self, lines: asyncio.Queue[bytes | None]) -> None:
        while (line := await lines.get()) is not None:
            if not line.strip():
                continue
            try:
                await self._handle_client_line(line)
            except Exception:  # one bad message must not take the whole connection down
                log.exception("error while handling a client message (not forwarded)")
                self._reply(error_response(None, INVALID_REQUEST, "MCP Guardian: message could not be processed, not forwarded"))

    async def _handle_client_line(self, line: bytes) -> None:
        try:
            msg = loads_strict(line)
        except ValueError as exc:
            # Never forward what we could not parse: the server might parse it differently.
            self._reply(error_response(None, PARSE_ERROR, f"MCP Guardian: message rejected, not valid JSON ({exc})"))
            self._spawn(self.client.event("rejected_message", server=self.session.server, error=str(exc)))
            return
        if isinstance(msg, list):  # JSON-RPC batch (MCP 2025-03-26 only): inspect each element
            if not msg:
                self._reply(error_response(None, INVALID_REQUEST, "MCP Guardian: empty batch"))
            for item in msg:
                await self._handle_message(item)
            return
        await self._handle_message(msg)

    async def _handle_message(self, msg: Any) -> None:
        kind = classify(msg)
        if kind is Kind.REQUEST:
            if self.tracker.observe_client_request(msg):
                self._spawn(self.client.update_session(self.session))
            key = id_key(msg["id"])
            if key in self._held:
                self._reply(error_response(msg["id"], INVALID_REQUEST, "MCP Guardian: duplicate id of a request awaiting approval"))
                return
            if self.tracker.is_gated(msg["method"], self.client.gated_methods):
                if self.tracker.is_approved_continuation(msg):
                    self._spawn(self.client.event("continuation_allowed", server=self.session.server, method=msg["method"]))
                    self.tracker.track_forwarded(msg, approved=True)
                    await self._to_server(msg)
                    return
                self._held[key] = asyncio.create_task(self._gate(msg, key))
                return
            self.tracker.track_forwarded(msg, approved=False)
            await self._to_server(msg)
        elif kind is Kind.NOTIFICATION:
            if self.tracker.is_gated(msg["method"], self.client.gated_methods):
                # e.g. tools/call without an id: no answer is possible, and a lax
                # JSON-RPC server might still execute it. Never forward.
                log.warning("dropped gated method %r sent as a notification", msg["method"])
                self._spawn(self.client.event("rejected_message", server=self.session.server,
                                              error=f"gated method {msg['method']} sent without an id"))  # fmt: skip
                return
            if msg["method"] == "notifications/cancelled":
                request_id = params_of(msg).get("requestId")
                held = self._held.get(id_key(request_id)) if is_valid_request_id(request_id) else None
                if held is not None:
                    held.cancel()  # popup is withdrawn; the server never saw the request
                    return
            await self._to_server(msg)
        elif kind is Kind.RESPONSE:
            await self._to_server(msg)  # answers to server->client requests (sampling, roots, elicitation)
        else:
            if isinstance(msg, dict) and "method" in msg:
                rid = msg.get("id") if is_valid_request_id(msg.get("id")) else None
                if "id" in msg:
                    self._reply(error_response(rid, INVALID_REQUEST, "MCP Guardian: invalid JSON-RPC request, not forwarded"))
            log.warning("dropped invalid JSON-RPC message from client")

    async def _gate(self, msg: dict[str, Any], key: str) -> None:
        try:
            req = self.tracker.build_request(msg)
            try:
                decision = await self.client.request_decision(req)
            finally:
                self._held.pop(key, None)
            if decision.allowed:
                self.tracker.track_forwarded(msg, approved=True, guardian_id=req.request_id)
                await self._to_server(msg)
                return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never leave the client hanging, never fail open
            log.exception("error while gating a request")
            decision = Decision.block("error", f"{type(exc).__name__}: {exc}")
        self._reply(blocked_response(msg, decision, self.client.block_style))

    async def _to_server(self, msg: Any) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        try:
            self._proc.stdin.write(encode_line(msg))
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            log.warning("MCP server stdin closed: %s", exc)

    def _reply(self, msg: dict[str, Any]) -> None:
        assert self._writer is not None
        self._writer.write(encode_line(msg))

    # ------------------------------------------------------------------ server -> client

    async def _server_to_client(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None and self._writer is not None
        while line := await read_line(self._proc.stdout):
            self._writer.write(line if line.endswith(b"\n") else line + b"\n")
            self._observe_server(line)

    def _observe_server(self, line: bytes) -> None:
        try:
            msg = loads_lenient(line)
        except ValueError:
            return  # e.g. a server printing logs to stdout; forwarded untouched
        for item in msg if isinstance(msg, list) else [msg]:
            try:
                record, changed = self.tracker.observe_server_message(item)
            except Exception:
                log.exception("error while observing server message")
                continue
            if changed:
                self._spawn(self.client.update_session(self.session))
            if record is not None:
                self._spawn(self.client.event("completed", **record))


def run_shim(server_name: str, command: list[str], home: Path, settings: Settings) -> int:
    logging.basicConfig(
        level=os.environ.get("MCP_GUARDIAN_LOG_LEVEL", "INFO").upper(),
        format=f"[mcp-guardian shim:{server_name}] %(levelname)s %(message)s",
        stream=sys.stderr,  # stdout carries the MCP protocol; logs go to Claude's mcp-server-*.log
    )
    try:
        return asyncio.run(StdioShim(server_name, command, home, settings).run())
    except KeyboardInterrupt:
        return 130
