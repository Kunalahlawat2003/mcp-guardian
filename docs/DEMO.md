# Demo video script (about 5 minutes)

A shot list for recording the demonstration. It covers approval, blocking, concurrent requests, cancellation and claude.ai. Record with QuickTime (File → New Screen Recording) or ⌘⇧5 on macOS.

## Before recording

```bash
cd mcp-guardian && source .venv/bin/activate
```

```bash
mcp-guardian stop; mcp-guardian start
```

```bash
mcp-guardian add-demo        # adds the "guardian-demo" server to Claude Desktop, already guarded
```

```bash
mcp-guardian install         # also guard any other servers you have (optional)
```

Then fully quit and reopen Claude Desktop (⌘Q). In a terminal next to Claude, run the live monitor:

```bash
mcp-guardian monitor
```

For the claude.ai part, prepare three terminals in advance:

```bash
mcp-guardian demo-server --transport http --port 9000
```

```bash
mcp-guardian gateway --upstream demo=http://127.0.0.1:9000/mcp
```

```bash
cloudflared tunnel --url http://127.0.0.1:8787
```

In claude.ai: **Settings → Connectors → Add custom connector** → name "Guardian demo", URL `https://<tunnel-host>/mcp/demo/<secret>` (the secret is printed by the gateway). Enable it in a new chat.

## Shot list

| # | Time | Show | Say |
|---|---|---|---|
| 1 | 0:00 | README diagram | "MCP Guardian puts a human approval step in front of every MCP tool call from Claude Desktop and claude.ai." |
| 2 | 0:20 | `mcp-guardian servers` and `cat ~/Library/Application\ Support/Claude/claude_desktop_config.json` | "install rewrote Claude Desktop's config so each local server is started through the guardian shim. The original command sits after `--`, and uninstall restores it exactly." |
| 3 | 0:45 | Claude Desktop: *"Use guardian-demo to save a note called groceries with: milk, eggs, coffee."* | Popup appears on top. Point at: requested by Claude Desktop, server + command line, tool `write_note`, description, MODIFIES DATA badge, exact arguments, countdown. |
| 4 | 1:10 | Click **Allow** | Claude confirms; the monitor shows REQUEST → ALLOW → COMPLETED. Optionally `cat ~/.mcp-guardian/demo-notes/groceries.txt`. |
| 5 | 1:30 | *"Now delete the groceries note."* | Red DESTRUCTIVE popup. Click **Block**. Claude says the action was blocked and not executed. Show that the file still exists. |
| 6 | 2:00 | *"Run the shell command `rm -rf ~/Documents` using guardian-demo."* (the tool is a harmless simulation) | Block it. "This is the scenario this tool exists for." |
| 7 | 2:20 | *"Get the weather for Paris, Tokyo and Lima at the same time."* | Three cascaded popups, "1 of 3 / 2 of 3 / 3 of 3 pending". Allow Paris, Block Tokyo, Allow Lima, in a different order than they arrived. Claude gets two results and one denial. |
| 8 | 2:50 | Ask for a weather call again, tick **Remember my choice**, then Allow | "Remembered for this tool until Guardian restarts; waiting requests with the same arguments are settled too, while different arguments still get their own popup." Ask again: no popup, and the monitor shows `[remembered]`. |
| 9 | 3:10 | `mcp-guardian status` | Sessions identified as Claude Desktop, pending requests, remembered decisions, recent events. |
| 10 | 3:25 | claude.ai chat with the "Guardian demo" connector: *"Use the Guardian demo connector to write a note called web-test saying hello from claude.ai."* | Popup now says **Claude (claude.ai / remote connector)**, transport http, upstream URL, remote peer. **Allow**, and the result appears in claude.ai. |
| 11 | 3:55 | claude.ai: *"Delete the web-test note."* | **Block**. claude.ai shows the tool error. |
| 12 | 4:15 | Resilience: `mcp-guardian stop`, then ask Claude Desktop for a tool call | The shim restarts the daemon automatically and the popup still appears. (Or set `auto_start_daemon = false` to show fail-closed: the call is blocked with "approval service is not running".) |
| 13 | 4:40 | `pytest -q` | "61 tests, including end-to-end runs through real processes with the official MCP SDK client." |
| 14 | 4:55 | `mcp-guardian uninstall` | "One command restores the original configuration." |

## Tips

- If Claude Desktop has a "Allow for this chat" permission prompt for the tool, approve it first. That's Claude's own UI; Guardian's popup is the independent, user-controlled gate behind it.
- Keep the monitor terminal visible: it shows the monitoring side (every request, decision and completion), not just the popups.
- To show a timeout quickly, set `approval_timeout = 15` in `~/.mcp-guardian/config.toml` and restart the daemon.
