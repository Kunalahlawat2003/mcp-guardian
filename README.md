# MCP Guardian

**A human-in-the-loop firewall for Model Context Protocol (MCP) traffic from Claude Desktop and claude.ai.**

Every time Claude tries to call an MCP tool, read an MCP resource or fetch an MCP prompt, MCP Guardian holds the request, shows an always-on-top popup with who is asking, which server, which tool and the exact arguments, and lets you **Allow** or **Block** it. Allowed requests continue to the server unchanged; blocked requests never reach the server and Claude receives a clear denial.

> 🎥 **Demo video:** _add link here_ · 📐 **Architecture:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) · 🎬 **Demo script:** [docs/DEMO.md](docs/DEMO.md)

```
 Claude Desktop ──stdio──▶ guardian shim ──stdio──▶ local MCP server      (filesystem, git, …)
                                  │
                                  ▼  authenticated local IPC
                         ┌──────────────────┐
                         │ Guardian daemon  │──▶ always-on-top Allow / Block popup
                         │ rules · timeouts │──▶ audit log, `status`, `monitor`
                         └──────────────────┘
                                  ▲
 claude.ai ─▶ Anthropic cloud ─HTTPS─▶ guardian gateway ──HTTP──▶ remote MCP server
```

## Highlights

- **Works with Claude Desktop and claude.ai.** A stdio proxy ("shim") guards Claude Desktop's local servers; a Streamable-HTTP gateway guards remote connectors used by claude.ai (and by Claude Desktop/mobile, which use the same connectors).
- **Protocol-level interception.** No packet sniffing, TLS interception or app patching. Guardian is a JSON-RPC-aware proxy on the only paths MCP traffic actually takes.
- **Real request holding.** The request stays pending until you decide. Allowed → forwarded byte-for-byte as inspected. Blocked → JSON-RPC error `-32050` that tells the model the action was *not* executed.
- **Concurrency.** Many requests can wait at once (one popup each, cascaded, "2 of 3 pending"); other traffic such as `tools/list`, pings and progress keeps flowing; decisions can be made in any order.
- **Edge cases.** Client cancellation and disconnects withdraw the popup, timeouts auto-block, the daemon crashing fails closed, servers crashing are reported faithfully, and malformed or ambiguous JSON is refused, never forwarded.
- **Security by default.** Fail-closed, mutually authenticated IPC (HMAC challenge/response, 0600 socket), anti-mis-click arming delay, no keyboard shortcut for Allow, strict JSON parsing against parser-differential smuggling, capability URLs for the public gateway, redacted audit log.
- **Every protocol revision.** Tested with the official MCP Python SDK v2 against handshake-era (2024-11-05 → 2025-11-25) and stateless 2026-07-28 sessions, including multi-round `input_required` tool calls.
- **Useful context in the popup.** Tool description and server-declared risk annotations (read-only / destructive) from `tools/list`, client identity, server name/version and command line, full pretty-printed arguments.
- **Rules and memory.** Optional allow/block rules (globs) in `config.toml`, plus "Remember my choice" in the popup, which also settles waiting requests with identical arguments.

## Requirements

- Python **3.11+** with **Tkinter** (for the popup).
  - macOS: the [python.org installer](https://www.python.org/downloads/) includes Tk; with Homebrew run `brew install python-tk@3.13`. Without Tk, Guardian falls back to native macOS dialogs.
  - Windows: the python.org installer includes Tk.
  - Linux: `sudo apt install python3-tk`.
- Claude Desktop (macOS/Windows) and/or a claude.ai account with custom connectors.
- For claude.ai only: a way to expose a local port over HTTPS, e.g. [`cloudflared`](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/) (`brew install cloudflared`) or ngrok.

## Quick start: Claude Desktop (about 2 minutes)

```bash
git clone <this repo> mcp-guardian && cd mcp-guardian
```

```bash
python3 -m venv .venv && source .venv/bin/activate
```

```bash
pip install -e ".[demo,macos]"
```

(`[demo]` installs the official MCP SDK for the bundled demo server, `[macos]` adds PyObjC to hide the Dock icon and focus popups. Both are optional; on Windows use `pip install -e ".[demo]"` and `.venv\Scripts\activate`.)

1. **Start the daemon** (runs in the background; shims also start it automatically if needed):

   ```bash
   mcp-guardian start
   ```

2. **Guard your Claude Desktop servers.** This rewrites `claude_desktop_config.json` so each stdio server is launched through the shim (a timestamped backup is written first; `mcp-guardian uninstall` restores the originals exactly):

   ```bash
   mcp-guardian install
   ```

   No MCP servers configured yet? Add the bundled demo server (already guarded):

   ```bash
   mcp-guardian add-demo
   ```

3. **Fully quit and reopen Claude Desktop** (⌘Q on macOS; File → Exit on Windows).

4. In Claude Desktop, ask for example:
   - *"Use guardian-demo to save a note called groceries that says: milk, eggs."* → popup → **Allow** → the note is written.
   - *"Delete the groceries note."* → popup (red, DESTRUCTIVE) → **Block** → Claude reports it was blocked and the note is untouched.
   - *"Get the weather in Paris, Tokyo and Lima."* → three popups at once; answer them in any order.

Check what is going on at any time:

```bash
mcp-guardian status
```

```bash
mcp-guardian monitor
```

## Claude Web (claude.ai) setup

claude.ai does not run MCP in your browser: Anthropic's cloud is the MCP client and connects to the connector URL you register. So Guardian sits **at that URL**: the gateway is what you register, and it forwards to the real server after you approve.

1. Tell the gateway which remote MCP servers to protect, either in `~/.mcp-guardian/config.toml` (see [examples/config.toml](examples/config.toml)) or on the command line:

   ```bash
   mcp-guardian gateway --upstream deepwiki=https://mcp.deepwiki.com/mcp
   ```

   To try the bundled demo server over HTTP instead, run `mcp-guardian demo-server --transport http --port 9000` in another terminal and use `--upstream demo=http://127.0.0.1:9000/mcp`.

   The gateway prints a connector URL per upstream, e.g. `http://127.0.0.1:8787/mcp/deepwiki/<secret>`. The last segment is a random secret (stored in `~/.mcp-guardian/gateway.secret`); without it every path returns 404.

2. Expose the gateway over HTTPS (a quick tunnel needs no account):

   ```bash
   cloudflared tunnel --url http://127.0.0.1:8787
   ```

3. In claude.ai: **Settings → Connectors → Add custom connector**, and paste `https://<your-tunnel-host>/mcp/deepwiki/<secret>`. Enable the connector in a chat and ask Claude to use it. The approval popup appears on the machine running Guardian.

Connectors added this way also work in Claude Desktop and the mobile apps (they share your account's connectors), and they're guarded by the same popup.

## The popup

| Element | Meaning |
|---|---|
| Header colour + badge | Risk from the tool's server-declared annotations: red **DESTRUCTIVE**, amber **MODIFIES DATA** / **UNKNOWN RISK**, green **READ-ONLY**. These are hints from the server, not guarantees. |
| Requested by | Who initiated the request: `Claude Desktop` (stdio) or `Claude (claude.ai / remote connector)` (HTTP), plus the raw `clientInfo`. |
| MCP server | Config name, the server's self-reported name/version, and the real command line or upstream URL. |
| Request / Description | MCP method and tool, resource URI or prompt name, plus the tool's description from `tools/list`. |
| Arguments | The exact arguments, pretty-printed and scrollable. What you see is what gets forwarded. |
| Remember my choice | Applies your decision to later calls of the same tool on the same server until Guardian restarts. Requests already waiting are settled only if their arguments are identical. |
| Countdown | The request auto-blocks when `approval_timeout` (default 5 min) expires. |

Allow and Block are disabled for the first 0.8 s so a popup that appears while you are typing or clicking can't be answered by accident. There is no keyboard shortcut for Allow; **Esc** or closing the window means Block.

## Configuration

Optional. Copy [examples/config.toml](examples/config.toml) to `~/.mcp-guardian/config.toml`. Main settings:

| Key | Default | Purpose |
|---|---|---|
| `gated_methods` | `tools/call`, `resources/read`, `prompts/get` | Methods that need approval. Add `initialize` to approve every new connection. |
| `default_action` | `ask` | For gated requests no rule matches: `ask`, `allow` or `block`. |
| `approval_timeout` | `300` | Seconds before auto-block (`0` = wait forever). |
| `fail_mode` | `closed` | Daemon unreachable → `closed` blocks, `open` allows. |
| `block_style` | `error` | Blocked `tools/call` as JSON-RPC error or as an `isError` tool result. |
| `[[rules]]` | none | `action` + glob patterns on `server`, `tool`, `method`, `transport`. First match wins. |
| `[gateway]` | `127.0.0.1:8787` | Gateway bind address, secret and `upstreams` (with optional injected headers). |

The state directory is `~/.mcp-guardian` (override with `MCP_GUARDIAN_HOME`).

## CLI reference

| Command | Description |
|---|---|
| `mcp-guardian start` / `stop` | Run / stop the daemon in the background. |
| `mcp-guardian daemon [--ui tk\|osascript\|console]` | Run the daemon in the foreground (`console` = approve in the terminal, for headless machines). |
| `mcp-guardian status [--json]` | Sessions, pending approvals, remembered decisions, recent events. |
| `mcp-guardian monitor [--json]` | Live stream of all observed MCP activity. |
| `mcp-guardian install` / `uninstall [--server NAME] [--dry-run]` | Guard / unguard servers in `claude_desktop_config.json`. |
| `mcp-guardian servers` | List Claude Desktop servers and whether they are guarded. |
| `mcp-guardian add-demo` | Add the bundled demo server to Claude Desktop, guarded. |
| `mcp-guardian gateway --upstream NAME=URL` | HTTP gateway for claude.ai connectors. |
| `mcp-guardian forget` | Forget remembered decisions. |
| `mcp-guardian doctor` | Check Python/Tk, daemon, permissions and the Claude config. |
| `mcp-guardian shim --server=NAME -- CMD …` | The stdio proxy (written into Claude's config by `install`). |
| `mcp-guardian demo-server [--transport http]` | Demo MCP server: weather, notes, simulated shell. |

## How it works (short version)

Full write-up with sequence diagrams and the alternatives I rejected: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

- **Claude Desktop → local servers (stdio).** Claude Desktop spawns each configured server and speaks newline-delimited JSON-RPC over its stdin/stdout. `install` replaces each server's command with `python -m mcp_guardian shim --server=NAME -- <original command>`. The shim spawns the real server and relays both directions. Gated requests are parked as independent asyncio tasks while the daemon asks you; everything else passes straight through.
- **claude.ai → remote servers (Streamable HTTP).** Requests originate in Anthropic's cloud, so the only interception point is the server endpoint. The gateway is an aiohttp reverse proxy that inspects each POSTed JSON-RPC message, holds gated ones, and streams the upstream's JSON or SSE response back unchanged.
- **The daemon** is the single decision point (policy decision point; the shim and gateway are enforcement points). It evaluates rules, shows the popup, enforces timeouts and limits, and writes the audit log (`~/.mcp-guardian/logs/audit.jsonl`). Tk owns the main thread (a macOS requirement); asyncio runs on a worker thread; they communicate through a queue and `call_soon_threadsafe`.
- **IPC** is newline-delimited JSON over a Unix socket (0600, inside a 0700 directory) or loopback TCP on Windows, with mutual HMAC authentication using a token only the user can read. One connection multiplexes any number of pending approvals.

## Security model

| Threat | Mitigation |
|---|---|
| Daemon stopped/crashed | Fail-closed by default: gated requests are blocked with a clear message. Shims auto-restart the daemon. |
| Request smuggling via ambiguous JSON (duplicate keys, `NaN`, invalid ids, odd method casing) | Strict parser; anything ambiguous is rejected with a JSON-RPC error, never forwarded; the re-serialised inspected object is what gets forwarded; gating is case/whitespace-insensitive. |
| Accidental approval (popup steals focus while typing) | Buttons armed after 0.8 s; no Enter/Space shortcut for Allow; Esc/close = Block. |
| Fake daemon auto-approving (port squatting on Windows) | Clients verify the daemon's HMAC proof before trusting any decision. |
| Other local users | Socket 0600 in a 0700 directory; token, backups and audit log are 0600. |
| Strangers calling the public gateway URL | Unguessable path secret (capability URL), constant-time comparison, 404 otherwise; request size limits; per-session and global pending limits stop popup flooding. Popups show the remote peer address. |
| Secrets in logs | Audit log redacts argument keys that look sensitive and truncates large values (configurable). |
| Upstream API keys | Can be injected by the gateway (`headers = {…}`) so claude.ai never sees them. |

## Edge cases handled

| Scenario | Behaviour |
|---|---|
| Several tool calls at once (Claude issues parallel tool calls) | One popup per request, cascaded with a "n of m pending" badge; decided independently, in any order. Verified by tests that decide in reverse order. |
| Claude cancels a request (`notifications/cancelled`) or times out | Popup withdrawn ("Withdrawn: the client cancelled…"); the server never sees the request; no response is sent (per spec). |
| HTTP client disconnects while waiting | aiohttp handler cancellation propagates and withdraws the popup. |
| Slow decision behind a tunnel/CDN (e.g. Cloudflare's ~100 s limit) | SSE clients get the response headers at once plus keep-alive comments every 15 s, so the request survives until you decide. |
| Several claude.ai conversations at once | Gateway state is isolated per `Mcp-Session-Id`: identity, request ids and cancellations never leak between conversations. |
| Gated method sent without an id (as a notification) | Dropped, never forwarded (some JSON-RPC servers would execute it). |
| Nobody answers | Auto-block after `approval_timeout`, with a `timeout` reason. |
| Daemon dies while a popup is open | Pending requests resolve as blocked (`unavailable`). |
| MCP server crashes / command not found | The shim exits with the server's code (127 if the command is missing); the error goes to Claude's `mcp-server-*.log`. |
| Claude Desktop quits | stdin EOF → server stdin closed → SIGTERM → SIGKILL (MCP shutdown sequence); popups withdrawn. |
| Huge messages (files, images) | No line-length limits on stdio; up to 64 MiB on IPC and 16 MiB request bodies on the gateway (configurable). |
| JSON-RPC batches (MCP 2025-03-26) | Each element is gated individually. |
| Multi-round tool calls (MCP 2026-07-28 `input_required`) | The follow-up round of an already-approved call isn't prompted again (single-use, matched by `requestState`). |
| Popup flood | `max_pending` / `max_pending_per_session` auto-block the excess. |
| GUI apps with a minimal `PATH` | The shim resolves `npx`, `uvx`, etc. in common install locations (Homebrew, nvm, ~/.local/bin). |

## Limitations (honest list)

- **claude.ai connectors that bypass the gateway can't be intercepted locally.** Directory connectors (e.g. ones added from Anthropic's catalogue) are called from Anthropic's cloud straight to the vendor, so traffic never touches the user's machine. Only connectors registered through the gateway URL are guarded. A browser extension could watch the claude.ai UI, but it couldn't block server-side execution reliably and would depend on private, changing APIs, so I chose not to build one (see [ARCHITECTURE.md](docs/ARCHITECTURE.md#alternatives-considered)).
- **Stateless remote servers** (no `Mcp-Session-Id`, e.g. DeepWiki) don't let the gateway link a tool call to the `initialize` that named the client, so the popup shows "Remote HTTP client" plus the peer address and user agent. It doesn't guess from another conversation. Clients using the 2026-07-28 protocol send their identity with every request, so they're still named.
- **Desktop Extensions (`.mcpb`/`.dxt`)** are managed by Claude Desktop outside `claude_desktop_config.json` and aren't wrapped automatically. The equivalent server can be added to the config manually and guarded.
- **OAuth-protected upstreams** behind the gateway: OAuth discovery would point claude.ai at the upstream's resource, not the gateway. Use authless upstreams or static credentials injected by the gateway.
- **Client-side timeouts.** If you take longer than the client is willing to wait, the client cancels (Guardian then withdraws the popup). The request was never executed, so this is safe, but the model sees a timeout.
- **macOS full-screen Spaces** may hide floating windows from other apps; the popup is on the normal desktop.
- Tested on macOS (Apple Silicon, Python 3.13, Tk 8.6). Windows support is implemented (TCP IPC, `pythonw` background start, `%APPDATA%` paths) but I haven't tested it on real Windows hardware.

## Development

```bash
pip install -e ".[dev]"
```

```bash
pytest -q
```

61 tests run in about 16 s and never show real popups (a scripted "user" stands in for the UI):

- **Unit:** strict JSON-RPC parsing and classification, rules and remembered decisions, config validation, redaction, the broker (timeouts, cancellation, limits, out-of-order resolution, UI failure fails closed), session tracking (identity, tool catalogue, 2026-07-28 continuations), config rewrite round-trips, IPC authentication and file permissions.
- **End-to-end (real processes):** the official MCP SDK client → shim → demo server, in both handshake and 2026-07-28 modes; concurrent calls decided in reverse order; traffic flowing while a call waits; cancellation; smuggling attempts; batches; daemon absent / dying mid-request; server crash exit codes; and the same flows through the HTTP gateway (including client disconnects and wrong secrets).

Manually verified on macOS: the Tk popup (topmost, cascading, arming, Allow click, withdrawal); the `start` → `status` → timeout → `stop` lifecycle; the daemon auto-starting from a shim; the real `@modelcontextprotocol/server-filesystem` (npx) through the shim; and the public DeepWiki remote server through the gateway.

## Troubleshooting

- `mcp-guardian doctor` checks everything below.
- **No popup?** Run `mcp-guardian status`; is the daemon running and is your session listed? Check `~/.mcp-guardian/logs/daemon.log`.
- **Server fails in Claude Desktop?** Claude's logs are in `~/Library/Logs/Claude/mcp-server-<name>.log` (macOS) or `%APPDATA%\Claude\logs` (Windows). Shim messages are prefixed `[mcp-guardian shim:<name>]`.
- **Moved or recreated the virtualenv?** Re-run `mcp-guardian install`; it re-points guarded servers at the current interpreter.
- **Undo everything:** `mcp-guardian uninstall && mcp-guardian stop`.

## Project layout

```
src/mcp_guardian/
  shim.py           stdio proxy launched by Claude Desktop (enforcement point)
  gateway.py        Streamable-HTTP reverse proxy for claude.ai connectors (enforcement point)
  interceptor.py    shared MCP inspection: identity, tool catalogue, continuations, block responses
  jsonrpc.py        strict JSON-RPC parsing/classification
  client.py         IPC client used by shim and gateway (reconnect, auto-start, fail mode)
  ipc.py            framing + mutual HMAC authentication, Unix socket / loopback TCP
  daemon.py         decision daemon (policy decision point), sessions, status, monitor
  broker.py         pending approvals: rules → popup → timeout / cancellation / limits
  policy.py         glob rules and remembered decisions
  audit.py          JSONL audit log with redaction and rotation, event fan-out
  claude_config.py  safe, reversible rewrite of claude_desktop_config.json
  ui/               Tk popups (default), macOS AppleScript fallback, console UI
  demo/server.py    demo MCP server (official SDK): read-only, write, destructive, slow tools
tests/              unit + end-to-end tests
docs/               architecture and demo script
```

## License

MIT
