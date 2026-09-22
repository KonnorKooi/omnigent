"""Live usability probe for a single MCP server from ``~/.claude.json``.

The read-only listing in :mod:`omnigent.server.routes.mcp_config` reports a
server's PATH *resolvability* (does ``shutil.which`` find the command). That
answers "could this launch", not "does this actually work from Omnigent" — an
``npx`` command resolves even when the package fails to download, the token is
wrong, or the server crashes on startup.

This module answers the stronger question by doing what a runner does: it
launches the server with the SAME augmented PATH + env a runner would give it,
performs the MCP ``initialize`` handshake, and lists the tools. If that round
trip succeeds the server is genuinely usable from Omnigent.

Design constraints:
- **Never raises.** Every failure mode (spawn error, timeout, protocol error,
  bad config) is caught and returned as ``usable=False`` with a short reason.
- **Bounded.** A hard timeout caps the whole handshake so a hanging server
  can't wedge the request.
- **No secrets in the result.** Only a boolean, a tool count, and a short
  human reason string are returned — never env values or the launch command.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

_logger = logging.getLogger(__name__)

# Hard cap on the whole probe (spawn + initialize + list_tools). npx servers
# that must download a package on first run are the slow case; 30s covers cold
# starts (package download + auth handshake) while still bounding a real hang.
_PROBE_TIMEOUT_SECONDS = 30.0

# HTTP-family transport type values. Kept as a local copy (not imported from
# mcp_config) to avoid an import cycle: mcp_config imports this module at load
# time. Must stay in sync with mcp_config._HTTP_TRANSPORT_TYPES.
_HTTP_TRANSPORT_TYPES: frozenset[str] = frozenset(
    {"http", "sse", "streamable-http", "streamableHttp"}
)


def _augmented_env(entry_env: Any) -> dict[str, str]:
    """Build the child env a runner would hand an MCP server.

    Starts from the current process env, then overlays the server's own
    ``env`` block from the harness config (which carries its tokens).

    PATH is inherited as-is, deliberately. A runner on this host receives
    PATH straight through the allowlist in
    :data:`omnigent.host.connect._RUNNER_ENV_ALLOWLIST` with no extra
    bin-dir prepended, so prepending one here would let a server probe as
    usable on a PATH no runner will ever hand it — the probe would then be
    reporting on an environment that does not exist.

    :param entry_env: The raw ``env`` value from the server entry (dict or
        anything else — non-dicts are ignored).
    :returns: A complete environment mapping for the child process.
    """
    env = dict(os.environ)
    if isinstance(entry_env, dict):
        for key, value in entry_env.items():
            # Env values are strings; coerce defensively and skip Nones.
            if value is not None:
                env[str(key)] = str(value)
    return env


async def _probe_stdio(entry: dict[str, Any]) -> dict[str, Any]:
    """Probe a stdio MCP server: spawn, initialize, list tools.

    :param entry: The raw server entry (must have a ``command``).
    :returns: A result dict — see :func:`probe_mcp_server`.
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = str(entry["command"])
    args = [str(a) for a in entry.get("args", []) or []]
    params = StdioServerParameters(
        command=command,
        args=args,
        env=_augmented_env(entry.get("env")),
    )

    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        await session.initialize()
        tools = await session.list_tools()
        return {
            "usable": True,
            "tool_count": len(tools.tools),
            "detail": "initialized",
        }


async def _probe_http(entry: dict[str, Any]) -> dict[str, Any]:
    """Probe an http/sse MCP server: connect, initialize, list tools.

    :param entry: The raw server entry (must have a ``url``).
    :returns: A result dict — see :func:`probe_mcp_server`.
    """
    from mcp import ClientSession

    raw_url = entry.get("url")
    if not raw_url:
        return {"usable": False, "tool_count": 0, "detail": "no url configured"}
    url = str(raw_url)
    transport_type = entry.get("type", "")
    headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else None

    if transport_type == "sse":
        from mcp.client.sse import sse_client

        client_cm = sse_client(url, headers=headers)
    else:
        from mcp.client.streamable_http import streamablehttp_client

        client_cm = streamablehttp_client(url, headers=headers)

    async with client_cm as streams:
        # streamablehttp_client yields a 3-tuple; sse_client a 2-tuple.
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return {
                "usable": True,
                "tool_count": len(tools.tools),
                "detail": "initialized",
            }


async def probe_mcp_server(name: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Probe one MCP server for real usability. Never raises.

    Launches the server exactly as a runner would (augmented PATH + the
    entry's own env), performs the MCP ``initialize`` handshake, and lists
    tools. The whole round trip is bounded by :data:`_PROBE_TIMEOUT_SECONDS`.

    :param name: The server name (for the result + logging).
    :param entry: The raw ``mcpServers[name]`` dict from ``.claude.json``.
    :returns: ``{"name", "usable": bool, "tool_count": int, "detail": str}``.
        ``detail`` is a short human reason ("initialized", "timeout",
        "spawn failed: …") — never contains secrets.
    """
    result: dict[str, Any] = {"name": name, "usable": False, "tool_count": 0}
    try:
        is_stdio = entry.get("command") is not None
        transport_type = entry.get("type", "")
        is_http = (
            isinstance(transport_type, str) and transport_type in _HTTP_TRANSPORT_TYPES
        ) or (not is_stdio and entry.get("url") is not None)

        if is_stdio:
            coro = _probe_stdio(entry)
        elif is_http:
            coro = _probe_http(entry)
        else:
            result["detail"] = "unsupported transport"
            return result

        probed = await asyncio.wait_for(coro, timeout=_PROBE_TIMEOUT_SECONDS)
        result.update(probed)
        return result
    except asyncio.TimeoutError:
        result["detail"] = f"timeout after {_PROBE_TIMEOUT_SECONDS:.0f}s"
        return result
    except FileNotFoundError:
        result["detail"] = "command not found on PATH"
        return result
    except (KeyboardInterrupt, SystemExit):
        # Real interpreter shutdown signals — never swallow these.
        raise
    except BaseException as exc:  # noqa: BLE001 — probe must never raise
        # Catch BaseException (not just Exception): the MCP stdio/http clients
        # run inside an anyio task group, and cancellation on the timeout path
        # can surface a BaseExceptionGroup (holding CancelledError), which is
        # NOT an Exception subclass and would otherwise escape as a 500.
        _logger.debug("MCP probe failed for %r", name, exc_info=True)
        # Keep the reason short and free of any embedded config/secret text.
        result["detail"] = f"{type(exc).__name__}"
        return result
