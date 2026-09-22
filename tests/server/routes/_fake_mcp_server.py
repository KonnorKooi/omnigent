"""A minimal real MCP stdio server, launched by the probe tests.

The probe drives the actual ``mcp`` client SDK — it spawns the command,
performs the ``initialize`` handshake, and lists tools — so proving it works
needs a genuine server on the other end rather than a mock. This is the
smallest one that satisfies the handshake: a single ``ping`` tool.

Run as ``python _fake_mcp_server.py``; it speaks MCP over stdio and exits when
its stdin closes.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("fake")


@mcp.tool()
def ping() -> str:
    """Return a fixed string, so the tool list is non-empty."""
    return "pong"


if __name__ == "__main__":
    mcp.run()
