"""Shared fixtures: an in-process daemon with a scripted 'user' instead of a popup."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

import pytest

from mcp_guardian.config import Settings
from mcp_guardian.daemon import GuardianDaemon
from mcp_guardian.jsonrpc import encode_line
from mcp_guardian.models import ApprovalRequest, Decision
from mcp_guardian.ui.base import PromptUI, Resolver


class ScriptedUI(PromptUI):
    """Plays the user. ``script(req)`` returns allow | block | allow! | block! | hold."""

    def __init__(self) -> None:
        self.shown: list[ApprovalRequest] = []
        self.dismissed: dict[str, str] = {}
        self.notifications: list[tuple[str, str]] = []
        self.resolvers: dict[str, Resolver] = {}
        self.script: Callable[[ApprovalRequest], str] = lambda req: "allow"
        self.delay: Callable[[ApprovalRequest], float] = lambda req: 0.05

    def show(self, req: ApprovalRequest, deadline: float | None, resolve: Resolver) -> None:
        self.shown.append(req)
        self.resolvers[req.request_id] = resolve
        action = self.script(req)
        if action == "hold":
            return
        decision = Decision(action.startswith("allow"), "user", "scripted", remember=action.endswith("!"))
        asyncio.get_running_loop().call_later(self.delay(req), resolve, decision)

    def dismiss(self, request_id: str, message: str) -> None:
        self.dismissed[request_id] = message

    def notify(self, title: str, message: str) -> None:
        self.notifications.append((title, message))

    def targets(self) -> list[str | None]:
        return [r.target for r in self.shown]


@pytest.fixture
def home() -> Path:
    # AF_UNIX paths are limited to ~104 bytes on macOS, so keep this short on POSIX.
    path = Path(tempfile.mkdtemp(prefix="mg-", dir="/tmp" if os.name != "nt" else None))
    # Interceptors read this; tests must never spawn a real (popup) daemon.
    (path / "config.toml").write_text("auto_start_daemon = false\napproval_timeout = 20\n")
    notes = path / "notes"
    notes.mkdir()
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def child_env(home: Path) -> dict[str, str]:
    return dict(os.environ, MCP_GUARDIAN_HOME=str(home), MCP_GUARDIAN_DEMO_NOTES=str(home / "notes"))


@pytest.fixture
def ui() -> ScriptedUI:
    return ScriptedUI()


@pytest.fixture
def settings() -> Settings:
    return Settings(approval_timeout=20, auto_start_daemon=False, notify_new_sessions=True)


@pytest.fixture
async def daemon(home: Path, ui: ScriptedUI, settings: Settings):
    d = GuardianDaemon(settings, home, ui)
    task = asyncio.create_task(d.run())
    for _ in range(200):
        if d.ready.is_set():
            break
        await asyncio.sleep(0.01)
    assert d.ready.is_set(), "daemon did not start"
    yield d
    d.request_stop()
    await asyncio.wait_for(task, 5)


def shim_command(server: str = "demo") -> list[str]:
    return [sys.executable, "-m", "mcp_guardian", "shim", "--server", server, "--",
            sys.executable, "-m", "mcp_guardian.demo.server"]  # fmt: skip


class RawStdioClient:
    """Speaks raw JSON-RPC lines to a shim process (for precise edge-case tests)."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self.inbox: list[dict[str, Any]] = []

    @classmethod
    async def start(cls, env: dict[str, str], home: Path, server: str = "demo") -> RawStdioClient:
        stderr = open(home / f"shim-{server}.log", "ab")
        proc = await asyncio.create_subprocess_exec(
            *shim_command(server), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=stderr, env=env, limit=16 * 1024 * 1024,
        )  # fmt: skip
        stderr.close()
        return cls(proc)

    async def send(self, msg: Any) -> None:
        data = msg if isinstance(msg, bytes) else encode_line(msg)
        self.proc.stdin.write(data)
        await self.proc.stdin.drain()

    async def recv(self, timeout: float = 10.0) -> dict[str, Any]:
        line = await asyncio.wait_for(self.proc.stdout.readline(), timeout)
        assert line, "shim closed stdout"
        return json.loads(line)

    async def response(self, request_id: Any, timeout: float = 15.0) -> dict[str, Any]:
        """Wait for the response with this id (buffering others)."""
        for i, msg in enumerate(self.inbox):
            if msg.get("id") == request_id and "method" not in msg:
                return self.inbox.pop(i)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            msg = await self.recv(max(0.1, deadline - asyncio.get_running_loop().time()))
            if msg.get("id") == request_id and "method" not in msg:
                return msg
            self.inbox.append(msg)

    async def initialize(self) -> dict[str, Any]:
        await self.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "claude-ai", "version": "0.1.0"}}})  # fmt: skip
        result = await self.response(0)
        await self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    async def call_tool(self, request_id: Any, name: str, arguments: dict[str, Any] | None = None) -> None:
        await self.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments or {}}})  # fmt: skip

    async def close(self) -> int:
        if self.proc.stdin and not self.proc.stdin.is_closing():
            self.proc.stdin.close()
        try:
            return await asyncio.wait_for(self.proc.wait(), 10)
        except asyncio.TimeoutError:
            self.proc.kill()
            return await self.proc.wait()


async def wait_until(predicate: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)
