"""End-to-end: MCP client -> shim process -> real MCP server process, decisions from the daemon."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from mcp_guardian.jsonrpc import GUARDIAN_BLOCKED, INVALID_REQUEST, PARSE_ERROR

from conftest import RawStdioClient, ScriptedUI, shim_command, wait_until

mcp = pytest.importorskip("mcp")
from mcp import Client, StdioServerParameters  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402


def sdk_params(env: dict[str, str]) -> StdioServerParameters:
    cmd = shim_command()
    return StdioServerParameters(command=cmd[0], args=cmd[1:], env=env)


@pytest.mark.parametrize("mode", ["legacy", "auto"])
async def test_official_client_allow_and_block(daemon, ui: ScriptedUI, child_env, home: Path, mode: str):
    """Works for the initialize handshake (legacy) and whatever the SDK negotiates by default."""
    ui.script = lambda req: "block" if req.target == "delete_note" else "allow"
    async with Client(sdk_params(child_env), mode=mode) as client:
        tools = await client.list_tools()  # not gated: no popup
        assert {"get_weather", "delete_note", "run_shell_command"} <= {t.name for t in tools.tools}
        assert ui.shown == []

        result = await client.call_tool("write_note", {"name": "todo", "content": "buy milk"})
        assert not result.is_error and "saved" in result.content[0].text
        assert (home / "notes" / "todo.txt").read_text() == "buy milk"

        with pytest.raises(MCPError) as exc_info:
            await client.call_tool("delete_note", {"name": "todo"})
        assert exc_info.value.error.code == GUARDIAN_BLOCKED
        assert (home / "notes" / "todo.txt").exists(), "blocked call must not execute"

    # The popup saw who asked, which server, which tool, the arguments and the tool's risk hints.
    first = ui.shown[0]
    assert first.session.source_label == "Local stdio client" or first.session.client_name
    assert first.session.server == "demo" and first.target == "write_note"
    assert first.arguments == {"name": "todo", "content": "buy milk"}
    assert first.tool and first.tool["description"].startswith("Create or overwrite")
    assert ui.shown[1].risk == "destructive"


async def test_concurrent_calls_decided_out_of_order(daemon, ui: ScriptedUI, child_env):
    """Several tool calls wait at once; decisions arrive in reverse order; responses stay matched."""
    delays = {"Paris": 0.9, "Tokyo": 0.6, "Lima": 0.3, "Oslo": 0.05}
    ui.script = lambda req: "block" if req.arguments.get("city") == "Tokyo" else "allow"
    ui.delay = lambda req: delays[req.arguments["city"]]
    async with Client(sdk_params(child_env), mode="legacy") as client:
        async def call(city: str):
            try:
                result = await client.call_tool("get_weather", {"city": city})
                return city, result.content[0].text
            except MCPError as exc:
                return city, exc.error.code

        results = dict(await asyncio.gather(*(call(c) for c in delays)))
    assert results["Tokyo"] == GUARDIAN_BLOCKED
    for city in ("Paris", "Lima", "Oslo"):
        assert results[city].startswith(city)
    assert len(ui.shown) == 4


async def test_other_traffic_flows_while_a_call_waits(daemon, ui: ScriptedUI, child_env, home: Path):
    ui.script = lambda req: "hold"
    client = await RawStdioClient.start(child_env, home)
    await client.initialize()
    await client.call_tool(10, "get_weather", {"city": "Rome"})
    await wait_until(lambda: len(ui.shown) == 1)
    await client.send({"jsonrpc": "2.0", "id": 11, "method": "tools/list"})
    listed = await client.response(11)
    assert any(t["name"] == "get_weather" for t in listed["result"]["tools"])
    await client.send({"jsonrpc": "2.0", "id": 12, "method": "ping"})
    assert (await client.response(12))["result"] == {}
    from mcp_guardian.models import Decision

    ui.resolvers[ui.shown[0].request_id](Decision.allow("user"))
    reply = await client.response(10)
    assert reply["result"]["content"][0]["text"].startswith("Rome")
    assert await client.close() == 0


async def test_client_cancellation_withdraws_popup(daemon, ui: ScriptedUI, child_env, home: Path):
    ui.script = lambda req: "hold"
    client = await RawStdioClient.start(child_env, home)
    await client.initialize()
    await client.call_tool("abc", "delete_note", {"name": "x"})
    await wait_until(lambda: len(ui.shown) == 1)
    await client.send({"jsonrpc": "2.0", "method": "notifications/cancelled",
                       "params": {"requestId": "abc", "reason": "user stopped"}})  # fmt: skip
    rid = ui.shown[0].request_id
    await wait_until(lambda: rid in ui.dismissed)
    assert "cancelled" in ui.dismissed[rid].lower()
    # no response is sent for a cancelled request; the connection keeps working
    await client.send({"jsonrpc": "2.0", "id": 99, "method": "ping"})
    assert (await client.response(99))["result"] == {}
    assert all(m.get("id") != "abc" for m in client.inbox)
    await client.close()


async def test_malformed_and_smuggled_messages_are_not_forwarded(daemon, ui: ScriptedUI, child_env, home: Path):
    client = await RawStdioClient.start(child_env, home)
    await client.initialize()
    await client.send(b"this is not json\n")
    assert (await client.recv())["error"]["code"] == PARSE_ERROR
    # duplicate keys could make the server see a different method than we inspected
    await client.send(b'{"jsonrpc":"2.0","id":5,"method":"tools/list","method":"tools/call","params":{"name":"delete_note","arguments":{"name":"x"}}}\n')
    assert (await client.recv())["error"]["code"] == PARSE_ERROR
    # null id requests are invalid in MCP and are refused rather than forwarded unchecked
    await client.send({"jsonrpc": "2.0", "id": None, "method": "tools/call", "params": {"name": "delete_note"}})
    assert (await client.recv())["error"]["code"] == INVALID_REQUEST
    # case games on the method name are still gated
    ui.script = lambda req: "block"
    await client.send({"jsonrpc": "2.0", "id": 6, "method": "TOOLS/CALL", "params": {"name": "delete_note"}})
    assert (await client.response(6))["error"]["code"] == GUARDIAN_BLOCKED
    assert ui.shown and ui.shown[0].method == "TOOLS/CALL"
    await client.close()


async def test_batch_elements_are_gated_individually(daemon, ui: ScriptedUI, child_env, home: Path):
    ui.script = lambda req: "allow" if req.target == "get_weather" else "block"
    client = await RawStdioClient.start(child_env, home)
    await client.initialize()
    await client.send([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_weather", "arguments": {"city": "Oslo"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "delete_note", "arguments": {"name": "x"}}},
    ])  # fmt: skip
    assert (await client.response(2))["error"]["code"] == GUARDIAN_BLOCKED
    assert (await client.response(1))["result"]["content"][0]["text"].startswith("Oslo")
    await client.close()


async def test_fail_closed_without_daemon(child_env, home: Path):
    """No daemon (and auto-start disabled): gated calls are blocked, everything else still works."""
    client = await RawStdioClient.start(child_env, home)
    await client.initialize()
    await client.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert "tools" in (await client.response(1))["result"]
    await client.call_tool(2, "get_weather", {"city": "Rome"})
    reply = await client.response(2)
    assert reply["error"]["code"] == GUARDIAN_BLOCKED and reply["error"]["data"]["reason"] == "unavailable"
    await client.close()


async def test_daemon_dies_while_waiting(home: Path, ui: ScriptedUI, settings, child_env):
    from mcp_guardian.daemon import GuardianDaemon

    ui.script = lambda req: "hold"
    daemon = GuardianDaemon(settings, home, ui)
    task = asyncio.create_task(daemon.run())
    await wait_until(daemon.ready.is_set)
    client = await RawStdioClient.start(child_env, home)
    await client.initialize()
    await client.call_tool(1, "get_weather", {"city": "Rome"})
    await wait_until(lambda: len(ui.shown) == 1)
    daemon.request_stop()
    await task
    reply = await client.response(1)
    assert reply["error"]["data"]["reason"] == "unavailable"
    await client.close()


async def test_server_crash_and_exit_codes(daemon, child_env, home: Path):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "mcp_guardian", "shim", "--server", "broken", "--",
        sys.executable, "-c", "import sys; sys.exit(3)",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=child_env,
    )  # fmt: skip
    assert await asyncio.wait_for(proc.wait(), 10) == 3
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "mcp_guardian", "shim", "--server", "missing", "--", "definitely-not-a-command-xyz",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=child_env,
    )  # fmt: skip
    assert await asyncio.wait_for(proc.wait(), 10) == 127


async def test_sessions_are_reported_to_daemon(daemon, ui: ScriptedUI, child_env, home: Path):
    client = await RawStdioClient.start(child_env, home)
    await client.initialize()
    await wait_until(lambda: any(s.client_name == "claude-ai" and s.server_name for s in daemon.sessions.values()))
    session = next(iter(daemon.sessions.values()))
    assert session.source_label == "Claude Desktop" and session.server_name == "guardian-demo"
    assert ui.notifications and "Claude Desktop" in ui.notifications[0][1]
    await client.close()
    await wait_until(lambda: not daemon.sessions)


async def test_gated_method_as_notification_is_dropped(daemon, ui: ScriptedUI, child_env, home: Path):
    (home / "notes" / "keep.txt").write_text("important")
    client = await RawStdioClient.start(child_env, home)
    await client.initialize()
    # tools/call without an id: no popup can be answered, so it must never reach the server
    await client.send({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "delete_note", "arguments": {"name": "keep"}}})
    await client.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert (await client.response(1))["result"] == {}
    assert (home / "notes" / "keep.txt").exists() and ui.shown == []
    await client.close()


async def test_unforwardable_json_is_rejected_without_killing_the_shim(daemon, ui: ScriptedUI, child_env, home: Path):
    client = await RawStdioClient.start(child_env, home)
    await client.initialize()
    await client.send(b'{"jsonrpc":"2.0","id":7,"method":"ping","params":{"x":1e400}}\n')
    assert (await client.recv())["error"]["code"] == PARSE_ERROR
    await client.send(b'{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"get_weather","arguments":{"city":"\\ud800"}}}\n')
    assert (await client.recv())["error"]["code"] == PARSE_ERROR
    await client.send({"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert (await client.response(9))["result"] == {}  # still alive
    assert ui.shown == []
    await client.close()
