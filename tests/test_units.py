"""Unit tests: JSON-RPC parsing, policy, config, broker, tracker, Claude config rewriting, IPC auth."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mcp_guardian import claude_config, ipc
from mcp_guardian.audit import EventHub, redact
from mcp_guardian.broker import ApprovalBroker
from mcp_guardian.config import ConfigError, Settings, parse_settings
from mcp_guardian.interceptor import SessionTracker, blocked_response
from mcp_guardian.jsonrpc import GUARDIAN_BLOCKED, Kind, classify, loads_strict
from mcp_guardian.models import ApprovalRequest, Decision, SessionInfo
from mcp_guardian.policy import PolicyEngine, Rule

from conftest import ScriptedUI, wait_until


def make_req(tool: str = "write_note", server: str = "demo", transport: str = "stdio", rid: str = "r1") -> ApprovalRequest:
    session = SessionInfo(session_id="s1", transport=transport, server=server, client_name="claude-ai")
    return ApprovalRequest(request_id=rid, method="tools/call", target=tool, arguments={"name": "x"}, session=session)


# --------------------------------------------------------------------------- JSON-RPC


def test_strict_parser_rejects_ambiguous_json():
    with pytest.raises(ValueError):
        loads_strict('{"jsonrpc":"2.0","method":"tools/list","method":"tools/call","id":1}')
    with pytest.raises(ValueError):
        loads_strict('{"jsonrpc":"2.0","id":NaN,"method":"ping"}')
    with pytest.raises(ValueError):
        loads_strict(b"\xff\xfe")
    assert loads_strict(b'{"a":1}') == {"a": 1}


@pytest.mark.parametrize(
    "msg, kind",
    [
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, Kind.REQUEST),
        ({"jsonrpc": "2.0", "id": "abc", "method": "x", "params": {}}, Kind.REQUEST),
        ({"jsonrpc": "2.0", "method": "notifications/initialized"}, Kind.NOTIFICATION),
        ({"jsonrpc": "2.0", "id": 1, "result": {}}, Kind.RESPONSE),
        ({"jsonrpc": "2.0", "id": 1, "error": {"code": 1, "message": "x"}}, Kind.RESPONSE),
        ({"jsonrpc": "2.0", "id": None, "method": "tools/call"}, Kind.INVALID),  # MCP forbids null ids
        ({"jsonrpc": "2.0", "id": 1.5, "method": "tools/call"}, Kind.INVALID),
        ({"jsonrpc": "2.0", "id": True, "method": "tools/call"}, Kind.INVALID),
        ({"jsonrpc": "1.0", "id": 1, "method": "tools/call"}, Kind.INVALID),
        ({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "x"}, Kind.INVALID),
        ({"jsonrpc": "2.0", "id": 1, "result": {}, "error": {}}, Kind.INVALID),
        ([], Kind.INVALID),
    ],
)
def test_classify(msg, kind):
    assert classify(msg) is kind


def test_gating_is_case_and_whitespace_insensitive():
    gated = frozenset({"tools/call"})
    assert SessionTracker.is_gated("Tools/Call ", gated)
    assert not SessionTracker.is_gated("tools/list", gated)


def test_blocked_response_shapes():
    msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "rm"}}
    err = blocked_response(msg, Decision.block("user"), "error")
    assert err["id"] == 7 and err["error"]["code"] == GUARDIAN_BLOCKED and "NOT executed" in err["error"]["message"]
    res = blocked_response(msg, Decision.block("timeout"), "result")
    assert res["result"]["isError"] is True and "in time" in res["result"]["content"][0]["text"]


# --------------------------------------------------------------------------- policy + config


def test_policy_first_match_and_remember():
    engine = PolicyEngine([Rule("block", tool="delete_*"), Rule("allow", server="demo", tool="get_*")])
    assert engine.evaluate(make_req("delete_note")).action == "block"
    assert engine.evaluate(make_req("get_weather")).action == "allow"
    assert engine.evaluate(make_req("get_weather", server="other")) is None
    assert engine.evaluate(make_req("write_note")) is None
    remembered = engine.remember(make_req("write[1]*"), allowed=True)
    assert remembered.matches(make_req("write[1]*"))
    assert not remembered.matches(make_req("write1x"))  # glob characters are escaped
    assert engine.forget_all() == 1


def test_config_parsing_and_validation():
    s = parse_settings({
        "gated_methods": ["tools/call"], "approval_timeout": 30, "fail_mode": "open",
        "rules": [{"action": "allow", "server": "demo", "tool": "get_*"}],
        "gateway": {"port": 9999, "upstreams": {"deepwiki": {"url": "https://mcp.deepwiki.com/mcp"}}},
    })  # fmt: skip
    assert s.gated_methods == ("tools/call",) and s.fail_mode == "open" and s.approval_timeout == 30
    assert s.rules[0].tool == "get_*" and s.gateway.upstreams["deepwiki"].url.startswith("https://")
    for bad in ({"fail_mode": "maybe"}, {"rules": [{"action": "yolo"}]}, {"approval_timeout": True},
                {"typo_key": 1}, {"gateway": {"upstreams": {"x": {"url": "ftp://x"}}}}):  # fmt: skip
        with pytest.raises(ConfigError):
            parse_settings(bad)


def test_redaction():
    out = redact({"path": "/tmp/x", "api_key": "sk-123", "nested": {"password": "p"}, "big": "a" * 2000})
    assert out["api_key"] == "[REDACTED]" and out["nested"]["password"] == "[REDACTED]"
    assert out["path"] == "/tmp/x" and len(out["big"]) < 600
    assert redact({"a": 1}, "none") == "[omitted]"


# --------------------------------------------------------------------------- broker


def make_broker(ui: ScriptedUI, **overrides) -> ApprovalBroker:
    settings = Settings(**{"approval_timeout": 5, **overrides})
    return ApprovalBroker(settings, PolicyEngine(settings.rules), EventHub(None), ui)


async def test_broker_allow_block_and_rules():
    ui = ScriptedUI()
    ui.script = lambda r: "block" if r.target.startswith("delete") else "allow"
    broker = make_broker(ui, rules=(Rule("allow", tool="get_*"),))
    assert (await broker.evaluate(make_req("write_note", rid="a"))).allowed
    assert not (await broker.evaluate(make_req("delete_note", rid="b"))).allowed
    decision = await broker.evaluate(make_req("get_weather", rid="c"))
    assert decision.allowed and decision.reason == "rule"
    assert ui.targets() == ["write_note", "delete_note"]  # the rule-matched call never prompted


async def test_broker_timeout_blocks_and_dismisses():
    ui = ScriptedUI()
    ui.script = lambda r: "hold"
    broker = make_broker(ui, approval_timeout=0.2)
    decision = await broker.evaluate(make_req())
    assert not decision.allowed and decision.reason == "timeout"
    assert "Timed out" in ui.dismissed["r1"]


async def test_broker_cancellation_withdraws_popup():
    ui = ScriptedUI()
    ui.script = lambda r: "hold"
    broker = make_broker(ui)
    task = asyncio.create_task(broker.evaluate(make_req()))
    await wait_until(lambda: ui.shown)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "r1" in ui.dismissed and broker.pending() == []


async def test_broker_concurrent_out_of_order_and_remember_resolves_siblings():
    ui = ScriptedUI()
    ui.script = lambda r: "hold"
    broker = make_broker(ui)
    tasks = {rid: asyncio.create_task(broker.evaluate(make_req("read_note", rid=rid))) for rid in ("a", "b", "c")}
    other = asyncio.create_task(broker.evaluate(make_req("delete_note", rid="d")))
    await wait_until(lambda: len(ui.shown) == 4)
    ui.resolvers["d"](Decision.block("user"))  # answered first although it arrived last
    assert not (await other).allowed
    ui.resolvers["b"](Decision(True, "user", remember=True))
    results = {rid: await t for rid, t in tasks.items()}
    assert results["b"].reason == "user"
    assert results["a"].allowed and results["a"].reason == "remembered"  # settled by the remembered rule
    assert results["c"].allowed and set(ui.dismissed) == {"a", "c"}
    # and future identical calls no longer prompt
    assert (await broker.evaluate(make_req("read_note", rid="e"))).reason == "remembered"
    assert len(ui.shown) == 4


async def test_broker_limits():
    ui = ScriptedUI()
    ui.script = lambda r: "hold"
    broker = make_broker(ui, max_pending_per_session=2)
    held = [asyncio.create_task(broker.evaluate(make_req(rid=str(i)))) for i in range(2)]
    await wait_until(lambda: len(ui.shown) == 2)
    decision = await broker.evaluate(make_req(rid="x"))
    assert not decision.allowed and decision.reason == "limit"
    for t in held:
        t.cancel()


async def test_broker_ui_failure_fails_closed():
    class BrokenUI(ScriptedUI):
        def show(self, req, deadline, resolve):
            raise RuntimeError("display gone")

    decision = await make_broker(BrokenUI()).evaluate(make_req())
    assert not decision.allowed and decision.reason == "no-ui"


# --------------------------------------------------------------------------- tracker


def test_tracker_learns_identity_tools_and_continuations():
    tracker = SessionTracker(SessionInfo(session_id="s", transport="stdio", server="demo"))
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "clientInfo": {"name": "claude-ai", "version": "0.1.0"}}}  # fmt: skip
    assert tracker.observe_client_request(init)
    assert tracker.session.source_label == "Claude Desktop"
    tracker.track_forwarded(init, approved=False)
    tracker.observe_server_message({"jsonrpc": "2.0", "id": 0, "result": {"serverInfo": {"name": "fs", "version": "1"}}})
    assert tracker.session.server_name == "fs"

    lst = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    tracker.track_forwarded(lst, approved=False)
    tracker.observe_server_message({"jsonrpc": "2.0", "id": 1, "result": {"tools": [
        {"name": "rm", "description": "remove", "annotations": {"destructiveHint": True}}]}})  # fmt: skip
    call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "rm", "arguments": {"p": 1}}}
    req = tracker.build_request(call)
    assert req.risk == "destructive" and req.tool["description"] == "remove"

    # 2026-07-28 style multi-round call: the follow-up round of an approved call is not re-prompted
    tracker.track_forwarded(call, approved=True)
    record, _ = tracker.observe_server_message(
        {"jsonrpc": "2.0", "id": 2, "result": {"resultType": "input_required", "requestState": "st-1"}})
    assert record["outcome"] == "input_required"
    retry = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "rm", "arguments": {"p": 1}, "requestState": "st-1", "inputResponses": {}}}  # fmt: skip
    assert tracker.is_approved_continuation(retry)
    assert not tracker.is_approved_continuation(retry)  # single use

    modern = {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {"_meta": {
        "io.modelcontextprotocol/clientInfo": {"name": "claude-ai", "version": "2"},
        "io.modelcontextprotocol/protocolVersion": "2026-07-28"}}}  # fmt: skip
    assert tracker.observe_client_request(modern) and tracker.session.protocol_version == "2026-07-28"


# --------------------------------------------------------------------------- Claude Desktop config


def test_install_uninstall_roundtrip(tmp_path: Path):
    cfg = tmp_path / "claude_desktop_config.json"
    original = {
        "globalShortcut": "Ctrl+Space",
        "mcpServers": {
            "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
                           "env": {"TOKEN": "secret"}},  # fmt: skip
            "remote": {"url": "https://example.com/mcp"},
            "bare": {"command": "uvx"},
        },
    }
    cfg.write_text(json.dumps(original))
    report = claude_config.install(cfg, python="/venv/bin/python", home=tmp_path / "home")
    assert sorted(report.changed) == ["bare", "filesystem"] and "remote" in report.skipped
    assert report.backup and json.loads(report.backup.read_text()) == original
    wrapped = json.loads(cfg.read_text())
    fs = wrapped["mcpServers"]["filesystem"]
    assert fs["command"] == "/venv/bin/python" and fs["env"] == {"TOKEN": "secret"}
    assert fs["args"] == ["-m", "mcp_guardian", "shim", "--server=filesystem", "--", "npx", "-y",
                          "@modelcontextprotocol/server-filesystem", "/tmp"]  # fmt: skip
    assert wrapped["globalShortcut"] == "Ctrl+Space"
    # idempotent
    again = claude_config.install(cfg, python="/venv/bin/python", home=tmp_path / "home")
    assert again.changed == [] and again.backup is None
    # lossless uninstall
    claude_config.uninstall(cfg, home=tmp_path / "home")
    restored = json.loads(cfg.read_text())
    assert restored["mcpServers"]["filesystem"] == original["mcpServers"]["filesystem"]
    assert restored["mcpServers"]["bare"] == {"command": "uvx", "args": []}
    assert restored["mcpServers"]["remote"] == original["mcpServers"]["remote"]


def test_install_dry_run_and_errors(tmp_path: Path):
    cfg = tmp_path / "c.json"
    cfg.write_text('{"mcpServers": {"a": {"command": "x"}}}')
    report = claude_config.install(cfg, dry_run=True, home=tmp_path)
    assert report.changed == ["a"] and json.loads(cfg.read_text())["mcpServers"]["a"] == {"command": "x"}
    with pytest.raises(claude_config.ClaudeConfigError):
        claude_config.install(cfg, servers=["nope"], home=tmp_path)
    cfg.write_text("{not json")
    with pytest.raises(claude_config.ClaudeConfigError):
        claude_config.load(cfg)


# --------------------------------------------------------------------------- IPC authentication


async def test_ipc_rejects_wrong_token(home: Path, daemon):
    from mcp_guardian import paths

    good = paths.token_path(home).read_text()
    paths.token_path(home).write_text("f" * 64)
    try:
        with pytest.raises(ipc.AuthError):
            await ipc.connect_authenticated(home, {"role": "cli", "command": "status"})
    finally:
        paths.token_path(home).write_text(good)
    channel, welcome = await ipc.connect_authenticated(home, {"role": "cli", "command": "status"})
    status = await channel.recv()
    await channel.close()
    assert status["type"] == "status" and welcome["settings"]["gated_methods"]


async def test_ipc_socket_is_private(home: Path, daemon):
    import os
    import stat

    from mcp_guardian import paths

    if not paths.use_unix_socket():
        pytest.skip("POSIX only")
    assert stat.S_IMODE(os.stat(paths.socket_path(home)).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(home).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(paths.token_path(home)).st_mode) == 0o600


# --------------------------------------------------------------------------- review regressions


def test_strict_parser_rejects_unforwardable_values():
    with pytest.raises(ValueError):
        loads_strict('{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":1e400}}')  # overflows to inf
    with pytest.raises(ValueError):
        loads_strict('{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"\\ud800"}}')  # lone surrogate
    assert loads_strict('{"x":1.5e10,"y":"\\u00e9\\ud83d\\ude00"}') == {"x": 1.5e10, "y": "\u00e9\U0001F600"}


def test_continuation_requires_same_tool_and_arguments():
    tracker = SessionTracker(SessionInfo(session_id="s", transport="http", server="demo"))
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_file", "arguments": {"p": "a"}}}
    tracker.track_forwarded(call, approved=True)
    tracker.observe_server_message({"jsonrpc": "2.0", "id": 1, "result": {"resultType": "input_required", "requestState": "S"}})
    swapped = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "delete_everything", "arguments": {}, "requestState": "S", "inputResponses": {}}}  # fmt: skip
    assert not tracker.is_approved_continuation(swapped)  # a reused requestState can't launder another call
    wrong_state = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                   "params": {"name": "read_file", "arguments": {"p": "a"}, "requestState": "T", "inputResponses": {}}}  # fmt: skip
    assert not tracker.is_approved_continuation(wrong_state)
    plain_repeat = {"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_file", "arguments": {"p": "a"}}}
    assert not tracker.is_approved_continuation(plain_repeat)  # not a follow-up round: ask again
    good = {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "read_file", "arguments": {"p": "a"}, "requestState": "S", "inputResponses": {}}}  # fmt: skip
    assert tracker.is_approved_continuation(good)


async def test_remember_only_settles_identical_pending_requests():
    ui = ScriptedUI()
    ui.script = lambda r: "hold"
    broker = make_broker(ui)

    def shell(rid: str, command: str) -> ApprovalRequest:
        req = make_req("run_shell_command", rid=rid)
        req.arguments = {"command": command}
        return req

    ls1, ls2, rm = (asyncio.create_task(broker.evaluate(shell(r, c))) for r, c in (("1", "ls"), ("2", "ls"), ("3", "rm -rf ~")))
    await wait_until(lambda: len(ui.shown) == 3)
    ui.resolvers["1"](Decision(True, "user", remember=True))
    assert (await ls1).allowed and (await ls2).reason == "remembered"
    await asyncio.sleep(0.05)
    assert not rm.done() and "3" not in ui.dismissed  # different arguments: still waiting for the user
    ui.resolvers["3"](Decision.block("user"))
    assert not (await rm).allowed


@pytest.mark.parametrize("name", ["--", "-x", "--server", "my server"])
def test_install_handles_odd_server_names(tmp_path: Path, name: str):
    cfg = tmp_path / "c.json"
    original = {"mcpServers": {name: {"command": "uvx", "args": ["--", "tool", "--flag"]}}}
    cfg.write_text(json.dumps(original))
    claude_config.install(cfg, python="/py", home=tmp_path / "h")
    wrapped = json.loads(cfg.read_text())["mcpServers"][name]
    assert wrapped["args"][3] == f"--server={name}"
    claude_config.uninstall(cfg, home=tmp_path / "h")
    assert json.loads(cfg.read_text()) == original


def test_install_keeps_symlinked_config(tmp_path: Path):
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    real.write_text('{"mcpServers": {"a": {"command": "x"}}}')
    link = tmp_path / "claude_desktop_config.json"
    link.symlink_to(real)
    claude_config.install(link, python="/py", home=tmp_path / "h")
    assert link.is_symlink() and claude_config.is_wrapped(json.loads(real.read_text())["mcpServers"]["a"])
