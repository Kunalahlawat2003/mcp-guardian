"""Demo MCP server ("guardian-demo") built with the official MCP Python SDK.

Tools cover the interesting cases for the approval popup:

* ``get_weather`` / ``list_notes`` / ``read_note``: read-only
* ``write_note``: modifies data
* ``delete_note``: destructive
* ``run_shell_command``: looks dangerous but is a SIMULATION: it only echoes
  the command back and never executes anything
* ``slow_task``: takes a while (useful for concurrency demos)

Notes live in ``~/.mcp-guardian/demo-notes`` (or $MCP_GUARDIAN_DEMO_NOTES).

Run over stdio (Claude Desktop):  python -m mcp_guardian.demo.server
Run over Streamable HTTP:         python -m mcp_guardian.demo.server --transport http --port 9000
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import os
import re
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover
    raise SystemExit("The demo server needs the MCP SDK: pip install 'mcp-guardian[demo]'") from exc

NOTES = Path(os.environ.get("MCP_GUARDIAN_DEMO_NOTES", Path.home() / ".mcp-guardian" / "demo-notes"))
_NAME = re.compile(r"^[A-Za-z0-9 _.-]{1,64}$")

server = MCPServer(
    "guardian-demo",
    version="1.0.0",
    instructions="Demo server for MCP Guardian: weather, a small notes store and a simulated shell.",
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITES = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)


def _note_path(name: str) -> Path:
    if not _NAME.match(name) or name.startswith("."):
        raise ValueError("note names may contain letters, digits, space, '_', '-' and '.' (max 64 chars)")
    NOTES.mkdir(parents=True, exist_ok=True)
    return NOTES / f"{name}.txt"


@server.tool(title="Get weather", description="Get the (simulated) current weather for a city.", annotations=READ_ONLY)
def get_weather(city: str) -> str:
    temp = 18 + sum(map(ord, city.lower())) % 15
    return f"{city}: {temp}°C, partly cloudy (demo data)"


@server.tool(title="List notes", description="List the notes in the demo notes folder.", annotations=READ_ONLY)
def list_notes() -> list[str]:
    NOTES.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in NOTES.glob("*.txt"))


@server.tool(title="Read note", description="Read a note from the demo notes folder.", annotations=READ_ONLY)
def read_note(name: str) -> str:
    path = _note_path(name)
    if not path.exists():
        raise ValueError(f"note {name!r} does not exist")
    return path.read_text(encoding="utf-8")


@server.tool(title="Write note", description="Create or overwrite a note in the demo notes folder.", annotations=WRITES)
def write_note(name: str, content: str) -> str:
    path = _note_path(name)
    path.write_text(content, encoding="utf-8")
    return f"saved {len(content)} characters to {path}"


@server.tool(title="Delete note", description="Permanently delete a note from the demo notes folder.", annotations=DESTRUCTIVE)
def delete_note(name: str) -> str:
    path = _note_path(name)
    if not path.exists():
        raise ValueError(f"note {name!r} does not exist")
    path.unlink()
    return f"deleted {name}"


@server.tool(
    title="Run shell command",
    description="Run a shell command on the user's computer. DEMO ONLY: this is a simulation and nothing is executed.",
    annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True),
)
def run_shell_command(command: str) -> str:
    return f"[simulation] would have executed: {command!r} (nothing was run)"


@server.tool(title="Slow task", description="Wait for a number of seconds, then report back.", annotations=READ_ONLY)
async def slow_task(seconds: float = 2.0) -> str:
    seconds = max(0.0, min(float(seconds), 60.0))
    await asyncio.sleep(seconds)
    return f"finished after {seconds:g}s at {_dt.datetime.now().strftime('%H:%M:%S')}"


@server.resource("notes://{name}", title="Note", description="A note from the demo notes folder", mime_type="text/plain")
def note_resource(name: str) -> str:
    return read_note(name)


@server.prompt(title="Summarize notes", description="Ask Claude to summarize all demo notes.")
def summarize_notes() -> str:
    names = list_notes()
    return "Summarize these notes: " + (", ".join(names) if names else "(there are no notes yet)")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000)
    args = parser.parse_args(argv)
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run("streamable-http", host=args.host, port=args.port, streamable_http_path="/mcp")


if __name__ == "__main__":
    main()
