"""HTTP interception for claude.ai (and Claude Desktop) remote connectors.

claude.ai never runs MCP in the browser: Anthropic's cloud acts as the MCP
client and calls the connector URL over Streamable HTTP. The only point a user
controls on that path is the server endpoint, so the gateway *is* the endpoint
the user registers in claude.ai, and it reverse-proxies to the real server::

    claude.ai ─▶ Anthropic cloud ─HTTPS─▶ tunnel ─▶ gateway ─▶ real MCP server
                                                       │
                                                       └── IPC ──▶ daemon (popup)

Connector URL: ``https://<public-host>/mcp/<upstream>/<secret>``. The secret
path segment makes the URL a capability: without it every route returns 404.

Gated requests are held (the HTTP request stays open) until the user decides.
Allowed requests are forwarded with the body we inspected (re-serialised), and
the upstream response is streamed back unchanged (JSON or SSE). Blocked
requests are answered by the gateway with a JSON-RPC error and never reach the
upstream. If the client disconnects or sends ``notifications/cancelled``, the
popup is withdrawn.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hmac
import logging
import os
import secrets
import ssl
import time
from pathlib import Path
from typing import Any

from aiohttp import ClientSession, ClientTimeout, TCPConnector, web
from aiohttp.client_exceptions import ClientError

from mcp_guardian import __version__, paths
from mcp_guardian.client import GuardianClient
from mcp_guardian.config import GatewayConfig, Settings, UpstreamConfig
from mcp_guardian.interceptor import SessionTracker, blocked_response
from mcp_guardian.jsonrpc import (
    GUARDIAN_CANCELLED,
    INTERNAL_ERROR,
    INVALID_REQUEST,
    PARSE_ERROR,
    Kind,
    classify,
    dumps,
    error_response,
    id_key,
    is_valid_request_id,
    loads_lenient,
    loads_strict,
    params_of,
)
from mcp_guardian.models import Decision, SessionInfo, new_id

log = logging.getLogger("mcp_guardian.gateway")

# Request headers passed to the upstream (everything else, incl. cookies, is dropped).
_FORWARD_REQUEST_HEADERS = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id")
# Response headers passed back to the client.
_FORWARD_RESPONSE_HEADERS = ("content-type", "mcp-session-id", "cache-control", "www-authenticate", "retry-after")
SESSION_IDLE_TTL = 6 * 3600
MAX_JSON_RESPONSE = 64 * 1024 * 1024


def load_or_create_secret(home: Path, configured: str | None) -> str:
    if configured:
        return configured
    path = paths.gateway_secret_path(home)
    try:
        secret = path.read_text().strip()
        if len(secret) >= 16:
            return secret
    except FileNotFoundError:
        pass
    secret = secrets.token_urlsafe(24)
    paths.ensure_private_dir(home)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(secret)
    return secret


def tls_context() -> ssl.SSLContext:
    """System trust store plus certifi's bundle.

    python.org builds on macOS ship without root certificates until the user runs
    "Install Certificates.command", which would make every HTTPS upstream fail.
    """
    context = ssl.create_default_context()
    try:
        import certifi

        context.load_verify_locations(cafile=certifi.where())
    except (ImportError, OSError):
        pass
    return context


class SSEObserver:
    """Incrementally parses an SSE byte stream to observe JSON-RPC messages (never modifies it)."""

    def __init__(self) -> None:
        self._buffer = b""
        self._data: list[bytes] = []

    def feed(self, chunk: bytes) -> list[Any]:
        messages: list[Any] = []
        self._buffer += chunk
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                break
            line, self._buffer = self._buffer[:newline].rstrip(b"\r"), self._buffer[newline + 1 :]
            if not line:  # end of event
                if self._data:
                    try:
                        messages.append(loads_lenient(b"\n".join(self._data)))
                    except ValueError:
                        pass
                    self._data = []
            elif line.startswith(b"data:"):
                self._data.append(line[5:].lstrip(b" "))
        if len(self._buffer) > MAX_JSON_RESPONSE:  # pathological stream without newlines
            self._buffer = b""
        return messages


@dataclasses.dataclass
class _HttpSession:
    tracker: SessionTracker
    upstream: str
    session_key: str | None = None  # Mcp-Session-Id once known; None = per-request/stateless
    held: dict[str, asyncio.Task[Decision]] = dataclasses.field(default_factory=dict)
    last_seen: float = dataclasses.field(default_factory=time.monotonic)


@dataclasses.dataclass
class _Plan:
    """What to do with one POST body after inspection."""

    is_batch: bool
    forward: list[Any] = dataclasses.field(default_factory=list)
    answers: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    gated: list[tuple[dict[str, Any], str]] = dataclasses.field(default_factory=list)


class Gateway:
    #: Seconds between SSE keep-alive comments while a request waits for the user.
    #: Tunnels/CDNs drop requests that send no bytes for ~100 s (Cloudflare: 524).
    keepalive_seconds: float = 15.0

    def __init__(self, settings: Settings, home: Path, gateway: GatewayConfig | None = None) -> None:
        self.settings = settings
        self.config = gateway or settings.gateway
        self.home = home
        self.secret = load_or_create_secret(home, self.config.secret)
        self.client = GuardianClient(home, settings, "gateway")
        # Only sessions with a real Mcp-Session-Id are stored. Requests without one get a
        # fresh tracker each time, so unrelated clients never share state.
        self._sessions: dict[tuple[str, str], _HttpSession] = {}
        self._catalogs: dict[str, dict[str, dict[str, Any]]] = {}  # per-upstream tool metadata
        self._tasks: set[asyncio.Task[Any]] = set()
        self._http: ClientSession | None = None

    # ------------------------------------------------------------------ app

    def make_app(self) -> web.Application:
        app = web.Application(client_max_size=self.config.max_body_bytes)
        app.router.add_get("/healthz", self._health)
        app.router.add_route("*", "/mcp/{upstream}/{secret}", self._handle)
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, _app: web.Application) -> None:
        self._http = ClientSession(
            connector=TCPConnector(ssl=tls_context()),
            timeout=ClientTimeout(total=None, connect=20, sock_read=None),
            auto_decompress=True,
        )
        self._spawn(self.client.connect())

    async def _on_cleanup(self, _app: web.Application) -> None:
        for session in self._sessions.values():
            for task in session.held.values():
                task.cancel()
        if self._http is not None:
            await self._http.close()
        await self.client.close()
        for task in list(self._tasks):
            task.cancel()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def connector_urls(self, public_base: str | None = None) -> dict[str, str]:
        base = (public_base or f"http://{self.config.host}:{self.config.port}").rstrip("/")
        return {name: f"{base}/mcp/{name}/{self.secret}" for name in self.config.upstreams}

    async def _health(self, _request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "mcp-guardian-gateway", "version": __version__,
                                  "daemon_connected": self.client.connected})  # fmt: skip

    # ------------------------------------------------------------------ routing

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        if not hmac.compare_digest(request.match_info["secret"].encode(), self.secret.encode()):
            raise web.HTTPNotFound()
        upstream = self.config.upstreams.get(request.match_info["upstream"])
        if upstream is None:
            raise web.HTTPNotFound()
        if request.method == "POST":
            return await self._handle_post(request, upstream)
        if request.method in ("GET", "DELETE"):
            if request.method == "DELETE":
                self._drop_session(upstream.name, request.headers.get("Mcp-Session-Id", ""))
            return await self._proxy(request, upstream, body=None, session=None)
        raise web.HTTPMethodNotAllowed(request.method, ["GET", "POST", "DELETE"])

    # ------------------------------------------------------------------ sessions

    def _new_session(self, request: web.Request, upstream: UpstreamConfig, key: str | None) -> _HttpSession:
        info = SessionInfo(
            session_id=new_id(),
            transport="http",
            server=upstream.name,
            upstream_url=upstream.url,
            peer=request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or None)
            or request.remote,
            user_agent=request.headers.get("User-Agent"),
        )
        tracker = SessionTracker(info, tools=self._catalogs.setdefault(upstream.name, {}))
        return _HttpSession(tracker, upstream.name, key)

    def _session_for(self, request: web.Request, upstream: UpstreamConfig) -> _HttpSession:
        now = time.monotonic()
        for (name, key), sess in list(self._sessions.items()):
            if now - sess.last_seen > SESSION_IDLE_TTL and not sess.held:
                self._drop_session(name, key)
        key = request.headers.get("Mcp-Session-Id")
        if not key:
            return self._new_session(request, upstream, None)  # per-request (initialize or stateless)
        session = self._sessions.get((upstream.name, key))
        if session is None:  # e.g. the gateway restarted while the client kept its session id
            session = self._new_session(request, upstream, key)
            self._register(session, key)
        session.last_seen = now
        return session

    def _register(self, session: _HttpSession, key: str) -> None:
        session.session_key = key
        self._sessions[(session.upstream, key)] = session
        self._spawn(self.client.update_session(session.tracker.session))

    def _drop_session(self, upstream: str, key: str) -> None:
        session = self._sessions.pop((upstream, key), None)
        if session is not None:
            self._spawn(self.client.end_session(session.tracker.session.session_id))

    # ------------------------------------------------------------------ POST: the interesting part

    async def _handle_post(self, request: web.Request, upstream: UpstreamConfig) -> web.StreamResponse:
        try:
            raw = await request.read()
        except web.HTTPRequestEntityTooLarge:
            return web.json_response(error_response(None, INVALID_REQUEST, "MCP Guardian: request body too large"), status=413)
        try:
            payload = loads_strict(raw)
        except ValueError as exc:
            return web.json_response(error_response(None, PARSE_ERROR, f"MCP Guardian: invalid JSON ({exc})"), status=400)
        if isinstance(payload, list) and not payload:
            return web.json_response(error_response(None, INVALID_REQUEST, "MCP Guardian: empty batch"), status=400)

        session = self._session_for(request, upstream)
        plan = self._inspect(session, payload)
        if not plan.gated:
            return await self._respond(request, upstream, session, plan)

        lifecycle = any(m["method"] in ("initialize", "server/discover") for m, _ in plan.gated)
        if "text/event-stream" in request.headers.get("Accept", "") and not lifecycle:
            return await self._respond_streaming(request, upstream, session, plan)
        await self._decide(session, plan, keepalive=None)
        return await self._respond(request, upstream, session, plan)

    def _inspect(self, session: _HttpSession, payload: Any) -> _Plan:
        tracker = session.tracker
        plan = _Plan(is_batch=isinstance(payload, list))
        for msg in payload if plan.is_batch else [payload]:
            kind = classify(msg)
            if kind is Kind.INVALID:
                rid = msg.get("id") if isinstance(msg, dict) and is_valid_request_id(msg.get("id")) else None
                plan.answers.append(error_response(rid, INVALID_REQUEST, "MCP Guardian: invalid JSON-RPC message, not forwarded"))
                continue
            if kind is Kind.REQUEST:
                if tracker.observe_client_request(msg) and session.session_key:
                    self._spawn(self.client.update_session(tracker.session))
                if tracker.is_gated(msg["method"], self.client.gated_methods):
                    if tracker.is_approved_continuation(msg):
                        self._spawn(self.client.event("continuation_allowed", server=session.upstream, method=msg["method"]))
                        tracker.track_forwarded(msg, approved=True)
                        plan.forward.append(msg)
                        continue
                    key = id_key(msg["id"])
                    if key in session.held or any(k == key for _, k in plan.gated):
                        plan.answers.append(error_response(msg["id"], INVALID_REQUEST, "MCP Guardian: duplicate request id"))
                        continue
                    plan.gated.append((msg, key))
                    continue
                tracker.track_forwarded(msg, approved=False)
            elif kind is Kind.NOTIFICATION:
                if tracker.is_gated(msg["method"], self.client.gated_methods):
                    log.warning("dropped gated method %r sent as a notification", msg["method"])
                    continue  # no id => no answer possible; a lax server might still execute it
                if msg["method"] == "notifications/cancelled":
                    request_id = params_of(msg).get("requestId")
                    task = session.held.get(id_key(request_id)) if is_valid_request_id(request_id) else None
                    if task is not None:
                        task.cancel()  # still waiting for the user: withdraw, never forward
                        continue
            plan.forward.append(msg)
        return plan

    async def _decide(self, session: _HttpSession, plan: _Plan, keepalive: web.StreamResponse | None) -> None:
        """Wait for the user on every gated message; move each into forward or answers."""
        tracker = session.tracker
        tasks = []
        for msg, key in plan.gated:
            req = tracker.build_request(msg)
            task = asyncio.create_task(self.client.request_decision(req))
            session.held[key] = task
            tasks.append((msg, key, req, task))
        try:
            # Handler cancellation (the client disconnected) lands here and cancels
            # every pending approval, which withdraws the popups.
            pending = {t for *_, t in tasks}
            while pending:
                _, pending = await asyncio.wait(pending, timeout=self.keepalive_seconds if keepalive else None)
                if pending and keepalive is not None:
                    await keepalive.write(b": mcp-guardian is waiting for the user's approval\n\n")
        except BaseException:
            for *_, task in tasks:
                task.cancel()
            raise
        finally:
            for _, key, _, _ in tasks:
                session.held.pop(key, None)
        for msg, _key, req, task in tasks:
            if task.cancelled():
                plan.answers.append(error_response(msg["id"], GUARDIAN_CANCELLED, "MCP Guardian: request cancelled by the client"))
                continue
            try:
                decision = task.result()
            except Exception as exc:  # fail closed
                decision = Decision.block("error", f"{type(exc).__name__}: {exc}")
            if decision.allowed:
                tracker.track_forwarded(msg, approved=True, guardian_id=req.request_id)
                plan.forward.append(msg)
            else:
                plan.answers.append(blocked_response(msg, decision, self.client.block_style))

    # ------------------------------------------------------------------ responses

    async def _respond(self, request: web.Request, upstream: UpstreamConfig, session: _HttpSession, plan: _Plan) -> web.StreamResponse:
        if not plan.forward:
            if not plan.answers:
                return web.Response(status=202)
            return web.json_response(plan.answers if plan.is_batch else plan.answers[0])
        body = plan.forward if plan.is_batch else plan.forward[0]
        return await self._proxy(request, upstream, body=body, session=session, extra=plan.answers)

    async def _respond_streaming(self, request: web.Request, upstream: UpstreamConfig, session: _HttpSession, plan: _Plan) -> web.StreamResponse:
        """Answer as SSE right away and keep the stream alive while the user decides."""
        response = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache",
                                                           "X-Accel-Buffering": "no"})  # fmt: skip
        if session.session_key:
            response.headers["Mcp-Session-Id"] = session.session_key
        await response.prepare(request)
        await self._decide(session, plan, keepalive=response)
        for answer in plan.answers:
            await response.write(_sse_event(answer))
        if plan.forward:
            body = plan.forward if plan.is_batch else plan.forward[0]
            await self._forward_into_stream(request, upstream, session, body, response)
        await response.write_eof()
        return response

    async def _forward_into_stream(self, request: web.Request, upstream: UpstreamConfig, session: _HttpSession,
                                   body: Any, response: web.StreamResponse) -> None:  # fmt: skip
        assert self._http is not None
        try:
            upstream_resp = await self._http.request("POST", upstream.url, data=dumps(body).encode(),
                                                     headers=self._upstream_headers(request, upstream), allow_redirects=False)  # fmt: skip
        except (ClientError, asyncio.TimeoutError, OSError) as exc:
            for answer in _errors_for(body, f"upstream MCP server '{upstream.name}' is unreachable ({type(exc).__name__})"):
                await response.write(_sse_event(answer))
            return
        async with upstream_resp:
            content_type = upstream_resp.headers.get("Content-Type", "")
            if upstream_resp.status < 300 and content_type.startswith("text/event-stream"):
                observer = SSEObserver()
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                    for msg in observer.feed(chunk):
                        self._observe(session, msg)
                return
            try:
                payload = await _read_all(upstream_resp)
            except ValueError as exc:
                payload, content_type = b"", ""
                log.warning("upstream %s: %s", upstream.name, exc)
            if upstream_resp.status < 300 and content_type.startswith("application/json") and payload:
                try:
                    message = loads_lenient(payload)
                except ValueError:
                    message = None
                if message is not None:
                    self._observe(session, message)
                    await response.write(_sse_event(message))
                    return
            if upstream_resp.status == 202:
                return
            for answer in _errors_for(body, f"upstream MCP server '{upstream.name}' returned HTTP {upstream_resp.status}"):
                await response.write(_sse_event(answer))

    # ------------------------------------------------------------------ proxying

    def _upstream_headers(self, request: web.Request, upstream: UpstreamConfig) -> dict[str, str]:
        headers = {k: v for k in _FORWARD_REQUEST_HEADERS if (v := request.headers.get(k)) is not None}
        if upstream.forward_authorization and "Authorization" in request.headers:
            headers["Authorization"] = request.headers["Authorization"]
        headers.update(upstream.headers)
        headers["User-Agent"] = f"mcp-guardian-gateway/{__version__}"
        return headers

    async def _proxy(
        self,
        request: web.Request,
        upstream: UpstreamConfig,
        *,
        body: Any,
        session: _HttpSession | None,
        extra: list[dict[str, Any]] | None = None,
    ) -> web.StreamResponse:
        assert self._http is not None
        data = dumps(body).encode() if body is not None else None
        try:
            upstream_resp = await self._http.request(
                request.method, upstream.url, data=data, headers=self._upstream_headers(request, upstream), allow_redirects=False
            )
        except (ClientError, asyncio.TimeoutError, OSError) as exc:
            log.warning("upstream %s unreachable: %s", upstream.name, exc)
            message = f"upstream MCP server '{upstream.name}' is unreachable ({type(exc).__name__})"
            if body is None:
                return web.json_response({"error": "MCP Guardian: " + message}, status=502)
            answers = (extra or []) + _errors_for(body, message)
            if not answers:
                return web.json_response({"error": "MCP Guardian: " + message}, status=502)
            return web.json_response(answers if isinstance(body, list) else answers[0], status=502)

        async with upstream_resp:
            headers = {k: v for k in _FORWARD_RESPONSE_HEADERS if (v := upstream_resp.headers.get(k)) is not None}
            new_key = upstream_resp.headers.get("Mcp-Session-Id")
            if session is not None and new_key and session.session_key is None:
                self._register(session, new_key)  # the initialize response assigned this session its id
            content_type = upstream_resp.headers.get("Content-Type", "")

            if content_type.startswith("text/event-stream"):
                response = web.StreamResponse(status=upstream_resp.status, headers=headers)
                response.headers["Cache-Control"] = "no-cache"
                response.headers["X-Accel-Buffering"] = "no"
                await response.prepare(request)
                for answer in extra or []:  # our own answers for blocked batch members
                    await response.write(_sse_event(answer))
                observer = SSEObserver()
                async for chunk in upstream_resp.content.iter_any():
                    await response.write(chunk)
                    if session is not None:
                        for msg in observer.feed(chunk):
                            self._observe(session, msg)
                await response.write_eof()
                return response

            try:
                payload = await _read_all(upstream_resp)
            except ValueError as exc:
                log.warning("upstream %s: %s", upstream.name, exc)
                errors = _errors_for(body, str(exc)) if body is not None else []
                return web.json_response((extra or []) + errors if isinstance(body, list) else (errors[0] if errors else {"error": str(exc)}), status=502)
            is_json = content_type.startswith("application/json")
            if session is not None and is_json and payload:
                try:
                    self._observe(session, loads_lenient(payload))
                except ValueError:
                    pass
            if extra:  # batch with blocked members: their answers must not get lost
                if is_json and upstream_resp.status < 300:
                    try:
                        merged = loads_lenient(payload) if payload else []
                        payload = dumps((merged if isinstance(merged, list) else [merged]) + extra).encode()
                    except ValueError:
                        pass
                elif upstream_resp.status == 202:
                    return web.json_response(extra, headers=headers)
                else:
                    failure = f"upstream MCP server '{upstream.name}' returned HTTP {upstream_resp.status}"
                    return web.json_response(extra + _errors_for(body, failure), status=upstream_resp.status, headers=headers)
            return web.Response(status=upstream_resp.status, body=payload, headers=headers)

    def _observe(self, session: _HttpSession, msg: Any) -> None:
        for item in msg if isinstance(msg, list) else [msg]:
            try:
                record, changed = session.tracker.observe_server_message(item)
            except Exception:
                log.exception("error while observing upstream message")
                continue
            if changed and session.session_key:
                self._spawn(self.client.update_session(session.tracker.session))
            if record is not None:
                self._spawn(self.client.event("completed", **record))


def _sse_event(message: Any) -> bytes:
    return f"event: message\ndata: {dumps(message)}\n\n".encode()


def _errors_for(body: Any, message: str) -> list[dict[str, Any]]:
    """JSON-RPC errors for every request (not notification) in a forwarded body."""
    items = body if isinstance(body, list) else [body]
    return [error_response(m["id"], INTERNAL_ERROR, f"MCP Guardian: {message}")
            for m in items if isinstance(m, dict) and "method" in m and "id" in m]  # fmt: skip


async def _read_all(resp: Any) -> bytes:
    """Read an upstream body to EOF (``content.read(n)`` may return early)."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.content.iter_any():
        size += len(chunk)
        if size > MAX_JSON_RESPONSE:
            raise ValueError("upstream response too large")
        chunks.append(chunk)
    return b"".join(chunks)


async def serve_gateway(settings: Settings, home: Path, gateway: GatewayConfig, public_url: str | None = None) -> None:
    """Run the gateway until cancelled."""
    if not gateway.upstreams:
        raise SystemExit("No upstreams configured. Use --upstream NAME=URL or [gateway.upstreams.NAME] in config.toml.")
    gw = Gateway(settings, home, gateway)
    runner = web.AppRunner(gw.make_app(), handler_cancellation=True, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, gateway.host, gateway.port)
    await site.start()
    print(f"MCP Guardian gateway listening on http://{gateway.host}:{gateway.port}", flush=True)
    print("Connector URLs (keep them secret; the last path segment is the access key):", flush=True)
    for name, url in gw.connector_urls(public_url).items():
        print(f"  {name:<16} {url}", flush=True)
    if not public_url:
        print("\nclaude.ai connects from Anthropic's cloud, so expose this port publicly, e.g.:\n"
              f"  cloudflared tunnel --url http://{gateway.host}:{gateway.port}\n"
              "then add  https://<tunnel-host>/mcp/<upstream>/<secret>  as a custom connector "
              "(claude.ai > Settings > Connectors > Add custom connector).", flush=True)  # fmt: skip
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
