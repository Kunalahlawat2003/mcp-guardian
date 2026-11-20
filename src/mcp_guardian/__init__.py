"""MCP Guardian: a human-in-the-loop firewall for Model Context Protocol traffic.

Claude Desktop launches local MCP servers over stdio and claude.ai reaches remote
MCP servers over Streamable HTTP. MCP Guardian sits in both paths as a
protocol-aware proxy, holds every sensitive request (``tools/call``,
``resources/read``, ``prompts/get`` by default), asks the user in an
always-on-top popup, and only then forwards or refuses the request.
"""

__version__ = "0.1.0"
