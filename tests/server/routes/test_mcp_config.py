"""Tests for the host-backed MCP config routes (``/v1/mcp-config/servers``).

The routes proxy every operation to a *host* over the host tunnel, because an
MCP server is only usable on the machine where the harness CLI runs. These
tests therefore stub the tunnel (``mcp_config_on_host``) and a live host
connection, and assert the HTTP contract around it:

- list / add / remove / probe reach the host with the right op and arguments.
- Secret values sent inbound are never echoed back in a response.
- Host failures map to their HTTP status; an unreachable host maps to 502.
- Host resolution: no host → 409, unknown → 404, someone else's → 403,
  several online → 400.
- Admin-gated in multi-user mode (adding a stdio server is remote command
  execution on the host at the next session start).

The sanitisation and argv-construction logic they used to cover now lives in
:mod:`omnigent.harness_mcp_config` and is tested in
``tests/test_harness_mcp_config.py``.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.routes import mcp_config as mcp_config_module
from omnigent.server.routes._host_mcp import HostMcpError, HostMcpUnavailableError
from omnigent.server.routes.mcp_config import create_mcp_config_router

pytestmark = pytest.mark.asyncio

# A value that MUST NOT appear anywhere in a JSON response body.
_SECRET_VALUE = "super-secret-token-should-never-appear-in-response"

_HOST_ID = "host-1"
_OWNER = "alice@example.com"
# Owner the routes scope to in single-user mode (no auth provider), where
# there is no user id to key hosts by.
_LOCAL_OWNER = "local"


class _FakeHostRecord:
    """Minimal Host stand-in: id, owner, status, visibility, and a heartbeat.

    ``host_is_live`` reads ``status`` and ``updated_at``, so an offline host
    needs a genuinely stale heartbeat rather than just a status string.
    """

    def __init__(
        self,
        host_id: str,
        owner: str,
        *,
        online: bool = True,
        visibility: str = "owner",
    ) -> None:
        self.host_id = host_id
        self.owner = owner
        self.status = "online" if online else "offline"
        self.updated_at = int(time.time()) if online else int(time.time()) - 3600
        self.visibility = visibility


class _FakeHostStore:
    """Minimal HostStore stand-in over an in-memory record list."""

    def __init__(self, records: list[_FakeHostRecord]) -> None:
        self._records = records

    def get_host(self, host_id: str) -> _FakeHostRecord | None:
        return next((r for r in self._records if r.host_id == host_id), None)

    def list_hosts(self, owner: str) -> list[_FakeHostRecord]:
        return [r for r in self._records if r.owner == owner]


class _FakeConn:
    """Stand-in for a live HostConnection."""

    def __init__(self, host_id: str) -> None:
        self.host_id = host_id
        self.pending_mcp_requests: dict[str, Any] = {}


class _FakeHostRegistry:
    """Minimal HostRegistry stand-in: holds connections by host id."""

    def __init__(self, conns: dict[str, _FakeConn]) -> None:
        self._conns = conns

    def get(self, host_id: str) -> _FakeConn | None:
        return self._conns.get(host_id)


class _FakePermissionStore:
    """Minimal PermissionStore stand-in: admin membership by user id set."""

    def __init__(self, admins: set[str]) -> None:
        self._admins = admins

    def is_admin(self, user_id: str) -> bool:
        return user_id in self._admins


class _RecordingProxy:
    """Stub for ``mcp_config_on_host``: records calls, returns/raises on cue.

    :param payload: Payload to return, or a callable taking the recorded call
        kwargs and returning one.
    :param error: Exception to raise instead of returning.
    """

    def __init__(self, payload: Any = None, error: BaseException | None = None) -> None:
        self._payload = payload
        self._error = error
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        if callable(self._payload):
            return self._payload(kwargs)
        return self._payload if self._payload is not None else {}

    @property
    def last(self) -> dict[str, Any]:
        """The most recent call's kwargs."""
        return self.calls[-1]


def _install_proxy(
    monkeypatch: pytest.MonkeyPatch,
    payload: Any = None,
    error: BaseException | None = None,
) -> _RecordingProxy:
    """Replace the tunnel call with a recording stub.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param payload: Payload the stub returns.
    :param error: Exception the stub raises instead.
    :returns: The installed stub, for call assertions.
    """
    proxy = _RecordingProxy(payload=payload, error=error)
    monkeypatch.setattr(mcp_config_module, "mcp_config_on_host", proxy)
    return proxy


def _make_app(
    *,
    hosts: list[_FakeHostRecord] | None = None,
    conns: dict[str, _FakeConn] | None = None,
    auth_provider: Any = None,
    permission_store: Any = None,
    with_host_support: bool = True,
) -> FastAPI:
    """Build a minimal app mounting only the mcp-config router.

    Registers the ``OmnigentError`` handler that ``create_app`` installs, so
    ``require_user``'s ``OmnigentError(UNAUTHORIZED)`` becomes an HTTP 401.

    :param hosts: Host records the store reports; defaults to one live host
        owned by ``_LOCAL_OWNER`` so single-user auto-selection finds it.
    :param conns: Registry-live connections; defaults to one for that host.
    :param auth_provider: Optional auth provider.
    :param permission_store: Optional permission store (enables the admin gate).
    :param with_host_support: When ``False``, omit the store and registry so
        the routes report 503.
    :returns: The app.
    """
    if hosts is None:
        owner = _OWNER if auth_provider is not None else _LOCAL_OWNER
        hosts = [_FakeHostRecord(_HOST_ID, owner)]
    if conns is None:
        conns = {_HOST_ID: _FakeConn(_HOST_ID)}

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_mcp_config_router(
            auth_provider=auth_provider,
            permission_store=permission_store,
            host_store=_FakeHostStore(hosts) if with_host_support else None,
            host_registry=_FakeHostRegistry(conns) if with_host_support else None,
        ),
        prefix="/v1",
    )
    return app


async def _client(app: FastAPI) -> httpx.AsyncClient:
    """Build an async client bound to ``app``.

    :param app: The app to serve.
    :returns: An unentered client (caller uses ``async with``).
    """
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


@pytest_asyncio.fixture()
async def mcp_client() -> AsyncIterator[httpx.AsyncClient]:
    """Client for a single-user app with one live host."""
    async with await _client(_make_app()) as c:
        yield c


# ── list ───────────────────────────────────────────────────────────────────────

# What the host returns for a list op: env reduced to key names by the host,
# never values.
_LIST_PAYLOAD = {
    "servers": [
        {
            "name": "my-stdio-server",
            "harness": "claude",
            "transport": "stdio",
            "status": "resolvable",
            "command": "npx",
            "args": ["-y", "@my/mcp-server"],
            "env_keys": ["API_TOKEN", "WORKSPACE_ID"],
        },
        {
            "name": "my-http-server",
            "harness": "claude",
            "transport": "http",
            "status": "configured",
            "url": "https://mcp.example.com/v1",
        },
    ]
}


async def test_list_returns_servers_and_host_id(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET returns the host's servers plus the host they came from."""
    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    resp = await mcp_client.get("/v1/mcp-config/servers")
    assert resp.status_code == 200
    body = resp.json()
    assert {s["name"] for s in body["data"]} == {"my-stdio-server", "my-http-server"}
    assert body["host_id"] == _HOST_ID


async def test_list_sends_list_op_to_host(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The list route proxies op='list' with no name or spec."""
    proxy = _install_proxy(monkeypatch, _LIST_PAYLOAD)
    await mcp_client.get("/v1/mcp-config/servers")
    assert proxy.last["op"] == "list"
    assert proxy.last["name"] is None


async def test_list_forwards_harness_filter(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """?harness= is forwarded so the host reads only that CLI's config."""
    proxy = _install_proxy(monkeypatch, _LIST_PAYLOAD)
    await mcp_client.get("/v1/mcp-config/servers?harness=codex")
    assert proxy.last["harness"] == "codex"


async def test_list_env_keys_without_values(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env key NAMES are listed; the host never sends values, so none appear."""
    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    resp = await mcp_client.get("/v1/mcp-config/servers")
    servers = {s["name"]: s for s in resp.json()["data"]}
    assert servers["my-stdio-server"]["env_keys"] == ["API_TOKEN", "WORKSPACE_ID"]
    assert "env" not in servers["my-stdio-server"]


async def test_list_tolerates_missing_servers_key(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A payload without 'servers' yields an empty list, not a 500."""
    _install_proxy(monkeypatch, {})
    resp = await mcp_client.get("/v1/mcp-config/servers")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


async def test_list_tolerates_non_list_servers(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A malformed 'servers' value yields an empty list, not a 500."""
    _install_proxy(monkeypatch, {"servers": "nope"})
    resp = await mcp_client.get("/v1/mcp-config/servers")
    assert resp.status_code == 200
    assert resp.json()["data"] == []


# ── add ────────────────────────────────────────────────────────────────────────


async def test_add_forwards_spec_to_host(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST proxies op='add' with the harness, name, and full spec."""
    proxy = _install_proxy(monkeypatch, {"name": "gh", "harness": "claude"})
    resp = await mcp_client.post(
        "/v1/mcp-config/servers",
        json={
            "harness": "claude",
            "name": "gh",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": _SECRET_VALUE},
        },
    )
    assert resp.status_code == 200
    assert proxy.last["op"] == "add"
    assert proxy.last["harness"] == "claude"
    assert proxy.last["name"] == "gh"
    # The secret must reach the host — inbound is the only direction it travels.
    assert proxy.last["spec"]["env"] == {"GITHUB_TOKEN": _SECRET_VALUE}
    assert proxy.last["spec"]["command"] == "npx"


async def test_add_never_echoes_secrets(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The add response contains no submitted secret value."""
    _install_proxy(monkeypatch, {"name": "gh", "harness": "claude"})
    resp = await mcp_client.post(
        "/v1/mcp-config/servers",
        json={
            "harness": "claude",
            "name": "gh",
            "transport": "stdio",
            "command": "npx",
            "env": {"GITHUB_TOKEN": _SECRET_VALUE},
        },
    )
    assert _SECRET_VALUE not in resp.text, (
        "A submitted secret was echoed back — the write path must be one-way."
    )


async def test_add_header_secret_never_echoed(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Authorization header value is not echoed in the add response."""
    _install_proxy(monkeypatch, {"name": "sentry", "harness": "claude"})
    resp = await mcp_client.post(
        "/v1/mcp-config/servers",
        json={
            "harness": "claude",
            "name": "sentry",
            "transport": "http",
            "url": "https://mcp.example.com/mcp",
            "headers": {"Authorization": f"Bearer {_SECRET_VALUE}"},
        },
    )
    assert _SECRET_VALUE not in resp.text


async def test_add_reports_restart_required(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful add says a restart is needed — harnesses read MCP at boot."""
    _install_proxy(monkeypatch, {"name": "gh", "harness": "claude"})
    resp = await mcp_client.post(
        "/v1/mcp-config/servers",
        json={"harness": "claude", "name": "gh", "transport": "stdio", "command": "npx"},
    )
    body = resp.json()
    assert body["restart_required"] is True
    assert body["detail"]
    assert body["host_id"] == _HOST_ID


async def test_add_maps_host_rejection_to_status(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host-side rejection keeps its status and message."""
    _install_proxy(
        monkeypatch,
        error=HostMcpError(400, "harness_rejected", "a server named gh already exists"),
    )
    resp = await mcp_client.post(
        "/v1/mcp-config/servers",
        json={"harness": "claude", "name": "gh", "transport": "stdio", "command": "npx"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "a server named gh already exists"


async def test_add_maps_missing_cli_to_503(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host reporting an absent CLI surfaces as 503, not 500."""
    _install_proxy(
        monkeypatch,
        error=HostMcpError(503, "harness_unavailable", "the 'codex' CLI is not installed"),
    )
    resp = await mcp_client.post(
        "/v1/mcp-config/servers",
        json={"harness": "codex", "name": "gh", "transport": "stdio", "command": "npx"},
    )
    assert resp.status_code == 503


async def test_add_maps_unreachable_host_to_502(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tunnel timeout / connection loss is a 502 — the host never answered."""
    _install_proxy(monkeypatch, error=HostMcpUnavailableError("host did not respond"))
    resp = await mcp_client.post(
        "/v1/mcp-config/servers",
        json={"harness": "claude", "name": "gh", "transport": "stdio", "command": "npx"},
    )
    assert resp.status_code == 502


# ── remove ─────────────────────────────────────────────────────────────────────


async def test_remove_forwards_name_and_harness(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DELETE proxies op='remove' with the name and requested harness."""
    proxy = _install_proxy(monkeypatch, {"name": "gh", "harness": "codex"})
    resp = await mcp_client.delete("/v1/mcp-config/servers/gh?harness=codex")
    assert resp.status_code == 200
    assert proxy.last["op"] == "remove"
    assert proxy.last["name"] == "gh"
    assert proxy.last["harness"] == "codex"


async def test_remove_defaults_to_claude(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting ?harness= targets claude, matching the list default."""
    proxy = _install_proxy(monkeypatch, {"name": "gh", "harness": "claude"})
    await mcp_client.delete("/v1/mcp-config/servers/gh")
    assert proxy.last["harness"] == "claude"


async def test_remove_unknown_server_is_404(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host's 'not configured' answer surfaces as 404."""
    _install_proxy(
        monkeypatch,
        error=HostMcpError(404, "not_found", "MCP server 'nope' is not configured for claude"),
    )
    resp = await mcp_client.delete("/v1/mcp-config/servers/nope")
    assert resp.status_code == 404


# ── probe ──────────────────────────────────────────────────────────────────────


async def test_probe_returns_host_verdict(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe result is passed through verbatim from the host."""
    _install_proxy(
        monkeypatch,
        {"name": "gh", "usable": True, "tool_count": 7, "detail": "listed 7 tools"},
    )
    resp = await mcp_client.post("/v1/mcp-config/servers/gh/probe")
    assert resp.status_code == 200
    assert resp.json() == {
        "name": "gh",
        "usable": True,
        "tool_count": 7,
        "detail": "listed 7 tools",
    }


async def test_probe_sends_probe_op(
    mcp_client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe route proxies op='probe' with the server name.

    The probe must run on the host — that is where the server would actually
    be spawned, so a pass there means it will work in a session.
    """
    proxy = _install_proxy(monkeypatch, {"name": "gh", "usable": False, "tool_count": 0})
    await mcp_client.post("/v1/mcp-config/servers/gh/probe?harness=codex")
    assert proxy.last["op"] == "probe"
    assert proxy.last["name"] == "gh"
    assert proxy.last["harness"] == "codex"


# ── host resolution ────────────────────────────────────────────────────────────


async def test_no_host_support_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without a host store/registry there is nothing to manage → 503."""
    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    async with await _client(_make_app(with_host_support=False)) as c:
        resp = await c.get("/v1/mcp-config/servers")
    assert resp.status_code == 503


async def test_no_online_host_is_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered but offline host cannot serve the request → 409."""
    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    app = _make_app(hosts=[_FakeHostRecord(_HOST_ID, _LOCAL_OWNER, online=False)], conns={})
    async with await _client(app) as c:
        resp = await c.get("/v1/mcp-config/servers")
    assert resp.status_code == 409


async def test_several_online_hosts_require_explicit_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two live hosts → 400, so config is never written to an arbitrary machine."""
    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    app = _make_app(
        hosts=[_FakeHostRecord("host-a", _LOCAL_OWNER), _FakeHostRecord("host-b", _LOCAL_OWNER)],
        conns={"host-a": _FakeConn("host-a"), "host-b": _FakeConn("host-b")},
    )
    async with await _client(app) as c:
        resp = await c.get("/v1/mcp-config/servers")
    assert resp.status_code == 400
    assert "host_id" in resp.json()["detail"]


async def test_explicit_host_id_selects_that_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """?host_id= disambiguates when several hosts are online."""
    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    app = _make_app(
        hosts=[_FakeHostRecord("host-a", _LOCAL_OWNER), _FakeHostRecord("host-b", _LOCAL_OWNER)],
        conns={"host-a": _FakeConn("host-a"), "host-b": _FakeConn("host-b")},
    )
    async with await _client(app) as c:
        resp = await c.get("/v1/mcp-config/servers?host_id=host-b")
    assert resp.status_code == 200
    assert resp.json()["host_id"] == "host-b"


async def test_unknown_host_id_is_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown host id is a 404."""
    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    async with await _client(_make_app()) as c:
        resp = await c.get("/v1/mcp-config/servers?host_id=ghost")
    assert resp.status_code == 404


async def test_other_users_host_is_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """An admin may not manage another user's host by id → 403, not 404.

    404 would leak nothing but also hide a real permission problem; the hosts
    routes make the same distinction (unknown → 404, known-but-theirs → 403).
    """
    from omnigent.server.auth import AuthProvider

    class _AcceptAlice(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return _OWNER

    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    app = _make_app(
        hosts=[_FakeHostRecord("bobs-host", "bob@example.com")],
        conns={"bobs-host": _FakeConn("bobs-host")},
        auth_provider=_AcceptAlice(),
        permission_store=_FakePermissionStore({_OWNER}),
    )
    async with await _client(app) as c:
        resp = await c.get("/v1/mcp-config/servers?host_id=bobs-host")
    assert resp.status_code == 403


async def test_db_online_host_without_connection_is_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host online in the DB but not on this replica cannot be proxied → 409."""
    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    app = _make_app(hosts=[_FakeHostRecord(_HOST_ID, _LOCAL_OWNER)], conns={})
    async with await _client(app) as c:
        resp = await c.get("/v1/mcp-config/servers")
    assert resp.status_code == 409


# ── auth ───────────────────────────────────────────────────────────────────────


async def test_unauthenticated_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 in multi-user mode when unauthenticated."""
    from omnigent.server.auth import AuthProvider

    class _RejectAll(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return None

    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    app = _make_app(auth_provider=_RejectAll(), permission_store=_FakePermissionStore(set()))
    async with await _client(app) as c:
        resp = await c.get("/v1/mcp-config/servers")
    assert resp.status_code == 401


async def test_non_admin_blocked_on_workspace_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """403 when a non-admin tries to manage MCP on a workspace host.

    Workspace hosts require admin privileges — adding a stdio MCP server is
    arbitrary command execution on the shared host at the next session start.
    """
    from omnigent.server.auth import AuthProvider

    class _AcceptBob(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return "bob@example.com"

    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    shared_host = _FakeHostRecord(_HOST_ID, _OWNER, visibility="workspace")
    app = _make_app(
        hosts=[shared_host],
        conns={_HOST_ID: _FakeConn(_HOST_ID)},
        auth_provider=_AcceptBob(),
        permission_store=_FakePermissionStore(set()),
    )
    async with await _client(app) as c:
        resp = await c.get(f"/v1/mcp-config/servers?host_id={_HOST_ID}")
    assert resp.status_code == 403


async def test_non_admin_cannot_add_on_workspace_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The write path is gated on workspace hosts too, not just the read path."""
    from omnigent.server.auth import AuthProvider

    class _AcceptBob(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return "bob@example.com"

    proxy = _install_proxy(monkeypatch, {"name": "gh", "harness": "claude"})
    shared_host = _FakeHostRecord(_HOST_ID, _OWNER, visibility="workspace")
    app = _make_app(
        hosts=[shared_host],
        conns={_HOST_ID: _FakeConn(_HOST_ID)},
        auth_provider=_AcceptBob(),
        permission_store=_FakePermissionStore(set()),
    )
    async with await _client(app) as c:
        resp = await c.post(
            f"/v1/mcp-config/servers?host_id={_HOST_ID}",
            json={"harness": "claude", "name": "gh", "transport": "stdio", "command": "npx"},
        )
    assert resp.status_code == 403
    assert proxy.calls == [], "A rejected caller must not reach the host at all."


async def test_owner_can_manage_personal_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 when a non-admin manages MCP on their own personal host."""
    from omnigent.server.auth import AuthProvider

    class _AcceptBob(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return "bob@example.com"

    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    bobs_host = _FakeHostRecord("bobs-host", "bob@example.com", visibility="owner")
    app = _make_app(
        hosts=[bobs_host],
        conns={"bobs-host": _FakeConn("bobs-host")},
        auth_provider=_AcceptBob(),
        permission_store=_FakePermissionStore(set()),
    )
    async with await _client(app) as c:
        resp = await c.get("/v1/mcp-config/servers")
    assert resp.status_code == 200


async def test_admin_allowed_on_workspace_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 for an admin managing MCP on a workspace host."""
    from omnigent.server.auth import AuthProvider

    class _AcceptAlice(AuthProvider):
        def get_user_id(self, request: Any) -> str | None:
            return _OWNER

    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    shared_host = _FakeHostRecord(_HOST_ID, _OWNER, visibility="workspace")
    app = _make_app(
        hosts=[shared_host],
        conns={_HOST_ID: _FakeConn(_HOST_ID)},
        auth_provider=_AcceptAlice(),
        permission_store=_FakePermissionStore({_OWNER}),
    )
    async with await _client(app) as c:
        resp = await c.get(f"/v1/mcp-config/servers?host_id={_HOST_ID}")
    assert resp.status_code == 200


async def test_single_user_mode_allows_without_permission_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 in single-user mode — the admin gate is a no-op there."""
    _install_proxy(monkeypatch, _LIST_PAYLOAD)
    async with await _client(_make_app()) as c:
        resp = await c.get("/v1/mcp-config/servers")
    assert resp.status_code == 200


@pytest.mark.filterwarnings("ignore::pytest.PytestWarning")
def test_spec_payload_carries_every_field() -> None:
    """_spec_payload forwards all spec fields, so nothing is silently dropped."""
    from omnigent.server.routes.mcp_config import McpServerSpec, _spec_payload

    spec = _spec_payload(
        McpServerSpec(
            harness="claude",
            name="gh",
            transport="http",
            url="https://x/mcp",
            headers={"Authorization": "Bearer x"},
        )
    )
    assert spec["transport"] == "http"
    assert spec["url"] == "https://x/mcp"
    assert spec["headers"] == {"Authorization": "Bearer x"}
    # stdio fields present but empty rather than absent — the host reads them
    # unconditionally.
    assert spec["command"] is None
    assert spec["args"] == []
    assert json.dumps(spec)  # serialisable for the tunnel frame
