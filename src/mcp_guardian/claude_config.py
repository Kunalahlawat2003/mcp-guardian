"""Wrap / unwrap the stdio servers in ``claude_desktop_config.json``.

Install turns::

    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}

into::

    "filesystem": {"command": "/path/to/venv/bin/python",
                   "args": ["-m", "mcp_guardian", "shim", "--server=filesystem", "--",
                            "npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"]}

Everything else in the file (``env``, other keys, other settings) is kept as
is. The original command is recoverable from the args alone, so uninstall is
lossless and no extra keys are added that Claude Desktop might reject. A
timestamped backup is written before every change, and the file is replaced
atomically.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp_guardian import paths

SHIM_MARKER = ["-m", "mcp_guardian", "shim"]
SERVER_FLAG = "--server="  # one token, so names like "--" or "-x" can't confuse argument parsing


class ClaudeConfigError(Exception):
    pass


@dataclass
class ChangeReport:
    path: Path
    changed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)
    backup: Path | None = None
    dry_run: bool = False


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ClaudeConfigError(f"{path} does not exist. Is Claude Desktop installed? (Settings > Developer > Edit Config creates it.)")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ClaudeConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ClaudeConfigError(f"{path} must contain a JSON object")
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise ClaudeConfigError("'mcpServers' must be an object")
    return data


def is_wrapped(entry: dict[str, Any]) -> bool:
    args = entry.get("args")
    return isinstance(args, list) and args[: len(SHIM_MARKER)] == SHIM_MARKER and "--" in args


def wrap_entry(name: str, entry: dict[str, Any], python: str) -> dict[str, Any]:
    original_args = entry.get("args") or []
    wrapped = dict(entry)  # keeps env, cwd, and any other keys untouched
    wrapped["command"] = python
    wrapped["args"] = [*SHIM_MARKER, SERVER_FLAG + name, "--", entry["command"], *original_args]
    return wrapped


def _separator_index(args: list[str]) -> int:
    """Index of the '--' that ends the shim's own options (supports the older '--server NAME' form)."""
    i = len(SHIM_MARKER)
    while i < len(args):
        if args[i] == "--":
            return i
        i += 2 if args[i] == "--server" else 1
    raise ClaudeConfigError("wrapped entry has no '--' separator")


def unwrap_entry(entry: dict[str, Any]) -> dict[str, Any]:
    args = entry["args"]
    rest = args[_separator_index(args) + 1 :]
    if not rest:
        raise ClaudeConfigError("wrapped entry has no original command after '--'")
    unwrapped = dict(entry)
    unwrapped["command"] = rest[0]
    unwrapped["args"] = rest[1:]
    return unwrapped


def _skip_reason(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "not an object"
    if not isinstance(entry.get("command"), str) or not entry.get("command"):
        if entry.get("url") or entry.get("type") in ("http", "sse", "streamable-http"):
            return "remote server (use `mcp-guardian gateway` for HTTP servers)"
        return "no 'command' (not a stdio server)"
    if "args" in entry and not (isinstance(entry["args"], list) and all(isinstance(a, str) for a in entry["args"])):
        return "'args' is not a list of strings"
    return None


def _write(path: Path, data: dict[str, Any], home: Path) -> Path:
    backups = paths.ensure_private_dir(paths.backup_dir(home))
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = backups / f"claude_desktop_config.{stamp}.json"
    original = path.read_bytes()
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # may contain API keys in env
    with os.fdopen(fd, "wb") as fh:
        fh.write(original)
    target = Path(os.path.realpath(path))  # keep a symlinked config a symlink
    tmp = target.with_name(target.name + ".mcp-guardian.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # env may hold secrets
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    try:
        os.chmod(tmp, target.stat().st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, target)
    return backup


def install(
    path: Path | None = None,
    servers: list[str] | None = None,
    *,
    python: str | None = None,
    dry_run: bool = False,
    home: Path | None = None,
) -> ChangeReport:
    path = path or paths.claude_desktop_config_path()
    home = home or paths.guardian_home()
    python = python or sys.executable
    data = load(path)
    entries: dict[str, Any] = data.setdefault("mcpServers", {})
    report = ChangeReport(path=path, dry_run=dry_run)
    unknown = set(servers or []) - set(entries)
    if unknown:
        raise ClaudeConfigError(f"unknown server(s): {', '.join(sorted(unknown))}")
    for name, entry in entries.items():
        if servers and name not in servers:
            continue
        reason = _skip_reason(entry)
        if reason:
            report.skipped[name] = reason
        elif is_wrapped(entry):
            if entry["command"] != python:  # re-point at the current interpreter (e.g. new venv)
                entries[name] = dict(entry, command=python)
                report.changed.append(name)
            else:
                report.unchanged.append(name)
        else:
            entries[name] = wrap_entry(name, entry, python)
            report.changed.append(name)
    if report.changed and not dry_run:
        report.backup = _write(path, data, home)
    return report


def uninstall(
    path: Path | None = None,
    servers: list[str] | None = None,
    *,
    dry_run: bool = False,
    home: Path | None = None,
) -> ChangeReport:
    path = path or paths.claude_desktop_config_path()
    home = home or paths.guardian_home()
    data = load(path)
    entries: dict[str, Any] = data.get("mcpServers", {})
    report = ChangeReport(path=path, dry_run=dry_run)
    for name, entry in entries.items():
        if servers and name not in servers:
            continue
        if isinstance(entry, dict) and is_wrapped(entry):
            entries[name] = unwrap_entry(entry)
            report.changed.append(name)
        else:
            report.unchanged.append(name)
    if report.changed and not dry_run:
        report.backup = _write(path, data, home)
    return report


def describe(path: Path | None = None) -> list[dict[str, Any]]:
    path = path or paths.claude_desktop_config_path()
    data = load(path)
    rows = []
    for name, entry in data.get("mcpServers", {}).items():
        reason = _skip_reason(entry)
        if reason:
            rows.append({"name": name, "status": "unsupported", "detail": reason})
        elif is_wrapped(entry):
            original = unwrap_entry(entry)
            rows.append({"name": name, "status": "guarded", "detail": " ".join([original["command"], *original["args"]])})
        else:
            rows.append({"name": name, "status": "NOT guarded", "detail": " ".join([entry["command"], *entry.get("args", [])])})
    return rows
