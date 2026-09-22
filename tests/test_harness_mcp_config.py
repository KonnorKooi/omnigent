"""Sanitisation and CLI-argv behaviour of the host-side MCP config driver.

The security contract is the point of most of these: a listing exists so an
operator can see which servers are configured, and it must stay useful without
ever handing back the tokens those servers authenticate with.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from omnigent import harness_mcp_config as hm

_FAKE_SERVER = str(Path(__file__).parent / "server" / "routes" / "_fake_mcp_server.py")
_SECRET = "super-secret-token-should-never-appear"


@pytest.fixture()
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point config reads at a scratch HOME, never the developer's own."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _write_claude_config(home: Path, servers: dict[str, Any]) -> None:
    (home / ".claude.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# ── sanitisation ─────────────────────────────────────────────


def test_env_values_never_appear_only_key_names(fake_home: Path) -> None:
    """A listing names the env keys a server needs, never their values."""
    _write_claude_config(fake_home, {"gh": {"command": "npx", "env": {"GH_TOKEN": _SECRET}}})

    listed = hm.list_servers("claude")

    assert _SECRET not in json.dumps(listed)
    assert listed["servers"][0]["env_keys"] == ["GH_TOKEN"]


def test_header_values_never_appear_only_key_names(fake_home: Path) -> None:
    """Same contract for http-family servers' auth headers."""
    _write_claude_config(
        fake_home,
        {"api": {"url": "https://example.com/mcp", "headers": {"Authorization": _SECRET}}},
    )

    listed = hm.list_servers("claude")

    assert _SECRET not in json.dumps(listed)
    assert listed["servers"][0]["header_keys"] == ["Authorization"]


def test_url_userinfo_and_query_are_stripped(fake_home: Path) -> None:
    """Both routine hiding places for a token are removed from a URL."""
    _write_claude_config(
        fake_home, {"api": {"url": f"https://user:{_SECRET}@example.com/mcp?key={_SECRET}"}}
    )

    listed = hm.list_servers("claude")

    assert _SECRET not in json.dumps(listed)
    assert listed["servers"][0]["url"] == "https://example.com/mcp"


@pytest.mark.parametrize(
    "arg",
    ["--token=abc123", "--api-key=xyz", "sk-livekey", "ghp_abcdef", "PASSWORD:hunter2"],
)
def test_credential_shaped_args_are_redacted(fake_home: Path, arg: str) -> None:
    """A token pasted into argv is redacted, though plain args stay readable."""
    _write_claude_config(fake_home, {"s": {"command": "npx", "args": ["-y", "pkg", arg]}})

    listed = hm.list_servers("claude")
    args = listed["servers"][0]["args"]

    assert args[:2] == ["-y", "pkg"]
    assert args[2] == "<redacted>"


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"command": "sh"}, "resolvable"),
        ({"command": "definitely-not-installed-xyz"}, "unresolvable"),
        ({"url": "https://example.com/mcp", "type": "http"}, "configured"),
        ({}, "unknown"),
    ],
)
def test_status_reports_what_was_actually_checked(
    fake_home: Path, entry: dict[str, Any], expected: str
) -> None:
    """An http server reads ``configured``, not ``resolvable``: no local
    command was resolved and no reachability check was performed, so claiming
    resolvability would overstate what the listing knows."""
    _write_claude_config(fake_home, {"s": entry})

    assert hm.list_servers("claude")["servers"][0]["status"] == expected


def test_ordinary_args_survive_so_servers_stay_distinguishable(fake_home: Path) -> None:
    """Redaction is targeted: it must not erase what identifies a server."""
    _write_claude_config(
        fake_home, {"s": {"command": "npx", "args": ["-y", "@acme/mcp-server", "--port", "8080"]}}
    )

    assert hm.list_servers("claude")["servers"][0]["args"] == [
        "-y",
        "@acme/mcp-server",
        "--port",
        "8080",
    ]


# ── reading config ───────────────────────────────────────────


def test_missing_config_is_not_an_error(fake_home: Path) -> None:
    """An unconfigured harness lists nothing rather than failing."""
    assert hm.list_servers("claude") == {"servers": []}


def test_unparseable_config_is_reported(fake_home: Path) -> None:
    """A corrupt file is a real failure, distinct from an absent one."""
    (fake_home / ".claude.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(hm.HarnessMcpError) as excinfo:
        hm.list_servers("claude")

    assert excinfo.value.status == 500
    assert excinfo.value.code == "unreadable_config"


def test_unsupported_harness_is_rejected(fake_home: Path) -> None:
    """An unknown harness is a 400 naming what is supported."""
    with pytest.raises(hm.HarnessMcpError) as excinfo:
        hm.list_servers("nosuch")

    assert excinfo.value.status == 400
    assert excinfo.value.code == "unsupported_harness"


def test_listing_covers_every_harness_when_none_named(fake_home: Path) -> None:
    """Omitting the harness reports all of them, each row labelled."""
    _write_claude_config(fake_home, {"a": {"command": "npx"}})

    listed = hm.list_servers()

    assert [s["harness"] for s in listed["servers"]] == ["claude"]


# ── writes ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_pins_user_scope_so_the_listing_can_see_it(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``claude mcp`` defaults to project-local scope, which the reader
    cannot see — an add must pin the user scope or create invisible config."""
    calls: list[list[str]] = []

    async def _fake_run(harness: str, args: list[str]) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(hm, "_run_cli", _fake_run)
    await hm.add_server("claude", "gh", {"transport": "stdio", "command": "npx"})

    assert calls[0][:3] == ["add-json", "--scope", "user"]


@pytest.mark.asyncio
async def test_add_sends_secrets_inbound_in_the_spec(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Secrets do travel inbound — the contract is one-way, not no-secrets."""
    calls: list[list[str]] = []

    async def _fake_run(harness: str, args: list[str]) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(hm, "_run_cli", _fake_run)
    await hm.add_server(
        "claude", "gh", {"transport": "stdio", "command": "npx", "env": {"T": _SECRET}}
    )

    assert _SECRET in calls[0][-1]


@pytest.mark.asyncio
async def test_stdio_add_requires_a_command(fake_home: Path) -> None:
    """An incomplete spec fails before any CLI runs."""
    with pytest.raises(hm.HarnessMcpError) as excinfo:
        await hm.add_server("claude", "gh", {"transport": "stdio"})

    assert excinfo.value.status == 400
    assert excinfo.value.code == "invalid_spec"


@pytest.mark.asyncio
async def test_http_add_requires_a_url(fake_home: Path) -> None:
    """The http-family equivalent of the same check."""
    with pytest.raises(hm.HarnessMcpError) as excinfo:
        await hm.add_server("claude", "api", {"transport": "http"})

    assert excinfo.value.status == 400


@pytest.mark.asyncio
async def test_remove_unknown_server_is_404(fake_home: Path) -> None:
    """Removing what is not configured is a 404, not a silent success."""
    with pytest.raises(hm.HarnessMcpError) as excinfo:
        await hm.remove_server("claude", "nope")

    assert excinfo.value.status == 404
    assert excinfo.value.code == "not_found"


@pytest.mark.asyncio
async def test_update_replaces_the_definition(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An update swaps the spec, since the CLIs have no in-place edit."""
    _write_claude_config(fake_home, {"s1": {"command": "npx", "args": ["v1"]}})
    calls: list[list[str]] = []

    async def _fake_run(harness: str, args: list[str]) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(hm, "_run_cli", _fake_run)
    await hm.update_server("claude", "s1", {"transport": "stdio", "command": "npx"})

    assert [c[0] for c in calls] == ["remove", "add-json"]


@pytest.mark.asyncio
async def test_update_rejects_a_bad_spec_before_removing_anything(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Validation precedes the destructive step, so a rejected update cannot
    leave the server deleted."""
    _write_claude_config(fake_home, {"s1": {"command": "npx"}})
    calls: list[list[str]] = []

    async def _fake_run(harness: str, args: list[str]) -> str:
        calls.append(args)
        return ""

    monkeypatch.setattr(hm, "_run_cli", _fake_run)

    with pytest.raises(hm.HarnessMcpError) as excinfo:
        await hm.update_server("claude", "s1", {"transport": "stdio"})

    assert excinfo.value.code == "invalid_spec"
    assert calls == [], "nothing may run before the spec validates"


@pytest.mark.asyncio
async def test_update_unknown_server_is_404(fake_home: Path) -> None:
    """Updating what is not configured is a 404."""
    with pytest.raises(hm.HarnessMcpError) as excinfo:
        await hm.update_server("claude", "nope", {"transport": "stdio", "command": "npx"})

    assert excinfo.value.status == 404


# ── probe ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_proves_initialization_not_just_path_lookup(fake_home: Path) -> None:
    """The probe launches the server and lists its tools for real."""
    _write_claude_config(fake_home, {"fake": {"command": sys.executable, "args": [_FAKE_SERVER]}})

    result = await hm.probe_server("claude", "fake")

    assert result["usable"] is True
    assert result["tool_count"] >= 1


@pytest.mark.asyncio
async def test_probe_of_a_resolvable_but_broken_server_is_unusable(fake_home: Path) -> None:
    """PATH resolvability is not proof: a command that exists but is not an
    MCP server probes as unusable."""
    _write_claude_config(
        fake_home, {"broken": {"command": sys.executable, "args": ["-c", "pass"]}}
    )

    result = await hm.probe_server("claude", "broken")

    assert result["usable"] is False


@pytest.mark.asyncio
async def test_probe_unknown_server_is_404(fake_home: Path) -> None:
    """Probing what is not configured is a 404."""
    with pytest.raises(hm.HarnessMcpError) as excinfo:
        await hm.probe_server("claude", "nope")

    assert excinfo.value.status == 404
