"""Filesystem locations used by MCP Guardian and by Claude Desktop."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

# macOS limits AF_UNIX paths to 104 bytes (Linux: 108). Keep a margin.
MAX_UNIX_SOCKET_PATH = 100


def guardian_home() -> Path:
    """State directory (config, socket, auth token, logs). Override with MCP_GUARDIAN_HOME."""
    override = os.environ.get("MCP_GUARDIAN_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".mcp-guardian"


def ensure_private_dir(path: Path) -> Path:
    """Create ``path`` (and parents) and restrict it to the current user."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path


def use_unix_socket() -> bool:
    return os.name != "nt" and hasattr(socket, "AF_UNIX")


def socket_path(home: Path) -> Path:
    return home / "guardian.sock"


def port_file(home: Path) -> Path:
    """Windows only: the loopback TCP port the daemon listens on."""
    return home / "guardian.port"


def token_path(home: Path) -> Path:
    return home / "auth.token"


def lock_path(home: Path) -> Path:
    return home / "daemon.lock"


def pid_path(home: Path) -> Path:
    return home / "daemon.pid"


def config_path(home: Path) -> Path:
    return home / "config.toml"


def log_dir(home: Path) -> Path:
    return home / "logs"


def audit_log_path(home: Path) -> Path:
    return log_dir(home) / "audit.jsonl"


def daemon_log_path(home: Path) -> Path:
    return log_dir(home) / "daemon.log"


def backup_dir(home: Path) -> Path:
    return home / "backups"


def gateway_secret_path(home: Path) -> Path:
    return home / "gateway.secret"


def claude_desktop_config_path() -> Path:
    """Location of ``claude_desktop_config.json`` for the current platform."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def claude_desktop_log_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "Claude"
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(appdata) / "Claude" / "logs"
    return Path.home() / ".config" / "Claude" / "logs"
