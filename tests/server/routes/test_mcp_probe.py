"""Tests for the MCP usability probe (``probe_mcp_server``) and its route.

The probe answers "is this server actually usable from Omnigent", not merely
"is the command on PATH". These tests exercise it end-to-end against a real
(tiny) MCP stdio server so the ``initialize`` + ``list_tools`` handshake truly
runs, plus the failure and not-found paths.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.requests import Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.server.routes.mcp_config import create_mcp_config_router
from omnigent.server.routes.mcp_probe import probe_mcp_server

pytestmark = pytest.mark.asyncio

# The fake MCP server module, launched via `python <path>`.
_FAKE_SERVER = str(Path(__file__).parent / "_fake_mcp_server.py")


# ── probe_mcp_server: the core usability check ──────────────────────────────────


async def test_probe_real_stdio_server_is_usable() -> None:
    """A working stdio server probes as usable with its tools counted."""
    entry = {"command": sys.executable, "args": [_FAKE_SERVER]}
    result = await probe_mcp_server("fake", entry)
    assert result["usable"] is True
    assert result["tool_count"] >= 1  # the `ping` tool
    assert result["name"] == "fake"


async def test_probe_missing_command_is_unusable() -> None:
    """A command that does not exist probes as unusable, without raising."""
    entry = {"command": "this-command-does-not-exist-xyz", "args": []}
    result = await probe_mcp_server("bad", entry)
    assert result["usable"] is False
    assert result["tool_count"] == 0
    assert result["detail"]  # a non-empty reason string


async def test_probe_crashing_server_is_unusable() -> None:
    """A command that exits immediately (no MCP handshake) is unusable."""
    # `python -c "raise SystemExit(1)"` spawns fine but never speaks MCP.
    entry = {"command": sys.executable, "args": ["-c", "raise SystemExit(1)"]}
    result = await probe_mcp_server("crash", entry)
    assert result["usable"] is False


async def test_probe_unsupported_transport_is_unusable() -> None:
    """An entry with neither command nor url is reported unusable, not crashed."""
    result = await probe_mcp_server("weird", {"type": "carrier-pigeon"})
    assert result["usable"] is False
    assert result["detail"] == "unsupported transport"


async def test_probe_http_type_without_url_is_unusable() -> None:
    """An http-typed entry missing its url reports a clear reason, not a crash."""
    result = await probe_mcp_server("no-url", {"type": "http"})
    assert result["usable"] is False
    assert result["detail"] == "no url configured"


async def test_probe_never_leaks_env_secret() -> None:
    """The probe result never echoes the server's env values."""
    secret = "super-secret-should-not-appear-in-probe-result"
    entry = {
        "command": sys.executable,
        "args": [_FAKE_SERVER],
        "env": {"MY_TOKEN": secret},
    }
    result = await probe_mcp_server("fake", entry)
    assert secret not in repr(result)


# ── The probe route ─────────────────────────────────────────────────────────────


def _make_app_with_error_handler(router_kwargs: dict[str, Any] | None = None) -> FastAPI:
    """Minimal app with the OmnigentError → HTTP handler wired up."""
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(create_mcp_config_router(**(router_kwargs or {})), prefix="/v1")
    return app


def _write_claude_json(tmp_path: Path) -> None:
    """Write a .claude.json with the fake stdio server."""
    import json

    (tmp_path / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {"command": sys.executable, "args": [_FAKE_SERVER]},
                }
            }
        )
    )


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Monkeypatch Path.home() to a tmp dir containing a .claude.json."""
    _write_claude_json(tmp_path)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


@pytest_asyncio.fixture()
async def probe_client(fake_home: Path) -> AsyncIterator[httpx.AsyncClient]:
    """Async client wired to the mcp-config router (single-user, no auth)."""
    app = _make_app_with_error_handler({})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── Route-level probe behaviour ────────────────────────────────────────────────
#
# Not tested here. The probe route proxies op="probe" to a HOST (an MCP server
# is only usable on the machine where the harness CLI runs), so its contract —
# admin gating, host resolution, and the 404/403/502 mapping — is covered by
# tests/server/routes/test_mcp_config.py against that proxy. The tests that
# used to live here drove the superseded server-local probe, which read the
# API server's own ~/.claude.json: config no session could ever see.
