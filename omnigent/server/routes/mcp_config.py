"""Routes for managing a host's harness MCP server config (``/v1/mcp-config``).

Backs the Settings → Agent Config MCP surface: list, add, remove, and
live-probe the MCP servers registered with the harness CLIs on a *host*.

Why host-backed rather than server-local: an MCP server is only usable if it
is registered on the machine where the harness CLI actually runs. In the Docker
stack that is the host container; the server container's own ``~/.claude.json``
is read by no agent. An earlier version of this module read the server's file
directly, which meant the Settings screen listed config no session could ever
see. Every operation here is proxied over the host tunnel to
:mod:`omnigent.harness_mcp_config`, which drives each CLI's own ``mcp``
subcommand so the on-disk format stays owned by that CLI.

``host_id`` is an optional query parameter. When omitted the caller's single
online host is used, which is the common case (and the whole standalone Docker
stack); an explicit id is required only when several hosts are online, so the
API never silently writes config to an arbitrary machine.

Writes take effect for the next session launched on that host — a running
harness process reads its MCP config at startup, so an existing session must
be restarted to pick up a change. The response says so explicitly rather than
leaving the user to wonder why a new server did not appear.

Security contract, unchanged from the read-only version: NO secret VALUES are
ever returned. Env is reduced to key NAMES, credential-shaped args and headers
are redacted, and URL userinfo + query strings are stripped. Secrets travel
inbound only. The routes are admin-gated in multi-user mode — this is the
operator's machine config, and adding a stdio server is arbitrary command
execution on the host at the next session start.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_mcp import (
    HostMcpError,
    HostMcpUnavailableError,
    mcp_config_on_host,
)
from omnigent.server.routes.default_policies import _require_admin
from omnigent.stores.host_store import HostStore, host_is_live
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# Shown after a successful write so the user knows why a running session does
# not see the change yet.
_RESTART_HINT = (
    "Start a new session (or restart a running one) on this host to use the change — "
    "harness processes read their MCP config at startup."
)


class McpServerSpec(BaseModel):
    """Request body for adding an MCP server.

    :param harness: Target harness CLI — ``"claude"`` or ``"codex"``.
    :param name: Server name, unique per harness.
    :param transport: ``"stdio"`` (spawns ``command``) or ``"http"``/``"sse"``
        (connects to ``url``).
    :param command: Executable for a stdio server, e.g. ``"npx"``.
    :param args: Arguments for a stdio server.
    :param env: Environment variables for a stdio server. Values ARE secrets
        and are never echoed back.
    :param url: Endpoint for an http-family server.
    :param headers: Extra headers for an http-family server (e.g.
        ``Authorization``). Values ARE secrets and are never echoed back.
    """

    harness: str = Field(default="claude")
    name: str
    transport: str = Field(default="stdio")
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)


def _spec_payload(body: McpServerSpec) -> dict[str, Any]:
    """Build the tunnel spec dict from a request body.

    :param body: The validated request body.
    :returns: The spec the host handler expects.
    """
    return {
        "transport": body.transport,
        "command": body.command,
        "args": body.args,
        "env": body.env,
        "url": body.url,
        "headers": body.headers,
    }


def create_mcp_config_router(
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    host_store: HostStore | None = None,
    host_registry: HostRegistry | None = None,
) -> APIRouter:
    """Build the router for ``/v1/mcp-config/servers``.

    Mounted with ``prefix="/v1"``.

    :param auth_provider: Optional auth provider; when set, the caller must
        be authenticated.
    :param permission_store: Permission store for the admin check; ``None``
        (single-user mode) skips the admin gate.
    :param host_store: Host store used to resolve the target host. When
        ``None`` the MCP endpoints report 503 — there is no host to manage.
    :param host_registry: Registry holding live host connections.
    :returns: A FastAPI router exposing the MCP server CRUD + probe.
    """
    router = APIRouter()

    async def _resolve_host(request: Request, host_id: str | None) -> HostConnection:
        """Resolve the host connection whose MCP config to manage.

        :param request: The incoming request (for owner scoping).
        :param host_id: Explicit host id, or ``None`` to auto-select the
            caller's single online host.
        :returns: The live host connection.
        :raises HTTPException: 503 when host support is unconfigured, 404 for
            an unknown host, 403 for someone else's host, 409 when the host
            is offline, and 400 when several hosts are online and no
            ``host_id`` was given.
        """
        if host_store is None or host_registry is None:
            raise HTTPException(
                status_code=503,
                detail="host support is not configured on this server",
            )
        user_id = require_user(request, auth_provider)
        owner = user_id if user_id is not None else "local"

        if host_id is not None:
            host = await asyncio.to_thread(host_store.get_host, host_id)
            # 404 rather than 403 for a non-owner would leak existence, so
            # mirror the hosts routes: unknown → 404, known-but-theirs → 403.
            if host is None:
                raise HTTPException(status_code=404, detail="host not found")
            # Owner-only hosts reject non-owners; workspace hosts are
            # accessible to any authenticated user (caller gates admin).
            if user_id is not None and host.visibility == "owner" and host.owner != user_id:
                raise HTTPException(status_code=403, detail="not your host")
            conn = host_registry.get(host.host_id)
            if conn is None:
                raise HTTPException(status_code=409, detail="host is offline")
            return conn

        hosts = await asyncio.to_thread(host_store.list_hosts, owner)
        # Only registry-live hosts qualify: this replica must hold the
        # connection to proxy a frame, so a DB-online host attached to
        # another replica is not usable here.
        live = [h for h in hosts if host_is_live(h) and host_registry.get(h.host_id)]
        if not live:
            raise HTTPException(
                status_code=409,
                detail="no online host to manage MCP config on",
            )
        if len(live) > 1:
            names = ", ".join(sorted(h.host_id for h in live))
            raise HTTPException(
                status_code=400,
                detail=f"several hosts are online; pass ?host_id= to choose one ({names})",
            )
        conn = host_registry.get(live[0].host_id)
        if conn is None:  # pragma: no cover — filtered above; races to offline
            raise HTTPException(status_code=409, detail="host is offline")
        return conn

    async def _require_mcp_access(request: Request, host_id: str | None) -> HostConnection:
        """Resolve a host and enforce visibility-based MCP config access.

        Owner hosts are manageable by their owner. Workspace hosts require
        admin privileges.

        :param request: The incoming request (for auth + admin check).
        :param host_id: Explicit host id, or ``None`` for auto-select.
        :returns: The live host connection.
        :raises HTTPException: 403 when a non-admin tries to manage a
            workspace host's MCP config.
        """
        conn = await _resolve_host(request, host_id)
        host = await asyncio.to_thread(host_store.get_host, conn.host_id)
        # This version's Host has no visibility column (every host is
        # owner-scoped); tolerate its absence rather than 500.
        if host is not None and getattr(host, "visibility", None) == "workspace":
            await _require_admin(request, auth_provider, permission_store)
        return conn

    async def _proxy(
        conn: HostConnection,
        *,
        op: str,
        harness: str | None = None,
        name: str | None = None,
        spec: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run one MCP config op on the host, mapping errors to HTTP.

        :param conn: The live host connection.
        :param op: ``"list"`` / ``"add"`` / ``"remove"`` / ``"probe"``.
        :param harness: Target harness.
        :param name: Server name.
        :param spec: Server definition for ``"add"``.
        :returns: The host's payload.
        :raises HTTPException: Reproducing the host's status, or 502 when the
            host could not be reached.
        """
        assert host_registry is not None  # guaranteed by _resolve_host
        try:
            return await mcp_config_on_host(
                host_registry=host_registry,
                host_conn=conn,
                op=op,
                harness=harness,
                name=name,
                spec=spec,
            )
        except HostMcpError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
        except HostMcpUnavailableError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @router.get("/mcp-config/servers")
    async def list_mcp_servers(
        request: Request,
        host_id: str | None = Query(default=None),
        harness: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """List MCP servers registered with the harness CLIs on a host.

        Returns each server's name, harness, transport, command/url, env key
        names (never values), and a resolvability status.

        :param request: The incoming request (for auth).
        :param host_id: Host to read; omit to use the single online host.
        :param harness: Restrict to one harness; omit for all.
        :returns: ``{"data": [...], "host_id": str}``.
        """
        conn = await _require_mcp_access(request, host_id)
        payload = await _proxy(conn, op="list", harness=harness)
        servers = payload.get("servers")
        return {
            "data": servers if isinstance(servers, list) else [],
            "host_id": conn.host_id,
        }

    @router.post("/mcp-config/servers")
    async def add_mcp_server(
        request: Request,
        body: McpServerSpec,
        host_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Register an MCP server with a harness CLI on a host.

        A stdio server is an arbitrary command that the harness will execute
        on the host at the next session start. On workspace hosts, admin is
        required; on personal hosts, the owner may manage their own config.

        :param request: The incoming request (for auth).
        :param body: The server definition. Secret values in ``env`` /
            ``headers`` are written to the host's config and never echoed.
        :param host_id: Host to write; omit to use the single online host.
        :returns: ``{"name", "harness", "host_id", "restart_required",
            "detail"}``.
        """
        conn = await _require_mcp_access(request, host_id)
        payload = await _proxy(
            conn,
            op="add",
            harness=body.harness,
            name=body.name,
            spec=_spec_payload(body),
        )
        return {
            "name": payload.get("name", body.name),
            "harness": payload.get("harness", body.harness),
            "host_id": conn.host_id,
            "restart_required": True,
            "detail": _RESTART_HINT,
        }

    @router.delete("/mcp-config/servers/{name}")
    async def remove_mcp_server(
        request: Request,
        name: str,
        harness: str = Query(default="claude"),
        host_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Unregister an MCP server from a harness CLI on a host.

        :param request: The incoming request (for auth).
        :param name: The server name to remove.
        :param harness: Which harness's config to remove it from.
        :param host_id: Host to write; omit to use the single online host.
        :returns: ``{"name", "harness", "host_id", "restart_required",
            "detail"}``.
        """
        conn = await _require_mcp_access(request, host_id)
        payload = await _proxy(conn, op="remove", harness=harness, name=name)
        return {
            "name": payload.get("name", name),
            "harness": payload.get("harness", harness),
            "host_id": conn.host_id,
            "restart_required": True,
            "detail": _RESTART_HINT,
        }

    @router.put("/mcp-config/servers/{name}")
    async def update_mcp_server(
        request: Request,
        name: str,
        body: McpServerSpec,
        host_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Update an existing MCP server on a host (remove + re-add).

        :param request: The incoming request (for auth).
        :param body: The new server definition. The ``name`` in the path is
            authoritative; ``body.name`` is ignored.
        :param host_id: Host to write; omit to use the single online host.
        :returns: ``{"name", "harness", "host_id", "restart_required",
            "detail"}``.
        """
        conn = await _require_mcp_access(request, host_id)
        payload = await _proxy(
            conn,
            op="update",
            harness=body.harness,
            name=name,
            spec=_spec_payload(body),
        )
        return {
            "name": payload.get("name", name),
            "harness": payload.get("harness", body.harness),
            "host_id": conn.host_id,
            "restart_required": True,
            "detail": _RESTART_HINT,
        }

    @router.post("/mcp-config/servers/{name}/probe")
    async def probe_server(
        request: Request,
        name: str,
        harness: str = Query(default="claude"),
        host_id: str | None = Query(default=None),
    ) -> dict[str, Any]:
        """Live-probe ONE MCP server on the host for real usability.

        Launches the named server the way a runner would (augmented PATH + its
        own env), runs the MCP ``initialize`` handshake, and lists its tools —
        the definitive "usable from Omnigent" check, stronger than the
        PATH-resolvability status the list returns. Runs on the host, where the
        server would actually be spawned, so a probe that passes here means it
        will work in a session. Per-request and opt-in, never fired by the
        list, so loading Settings does not spawn a process per server.

        :param request: The incoming request (for auth).
        :param name: The server name to probe.
        :param harness: Which harness's config the server is in.
        :param host_id: Host to probe on; omit to use the single online host.
        :returns: ``{"name", "usable": bool, "tool_count": int, "detail": str}``.
        """
        conn = await _require_mcp_access(request, host_id)
        return await _proxy(conn, op="probe", harness=harness, name=name)

    # No raw config file endpoint, deliberately. Returning the file's text
    # would hand back every server's env values and Authorization headers —
    # and for ~/.claude.json, OAuth account data and project history besides —
    # breaking this module's contract that no secret value is ever returned.
    # Editing is per-server: add, update, remove.

    return router
