"""Command line interface: ``mcp-guardian <command>`` (or ``python -m mcp_guardian``)."""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from mcp_guardian import __version__, ipc, paths
from mcp_guardian.config import ConfigError, Settings, UpstreamConfig, load_settings


def _settings(home: Path) -> Settings:
    try:
        return load_settings(home)
    except ConfigError as exc:
        raise SystemExit(f"mcp-guardian: invalid configuration: {exc}")


# ----------------------------------------------------------------------------- daemon control


def cmd_daemon(args: argparse.Namespace, home: Path) -> int:
    from mcp_guardian.daemon import run_daemon

    logging.basicConfig(
        level=os.environ.get("MCP_GUARDIAN_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    return run_daemon(_settings(home), home, args.ui)


async def _cli_request(home: Path, command: str, **extra: Any) -> dict[str, Any] | None:
    try:
        channel, _ = await ipc.connect_authenticated(home, {"role": "cli", "command": command, **extra}, timeout=2.0)
    except (OSError, asyncio.TimeoutError, ipc.IpcError):
        return None
    try:
        return await asyncio.wait_for(channel.recv(), 5)
    finally:
        await channel.close()


def _daemon_status(home: Path) -> dict[str, Any] | None:
    return asyncio.run(_cli_request(home, "status"))


def cmd_start(args: argparse.Namespace, home: Path) -> int:
    from mcp_guardian.lifecycle import spawn_daemon

    status = _daemon_status(home)
    if status:
        print(f"MCP Guardian daemon already running (pid {status['pid']}, ui {status['ui']}).")
        return 0
    spawn_daemon(home, ui=args.ui)
    for _ in range(50):
        time.sleep(0.2)
        status = _daemon_status(home)
        if status:
            print(f"MCP Guardian daemon started in the background (pid {status['pid']}, ui {status['ui']}).")
            print(f"Logs: {paths.daemon_log_path(home)}   Audit: {paths.audit_log_path(home)}")
            return 0
    print(f"Daemon did not come up; see {paths.daemon_log_path(home)}", file=sys.stderr)
    return 1


def cmd_stop(args: argparse.Namespace, home: Path) -> int:
    from mcp_guardian.lifecycle import read_pid

    if asyncio.run(_cli_request(home, "shutdown")) is not None:
        for _ in range(25):
            if _daemon_status(home) is None:
                break
            time.sleep(0.2)
        print("MCP Guardian daemon stopped.")
        return 0
    pid = read_pid(home)
    if pid and os.name != "nt":
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"Sent SIGTERM to daemon pid {pid}.")
            return 0
        except ProcessLookupError:
            pass
    print("MCP Guardian daemon is not running.")
    return 0


def _fmt_ts(ts: str) -> str:
    try:
        return _dt.datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        return str(ts)


def format_event(event: dict[str, Any]) -> str:
    """One readable line per audit/monitor event."""
    kind = event.get("event", "?")
    ts = _fmt_ts(event.get("ts", ""))
    who = f"{event.get('source') or event.get('server') or ''}"
    target = f"{event.get('method', '')} {event.get('target') or ''}".strip()
    if kind == "request":
        line = f"REQUEST   {event.get('source')} -> {event.get('server')}: {target}  args={json.dumps(event.get('arguments'), ensure_ascii=False)[:120]}"
    elif kind == "decision":
        line = f"{event.get('action', '?').upper():<9} {event.get('server')}: {target}  [{event.get('reason')}] {event.get('detail', '')}  ({event.get('latency_ms')} ms)"
    elif kind == "completed":
        line = f"COMPLETED {event.get('server')}: {target} -> {event.get('outcome')} in {event.get('duration_ms')} ms"
    elif kind == "cancelled":
        line = f"CANCELLED {event.get('server')}: {target} (withdrawn by client)"
    elif kind == "session_started":
        session = event.get("session") or {}
        line = f"SESSION+  {session.get('transport')} server '{session.get('server')}'"
    elif kind == "session_identified":
        line = f"CLIENT    {who}: {event.get('client')} -> {event.get('server')}"
    elif kind == "session_ended":
        line = f"SESSION-  {event.get('transport')} server '{event.get('server')}'"
    else:
        rest = {k: v for k, v in event.items() if k not in ("ts", "event")}
        line = f"{kind.upper():<9} {json.dumps(rest, ensure_ascii=False)[:160]}"
    return f"{ts}  {line}"


def cmd_status(args: argparse.Namespace, home: Path) -> int:
    status = _daemon_status(home)
    if status is None:
        print("MCP Guardian daemon: NOT RUNNING  (start it with `mcp-guardian start`)")
        return 1
    if args.json:
        print(json.dumps(status, indent=2))
        return 0
    print(f"MCP Guardian daemon {status['version']}: running, pid {status['pid']}, ui {status['ui']}, up {status['uptime_s']:.0f}s")
    print(f"Gated methods: {', '.join(status['settings']['gated_methods'])}   fail mode: {status['settings']['fail_mode']}")
    print(f"\nActive MCP sessions ({len(status['sessions'])}):")
    for s in status["sessions"]:
        client = f"{s.get('client_name') or '?'} {s.get('client_version') or ''}".strip()
        print(f"  [{s['transport']:<5}] {s['server']:<20} {s['source']:<36} client={client}")
    print(f"\nPending approvals ({len(status['pending'])}):")
    for p in status["pending"]:
        print(f"  {p['server']}: {p['method']} {p.get('target') or ''}  waiting {p['waiting_s']}s")
    if status["remembered"]:
        print("\nRemembered decisions:")
        for r in status["remembered"]:
            print(f"  {r}")
    print("\nRecent events:")
    for event in status["recent"][-12:]:
        print("  " + format_event(event))
    return 0


def cmd_monitor(args: argparse.Namespace, home: Path) -> int:
    async def run() -> int:
        try:
            channel, _ = await ipc.connect_authenticated(home, {"role": "cli", "command": "subscribe", "history": args.history})
        except (OSError, asyncio.TimeoutError, ipc.IpcError):
            print("MCP Guardian daemon is not running.", file=sys.stderr)
            return 1
        print("Live MCP activity (Ctrl+C to stop)\n", flush=True)
        try:
            while (msg := await channel.recv()) is not None:
                if msg.get("type") == "event":
                    event = msg["event"]
                    print(json.dumps(event) if args.json else format_event(event), flush=True)
        finally:
            await channel.close()
        return 0

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


def cmd_forget(args: argparse.Namespace, home: Path) -> int:
    reply = asyncio.run(_cli_request(home, "forget"))
    if reply is None:
        print("MCP Guardian daemon is not running.")
        return 1
    print(f"Forgot {reply.get('forgotten', 0)} remembered decision(s).")
    return 0


# ----------------------------------------------------------------------------- interceptors


def cmd_shim(argv: list[str], home: Path) -> int:
    from mcp_guardian.shim import run_shim

    if "--" not in argv:
        print("usage: mcp-guardian shim --server=NAME -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    split = argv.index("--")
    own, command = argv[:split], argv[split + 1 :]
    parser = argparse.ArgumentParser(prog="mcp-guardian shim")
    parser.add_argument("--server", required=True, help="server name (key in claude_desktop_config.json)")
    args = parser.parse_args(own)
    if not command:
        print("mcp-guardian shim: missing server command after '--'", file=sys.stderr)
        return 2
    try:
        settings = load_settings(home)
    except ConfigError as exc:  # never fail open because of a config typo
        print(f"[mcp-guardian] invalid config ({exc}); using safe defaults", file=sys.stderr)
        settings = Settings()
    return run_shim(args.server, command, home, settings)


def cmd_gateway(args: argparse.Namespace, home: Path) -> int:
    from mcp_guardian.gateway import serve_gateway

    logging.basicConfig(level=os.environ.get("MCP_GUARDIAN_LOG_LEVEL", "INFO").upper(),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # fmt: skip
    settings = _settings(home)
    gateway = settings.gateway
    if args.host:
        gateway.host = args.host
    if args.port:
        gateway.port = args.port
    for spec in args.upstream or []:
        name, sep, url = spec.partition("=")
        if not sep or not name or not url.startswith(("http://", "https://")):
            raise SystemExit(f"--upstream must look like NAME=https://host/mcp (got {spec!r})")
        gateway.upstreams[name] = UpstreamConfig(name=name, url=url)
    try:
        asyncio.run(serve_gateway(settings, home, gateway, args.public_url))
    except KeyboardInterrupt:
        pass
    return 0


# ----------------------------------------------------------------------------- Claude Desktop config


def _print_report(report: Any, verb: str) -> None:
    prefix = "[dry run] would " if report.dry_run else ""
    print(f"Claude Desktop config: {report.path}")
    for name in report.changed:
        print(f"  {prefix}{verb}: {name}")
    for name in report.unchanged:
        print(f"  unchanged: {name}")
    for name, why in report.skipped.items():
        print(f"  skipped:   {name} ({why})")
    if report.backup:
        print(f"Backup of the previous file: {report.backup}")
    if report.changed and not report.dry_run:
        print("\nFully quit and reopen Claude Desktop to apply (Cmd+Q on macOS, File > Exit on Windows).")


def cmd_install(args: argparse.Namespace, home: Path) -> int:
    from mcp_guardian import claude_config

    try:
        report = claude_config.install(args.config, args.server or None, python=args.python, dry_run=args.dry_run, home=home)
    except claude_config.ClaudeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_report(report, "guard")
    if not report.changed and not report.unchanged:
        print("No stdio MCP servers found. Add one in Claude Desktop (Settings > Developer > Edit Config) and re-run.")
    return 0


def cmd_uninstall(args: argparse.Namespace, home: Path) -> int:
    from mcp_guardian import claude_config

    try:
        report = claude_config.uninstall(args.config, args.server or None, dry_run=args.dry_run, home=home)
    except claude_config.ClaudeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    _print_report(report, "restore")
    return 0


def cmd_servers(args: argparse.Namespace, home: Path) -> int:
    from mcp_guardian import claude_config

    try:
        rows = claude_config.describe(args.config)
    except claude_config.ClaudeConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not rows:
        print("No MCP servers configured in Claude Desktop.")
    for row in rows:
        print(f"  {row['status']:<12} {row['name']:<20} {row['detail']}")
    return 0


def cmd_add_demo(args: argparse.Namespace, home: Path) -> int:
    """Add the bundled demo server to Claude Desktop (already guarded)."""
    from mcp_guardian import claude_config

    path = args.config or paths.claude_desktop_config_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{\n  "mcpServers": {}\n}\n', encoding="utf-8")
    data = claude_config.load(path)
    servers = data.setdefault("mcpServers", {})
    if args.name in servers:
        print(f"'{args.name}' already exists in {path}")
        return 0
    servers[args.name] = {"command": sys.executable, "args": ["-m", "mcp_guardian.demo.server"]}
    if args.dry_run:
        print(json.dumps({args.name: servers[args.name]}, indent=2))
        return 0
    backup = claude_config._write(path, data, home)
    print(f"Added '{args.name}' to {path} (backup: {backup})")
    return cmd_install(argparse.Namespace(config=path, server=[args.name], python=None, dry_run=False), home)


# ----------------------------------------------------------------------------- diagnostics


def cmd_doctor(args: argparse.Namespace, home: Path) -> int:
    from mcp_guardian.ui import tk_available

    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"  [{'ok' if good else '!!'}] {label}" + (f": {detail}" if detail else ""))

    print(f"MCP Guardian {__version__} doctor\n")
    check("Python >= 3.11", sys.version_info >= (3, 11), sys.version.split()[0] + f" ({sys.executable})")
    check("Tkinter popup available", tk_available(),
          "" if tk_available() else "install python.org Python or `brew install python-tk@3.x` (osascript fallback on macOS)")  # fmt: skip
    if sys.platform == "darwin":
        try:
            import AppKit  # type: ignore[import-not-found]  # noqa: F401

            check("PyObjC (hide Dock icon, focus popups)", True)
        except ImportError:
            print("  [--] PyObjC not installed (optional): pip install 'mcp-guardian[macos]'")
    try:
        settings = load_settings(home)
        check("config", True, str(paths.config_path(home)) if paths.config_path(home).exists() else "defaults (no config.toml)")
    except ConfigError as exc:
        check("config", False, str(exc))
        settings = Settings()
    status = _daemon_status(home)
    check("daemon running", status is not None, f"pid {status['pid']}, ui {status['ui']}" if status else "run `mcp-guardian start`")
    if paths.use_unix_socket() and paths.socket_path(home).exists():
        mode = oct(paths.socket_path(home).stat().st_mode & 0o777)
        check("IPC socket permissions", mode == "0o600", f"{paths.socket_path(home)} {mode}")
    cfg = paths.claude_desktop_config_path()
    if cfg.exists():
        from mcp_guardian import claude_config

        try:
            rows = claude_config.describe(cfg)
            unguarded = [r["name"] for r in rows if r["status"] == "NOT guarded"]
            check("Claude Desktop servers guarded", not unguarded,
                  f"{len(rows)} configured" + (f"; not guarded: {', '.join(unguarded)}" if unguarded else ""))  # fmt: skip
            pythons = {claude_config.load(cfg)["mcpServers"][r["name"]]["command"] for r in rows if r["status"] == "guarded"}
            for py in pythons:
                check("shim interpreter exists", Path(py).exists(), py)
        except Exception as exc:
            check("Claude Desktop config readable", False, str(exc))
    else:
        print(f"  [--] Claude Desktop config not found at {cfg}")
    print(f"\n  gated methods: {', '.join(settings.gated_methods)}; fail mode: {settings.fail_mode}; "
          f"timeout: {settings.approval_timeout:g}s")  # fmt: skip
    print(f"  Claude Desktop MCP logs: {paths.claude_desktop_log_dir()}")
    print(f"  Guardian state dir:      {home}")
    return 0 if ok else 1


def cmd_demo_server(argv: list[str]) -> int:
    from mcp_guardian.demo.server import main as demo_main

    demo_main(argv)
    return 0


# ----------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-guardian",
        description="Human-in-the-loop approval for MCP requests from Claude Desktop and claude.ai.",
    )
    parser.add_argument("--version", action="version", version=f"mcp-guardian {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    p = sub.add_parser("daemon", help="run the decision daemon + popup UI in the foreground")
    p.add_argument("--ui", choices=["auto", "tk", "osascript", "console", "none"], help="approval UI (default: config / auto)")
    p.add_argument("--detached", action="store_true", help=argparse.SUPPRESS)

    p = sub.add_parser("start", help="start the daemon in the background")
    p.add_argument("--ui", choices=["auto", "tk", "osascript", "console", "none"])
    sub.add_parser("stop", help="stop the background daemon")
    p = sub.add_parser("status", help="show sessions, pending approvals and recent events")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("monitor", help="stream live MCP activity")
    p.add_argument("--json", action="store_true")
    p.add_argument("--history", type=int, default=10, help="number of past events to show first")
    sub.add_parser("forget", help="forget all remembered allow/block decisions")

    sub.add_parser("shim", help="(used by Claude Desktop) stdio proxy: shim --server=NAME -- CMD ARGS...", add_help=False)

    p = sub.add_parser("gateway", help="HTTP gateway for claude.ai remote connectors")
    p.add_argument("--upstream", action="append", metavar="NAME=URL", help="remote MCP server to protect (repeatable)")
    p.add_argument("--host", help="bind address (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default 8787)")
    p.add_argument("--public-url", help="public base URL (e.g. your tunnel) used when printing connector URLs")

    for name, helptext in (("install", "guard the stdio servers in claude_desktop_config.json"),
                           ("uninstall", "restore the original claude_desktop_config.json entries")):  # fmt: skip
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--config", type=Path, help="path to claude_desktop_config.json")
        p.add_argument("--server", action="append", help="only this server (repeatable)")
        p.add_argument("--dry-run", action="store_true")
        if name == "install":
            p.add_argument("--python", help="interpreter Claude Desktop should use (default: this one)")
    p = sub.add_parser("servers", help="list Claude Desktop MCP servers and whether they are guarded")
    p.add_argument("--config", type=Path)
    p = sub.add_parser("add-demo", help="add the bundled demo MCP server to Claude Desktop (guarded)")
    p.add_argument("--config", type=Path)
    p.add_argument("--name", default="guardian-demo")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("doctor", help="check the installation")
    sub.add_parser("demo-server", help="run the demo MCP server (--transport stdio|http)", add_help=False)
    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    home = paths.guardian_home()
    # These two pass their remaining arguments through untouched.
    if argv and argv[0] == "shim":
        return cmd_shim(argv[1:], home)
    if argv and argv[0] == "demo-server":
        return cmd_demo_server(argv[1:])
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "daemon": cmd_daemon, "start": cmd_start, "stop": cmd_stop, "status": cmd_status,
        "monitor": cmd_monitor, "forget": cmd_forget, "gateway": cmd_gateway, "install": cmd_install,
        "uninstall": cmd_uninstall, "servers": cmd_servers, "add-demo": cmd_add_demo, "doctor": cmd_doctor,
    }  # fmt: skip
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args, home)


if __name__ == "__main__":
    raise SystemExit(main())
