"""One round-trip helper for ``host.mcp_config``.

An MCP server is only usable on the machine where the harness CLI actually
runs, so every Settings → MCP Servers operation is proxied to a *host* rather
than applied to the server's own config. The request-id / future / frame /
timeout / cleanup shape lives here once, and the two routes that use it
(:mod:`omnigent.server.routes.mcp_config` and
:mod:`omnigent.server.routes.mcp_probe`) differ only in the op they send.

Failures arrive as a status + code + message the host chose, so the route can
pass the host's own HTTP status through instead of flattening every failure
into a 500. A host that never answers is a different condition entirely — the
config was neither read nor written — and raises
:class:`HostMcpUnavailableError`, which the routes map to 502.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from omnigent.host.frames import HostMcpConfigFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry

# A probe spawns an MCP server and waits for it to initialize and list tools,
# so it is allowed considerably longer than a config read or write.
DEFAULT_TIMEOUT_S = 20.0
PROBE_TIMEOUT_S = 45.0


class HostMcpError(Exception):
    """The host ran the operation and refused it.

    :param status: HTTP status the failure deserves, e.g. ``404``.
    :param code: Machine-readable failure code, e.g. ``"not_found"``.
    :param message: Human-readable, non-secret detail.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class HostMcpUnavailableError(Exception):
    """The host could not be reached, so the operation never ran.

    Distinct from :class:`HostMcpError`: nothing was read or written, and the
    caller should say the machine is unreachable rather than report a config
    result it never got.
    """


async def mcp_config_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    op: str,
    harness: str | None = None,
    name: str | None = None,
    spec: dict[str, Any] | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """
    Send a ``host.mcp_config`` frame and await the host's result.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection to act on.
    :param op: ``"list"``, ``"add"``, ``"remove"``, or ``"probe"``.
    :param harness: Harness CLI to act on, e.g. ``"claude"``. ``None`` on a
        ``list``, which covers every known harness.
    :param name: MCP server name for ``add`` / ``remove`` / ``probe``.
    :param spec: Full server definition on an ``add``. Carries secret values
        inbound; they are never echoed back.
    :param timeout_s: Seconds to wait. Defaults to :data:`PROBE_TIMEOUT_S`
        for a probe (it spawns a server) and :data:`DEFAULT_TIMEOUT_S`
        otherwise.
    :returns: The host's result payload, free of secret values.
    :raises HostMcpUnavailableError: The connection dropped or the host did
        not answer in time — the operation never ran.
    :raises HostMcpError: The host ran the operation and refused it.
    """
    if timeout_s is None:
        timeout_s = PROBE_TIMEOUT_S if op == "probe" else DEFAULT_TIMEOUT_S

    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_mcp_config[request_id] = future
    frame = encode_host_frame(
        HostMcpConfigFrame(
            request_id=request_id,
            op=op,
            harness=harness,
            name=name,
            spec=spec,
        )
    )
    try:
        host_registry.send_text(host_conn, frame)
        result = await asyncio.wait_for(future, timeout=timeout_s)
    except ConnectionError as exc:
        raise HostMcpUnavailableError("the host connection dropped") from exc
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise HostMcpUnavailableError(f"the host did not answer within {timeout_s:g}s") from exc
    finally:
        host_conn.pending_mcp_config.pop(request_id, None)

    if result.get("status") != "ok":
        raise HostMcpError(
            int(result.get("error_status") or 500),
            str(result.get("error_code") or "host_error"),
            str(result.get("error") or "the host could not complete the operation"),
        )
    payload = result.get("payload")
    return payload if isinstance(payload, dict) else {}
