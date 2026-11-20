"""Local IPC between interceptors (shim / gateway / CLI) and the decision daemon.

Transport
    POSIX: a Unix domain socket inside the 0700 state directory (mode 0600).
    Windows: loopback TCP on an ephemeral port published in ``guardian.port``.

Framing
    One JSON object per line (UTF-8), same as MCP's stdio transport.

Authentication (mutual, token never sent over the wire)
    daemon -> client  {"type": "challenge", "nonce": Ns}
    client -> daemon  {"type": "hello", "nonce": Nc, "mac": HMAC(token, "client|Ns|Nc"), ...}
    daemon -> client  {"type": "welcome", "mac": HMAC(token, "daemon|Nc|Ns"), "settings": {...}}

    The token lives in ``auth.token`` (mode 0600). The client verifies the
    daemon's MAC so a process squatting the port cannot impersonate the
    daemon and auto-approve requests.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp_guardian import paths

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 64 * 1024 * 1024
HANDSHAKE_TIMEOUT = 5.0


class IpcError(Exception):
    pass


class AuthError(IpcError):
    pass


def _write_private(path: Path, data: str) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data.encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, path)


def load_or_create_token(home: Path) -> bytes:
    path = paths.token_path(home)
    try:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token.encode()
    except FileNotFoundError:
        pass
    token = secrets.token_hex(32)
    _write_private(path, token)
    return token.encode()


def read_token(home: Path) -> bytes | None:
    try:
        token = paths.token_path(home).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token.encode() if token else None


def _mac(token: bytes, *parts: str) -> str:
    return hmac.new(token, "|".join(parts).encode(), hashlib.sha256).hexdigest()


class Channel:
    """A framed, bidirectional JSON message stream."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        self._closed = False

    async def send(self, msg: dict[str, Any]) -> None:
        if self._closed:
            raise ConnectionResetError("channel closed")
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode()
        self.writer.write(data)
        await self.writer.drain()

    async def recv(self) -> dict[str, Any] | None:
        """Next message, or None on EOF. Raises IpcError on malformed input."""
        try:
            line = await self.reader.readline()
        except (asyncio.LimitOverrunError, ValueError) as exc:
            raise IpcError(f"oversized IPC message: {exc}") from exc
        except (ConnectionResetError, BrokenPipeError):
            return None
        if not line:
            return None
        try:
            msg = json.loads(line)
        except ValueError as exc:
            raise IpcError("malformed IPC message") from exc
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise IpcError("IPC message must be an object with a 'type'")
        return msg

    @property
    def closed(self) -> bool:
        return self._closed or self.writer.is_closing()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 1.0)
        except Exception:
            pass


# --------------------------------------------------------------------------- server side


async def start_server(
    home: Path, on_connect: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]
) -> asyncio.AbstractServer:
    if paths.use_unix_socket():
        sock = paths.socket_path(home)
        if len(str(sock)) > paths.MAX_UNIX_SOCKET_PATH:
            raise IpcError(f"socket path too long ({len(str(sock))} chars): {sock}. Set a shorter MCP_GUARDIAN_HOME.")
        try:
            sock.unlink()  # stale socket; the caller holds the single-instance lock
        except FileNotFoundError:
            pass
        old_umask = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(on_connect, path=str(sock), limit=MAX_MESSAGE_BYTES)
        finally:
            os.umask(old_umask)
        os.chmod(sock, 0o600)
        return server
    server = await asyncio.start_server(on_connect, host="127.0.0.1", port=0, limit=MAX_MESSAGE_BYTES)
    port = server.sockets[0].getsockname()[1]
    _write_private(paths.port_file(home), str(port))
    return server


def cleanup_endpoint(home: Path) -> None:
    for path in (paths.socket_path(home), paths.port_file(home)):
        try:
            path.unlink()
        except (FileNotFoundError, OSError):
            pass


async def server_handshake(channel: Channel, token: bytes, welcome: dict[str, Any]) -> dict[str, Any]:
    """Authenticate a client. Returns its hello message. Raises AuthError."""
    server_nonce = secrets.token_hex(16)
    await channel.send({"type": "challenge", "v": PROTOCOL_VERSION, "nonce": server_nonce})
    hello = await asyncio.wait_for(channel.recv(), HANDSHAKE_TIMEOUT)
    if not hello or hello.get("type") != "hello":
        raise AuthError("expected hello")
    client_nonce = str(hello.get("nonce", ""))
    if len(client_nonce) < 16:
        raise AuthError("missing client nonce")
    expected = _mac(token, "client", server_nonce, client_nonce)
    if not hmac.compare_digest(expected, str(hello.get("mac", ""))):
        await channel.send({"type": "error", "error": "authentication failed"})
        raise AuthError("bad client MAC")
    await channel.send({"type": "welcome", "v": PROTOCOL_VERSION, "mac": _mac(token, "daemon", client_nonce, server_nonce), **welcome})
    return hello


# --------------------------------------------------------------------------- client side


async def open_channel(home: Path, timeout: float = 2.0) -> Channel:
    """Connect to the daemon endpoint (no handshake). Raises OSError if unreachable."""
    if paths.use_unix_socket():
        sock = paths.socket_path(home)
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(sock), limit=MAX_MESSAGE_BYTES), timeout
        )
    else:
        try:
            port = int(paths.port_file(home).read_text().strip())
        except (OSError, ValueError) as exc:
            raise ConnectionRefusedError("daemon port file missing") from exc
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", port, limit=MAX_MESSAGE_BYTES), timeout
        )
    return Channel(reader, writer)


async def client_handshake(channel: Channel, token: bytes, hello: dict[str, Any]) -> dict[str, Any]:
    """Authenticate to the daemon and verify the daemon. Returns the welcome message."""
    challenge = await asyncio.wait_for(channel.recv(), HANDSHAKE_TIMEOUT)
    if not challenge or challenge.get("type") != "challenge":
        raise AuthError("expected challenge from daemon")
    server_nonce = str(challenge.get("nonce", ""))
    client_nonce = secrets.token_hex(16)
    await channel.send(
        {"type": "hello", "v": PROTOCOL_VERSION, "nonce": client_nonce,
         "mac": _mac(token, "client", server_nonce, client_nonce), **hello}  # fmt: skip
    )
    welcome = await asyncio.wait_for(channel.recv(), HANDSHAKE_TIMEOUT)
    if not welcome or welcome.get("type") != "welcome":
        raise AuthError((welcome or {}).get("error", "daemon rejected the connection"))
    expected = _mac(token, "daemon", client_nonce, server_nonce)
    if not hmac.compare_digest(expected, str(welcome.get("mac", ""))):
        raise AuthError("daemon failed to prove knowledge of the auth token")
    return welcome


async def connect_authenticated(home: Path, hello: dict[str, Any], timeout: float = 2.0) -> tuple[Channel, dict[str, Any]]:
    token = read_token(home)
    if token is None:
        raise ConnectionRefusedError("auth token missing (daemon never started?)")
    channel = await open_channel(home, timeout)
    try:
        welcome = await client_handshake(channel, token, hello)
    except BaseException:
        await channel.close()
        raise
    return channel, welcome
