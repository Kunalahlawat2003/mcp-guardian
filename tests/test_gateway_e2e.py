"""End-to-end: MCP client -(Streamable HTTP)-> gateway -> real remote MCP server (demo, HTTP mode)."""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import aiohttp
import pytest
from aiohttp import web

from mcp_guardian.config import GatewayConfig, Settings, UpstreamConfig
from mcp_guardian.gateway import Gateway, SSEObserver
from mcp_guardian.jsonrpc import GUARDIAN_BLOCKED

from conftest import ScriptedUI, wait_until

pytest.importorskip("mcp")
from mcp import Client  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def upstream(child_env):
    port = free_port()
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "mcp_guardian.demo.server", "--transport", "http", "--port", str(port),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=child_env,
    )  # fmt: skip
    url = f"http://127.0.0.1:{port}/mcp"
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            await asyncio.sleep(0.1)
    yield url
    proc.terminate()
    await proc.wait()


@pytest.fixture
async def gateway(daemon, home: Path, upstream: str):
    settings = Settings(auto_start_daemon=False)
    config = GatewayConfig(host="127.0.0.1", port=free_port(), secret="test-secret-0123456789",
                           upstreams={"demo": UpstreamConfig(name="demo", url=upstream)})  # fmt: skip
    gw = Gateway(settings, home, config)
    runner = web.AppRunner(gw.make_app(), handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, config.host, config.port).start()
    yield gw, gw.connector_urls()["demo"]
    await runner.cleanup()


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_remote_connector_allow_and_block(gateway, ui: ScriptedUI, home: Path, mode: str):
    _, url = gateway
    ui.script = lambda req: "block" if req.target == "run_shell_command" else "allow"
    async with Client(url, mode=mode) as client:
        tools = await client.list_tools()
        assert "run_shell_command" in {t.name for t in tools.tools} and ui.shown == []
        result = await client.call_tool("get_weather", {"city": "Delhi"})
        assert result.content[0].text.startswith("Delhi")
        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("run_shell_command", {"command": "rm -rf ~"})
        assert exc_info.value.error.code == GUARDIAN_BLOCKED
    req = ui.shown[-1]
    assert req.session.transport == "http" and req.session.server == "demo"
    assert req.session.upstream_url.endswith("/mcp") and req.risk == "destructive"


async def test_remote_concurrent_calls(gateway, ui: ScriptedUI):
    _, url = gateway
    delays = {"a": 0.6, "b": 0.3, "c": 0.05}
    ui.script = lambda req: "block" if req.arguments["name"] == "b" else "allow"
    ui.delay = lambda req: delays[req.arguments["name"]]
    async with Client(url, mode="legacy") as client:
        async def call(name: str):
            try:
                return name, (await client.call_tool("write_note", {"name": name, "content": name})).content[0].text
            except MCPError as exc:
                return name, exc.error.code

        results = dict(await asyncio.gather(*(call(n) for n in delays)))
    assert results["b"] == GUARDIAN_BLOCKED
    assert "saved" in results["a"] and "saved" in results["c"]


async def test_wrong_secret_or_upstream_is_404(gateway):
    gw, url = gateway
    base = url.rsplit("/", 3)[0]
    async with aiohttp.ClientSession() as http:
        for bad in (f"{base}/mcp/demo/wrong-secret", f"{base}/mcp/other/{gw.secret}", f"{base}/mcp/demo"):
            async with http.post(bad, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}) as resp:
                assert resp.status == 404


HELD_CALL = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"city": "X"},
             "_meta": {"io.modelcontextprotocol/clientInfo": {"name": "claude-ai", "version": "1"}}}}  # fmt: skip


async def test_client_disconnect_withdraws_popup_json_client(gateway, ui: ScriptedUI):
    _, url = gateway
    ui.script = lambda req: "hold"
    async with aiohttp.ClientSession() as http:
        with pytest.raises(asyncio.TimeoutError):  # JSON-only client: nothing is sent until a decision
            async with http.post(url, json=HELD_CALL, headers={"Accept": "application/json"},
                                 timeout=aiohttp.ClientTimeout(total=0.7)):  # fmt: skip
                pass
    await wait_until(lambda: len(ui.dismissed) == 1)
    assert ui.shown[0].session.source_label.startswith("Claude")


async def test_sse_client_gets_keepalives_and_disconnect_withdraws_popup(gateway, ui: ScriptedUI):
    gw, url = gateway
    gw.keepalive_seconds = 0.2
    ui.script = lambda req: "hold"
    async with aiohttp.ClientSession() as http:
        async with http.post(url, json=HELD_CALL, headers={"Accept": "application/json, text/event-stream"}) as resp:
            # Headers arrive immediately, so tunnels/CDNs don't time out while the user decides.
            assert resp.status == 200 and resp.headers["Content-Type"].startswith("text/event-stream")
            line = await asyncio.wait_for(resp.content.readline(), 2)
            assert line.startswith(b": mcp-guardian")
    await wait_until(lambda: len(ui.dismissed) == 1)


async def _raw_session(http: aiohttp.ClientSession, url: str) -> dict[str, str]:
    """Legacy initialize handshake over raw HTTP; returns headers for follow-up requests."""
    accept = {"Accept": "application/json, text/event-stream"}
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-ai", "version": "1"}}}  # fmt: skip
    async with http.post(url, json=init, headers=accept) as resp:
        assert resp.status == 200, await resp.text()
        headers = {**accept, "Mcp-Session-Id": resp.headers["Mcp-Session-Id"], "MCP-Protocol-Version": "2025-06-18"}
        await resp.read()
    async with http.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers=headers) as resp:
        assert resp.status == 202
    return headers


async def test_sse_client_allowed_after_wait(gateway, ui: ScriptedUI):
    gw, url = gateway
    gw.keepalive_seconds = 0.1
    ui.script = lambda req: "allow"
    ui.delay = lambda req: 0.35
    call = {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"city": "Pune"}}}
    async with aiohttp.ClientSession() as http:
        headers = await _raw_session(http, url)
        async with http.post(url, json=call, headers=headers) as resp:
            assert resp.headers["Content-Type"].startswith("text/event-stream")
            body = (await resp.read()).decode()
    assert ": mcp-guardian" in body  # keep-alives were sent while waiting
    assert '"id":5' in body.replace(" ", "") and "Pune:" in body  # then the real upstream result
    assert ui.shown[0].session.client_name == "claude-ai"  # identity learned from initialize, same session


async def test_large_json_response_is_not_truncated(daemon, home: Path, ui: ScriptedUI):
    """Upstream sends a big JSON body in chunks: the gateway must relay all of it."""
    big = {"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": f"t{i}", "description": "x" * 400,
                                                             "inputSchema": {"type": "object"}} for i in range(1200)]}}  # fmt: skip
    payload = __import__("json").dumps(big).encode()

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "application/json"})
        await resp.prepare(request)
        for i in range(0, len(payload), 7000):
            await resp.write(payload[i : i + 7000])
            await asyncio.sleep(0)
        await resp.write_eof()
        return resp

    up_app = web.Application()
    up_app.router.add_post("/mcp", handler)
    up_runner = web.AppRunner(up_app)
    await up_runner.setup()
    up_port = free_port()
    await web.TCPSite(up_runner, "127.0.0.1", up_port).start()
    config = GatewayConfig(port=free_port(), secret="test-secret-0123456789",
                           upstreams={"big": UpstreamConfig("big", f"http://127.0.0.1:{up_port}/mcp")})  # fmt: skip
    gw = Gateway(Settings(auto_start_daemon=False), home, config)
    runner = web.AppRunner(gw.make_app(), handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", config.port).start()
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(gw.connector_urls()["big"], json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                 headers={"Accept": "application/json, text/event-stream"}) as resp:  # fmt: skip
                assert await resp.read() == payload
    finally:
        await runner.cleanup()
        await up_runner.cleanup()


async def test_sessions_are_isolated(gateway, ui: ScriptedUI):
    """Two claude.ai conversations must not share identity, ids or cancellations."""
    gw, url = gateway
    ui.script = lambda req: "allow"
    async with Client(url, mode="legacy") as a, Client(url, mode="legacy") as b:
        await asyncio.gather(a.call_tool("get_weather", {"city": "A"}), b.call_tool("get_weather", {"city": "B"}))
        assert len(gw._sessions) == 2  # one tracker per Mcp-Session-Id, nothing shared
        assert len({id(sess) for sess in gw._sessions.values()}) == 2
        assert ("demo", "") not in gw._sessions
    assert len({r.session.session_id for r in ui.shown}) == 2
    assert gw._sessions == {}  # DELETE on close ends both sessions


async def test_invalid_json_rejected(gateway):
    _, url = gateway
    async with aiohttp.ClientSession() as http:
        async with http.post(url, data=b'{"a":1,"a":2}', headers={"Content-Type": "application/json"}) as resp:
            assert resp.status == 400 and (await resp.json())["error"]["code"] == -32700


def test_sse_observer_handles_split_chunks():
    observer = SSEObserver()
    assert observer.feed(b'event: message\ndata: {"jsonrpc":"2.0",') == []
    assert observer.feed(b'"id":1,"result":{}}\n') == []
    assert observer.feed(b"\n") == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
