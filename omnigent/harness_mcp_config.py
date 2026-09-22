"""Read and change the harness CLIs' MCP server config, on the host.

Runs on the machine where the harness CLI actually runs — an MCP server is
only usable there, so every Settings → MCP Servers operation is proxied to a
host and lands here (see :mod:`omnigent.server.routes._host_mcp`).

Reads and writes are split deliberately:

* **Writes** drive the CLI's own ``mcp`` subcommand (``claude mcp add-json`` /
  ``claude mcp remove``), so the on-disk format stays owned by that CLI and
  this module never has to keep up with it.
* **Reads** parse the CLI's config file, because the CLIs emit human text
  rather than JSON for ``mcp list``. Scraping that text would break on any
  cosmetic change; the config path is the stable contract, and it is the same
  file :mod:`omnigent.install_ledger` already tracks.

Security contract: NO secret VALUES ever leave this module. ``env`` and
``headers`` are reduced to key NAMES, URL userinfo and query strings are
stripped, and credential-shaped argv values are redacted. Secrets travel
inbound only, in an ``add`` spec.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_logger = logging.getLogger(__name__)

# Seconds any single CLI invocation may take before it is treated as a hung
# harness. Generous enough for a cold node start, bounded so a wedged CLI
# cannot pin the host's frame task.
_CLI_TIMEOUT_S = 30.0

# Harnesses this module knows how to drive, mapped to their config file and
# the key holding the server table.
_HARNESSES: dict[str, dict[str, str]] = {
    "claude": {"path": "~/.claude.json", "key": "mcpServers", "format": "json"},
    "codex": {"path": "~/.codex/config.toml", "key": "mcp_servers", "format": "toml"},
}

# argv values shaped like credentials. A stdio server's args are visible in the
# listing (they are how you tell two `npx` servers apart), but a token pasted
# into them must not be.
_SECRET_ARG_RE = re.compile(
    r"(?i)(token|secret|key|password|passwd|pwd|credential|auth)[=:]"
    r"|^(sk|ghp|gho|ghu|ghs|ghr|github_pat|xox[abposr])[-_]"
)
_REDACTED = "<redacted>"


class HarnessMcpError(Exception):
    """An MCP config operation failed in a way the user should see.

    :param status: HTTP status the failure deserves, e.g. ``404``.
    :param code: Machine-readable code, e.g. ``"not_found"``.
    :param message: Human-readable, non-secret detail.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _harness_meta(harness: str) -> dict[str, str]:
    """Return the config metadata for *harness*, or reject it.

    :param harness: Harness CLI id, e.g. ``"claude"``.
    :returns: Its entry from :data:`_HARNESSES`.
    :raises HarnessMcpError: 400 when the harness is not supported.
    """
    meta = _HARNESSES.get(harness)
    if meta is None:
        raise HarnessMcpError(
            400,
            "unsupported_harness",
            f"unsupported harness {harness!r}; expected one of: {', '.join(sorted(_HARNESSES))}",
        )
    return meta


def _config_path(harness: str) -> Path:
    """Absolute path to *harness*'s config file."""
    return Path(os.path.expanduser(_harness_meta(harness)["path"]))


def _scope_args(harness: str, subcommand: str) -> list[str]:
    """Build the CLI argv prefix, pinning the scope this surface manages.

    ``claude mcp`` defaults to ``local`` scope, which writes a table keyed by
    the current working directory — config that applies to one project only,
    and that this module's reader (the global ``mcpServers`` table) would
    never see. Settings → MCP Servers is a host-wide operator surface, so it
    pins ``user`` scope on both writes: adding to a scope the listing cannot
    read would silently produce servers nobody can see or remove.

    :param harness: Harness CLI id.
    :param subcommand: The ``mcp`` subcommand, e.g. ``"add-json"``.
    :returns: The argv prefix for :func:`_run_cli`.
    """
    if harness == "claude":
        return [subcommand, "--scope", "user"]
    return [subcommand]


def _sanitize_url(url: str) -> str:
    """Strip credentials and query string from a URL.

    Userinfo (``https://user:token@host``) and the query string are both
    routine places to hide a token, and neither is needed to identify a
    server.

    :param url: The raw URL from config.
    :returns: Scheme, host, and path only.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return _REDACTED
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def _sanitize_args(args: Any) -> list[str]:
    """Redact credential-shaped argv values, keeping the rest readable."""
    if not isinstance(args, list):
        return []
    out: list[str] = []
    for arg in args:
        text = str(arg)
        out.append(_REDACTED if _SECRET_ARG_RE.search(text) else text)
    return out


def _sanitize_entry(name: str, entry: Any, harness: str) -> dict[str, Any]:
    """Reduce one raw config entry to a safe, listable shape.

    :param name: The server's name.
    :param entry: The raw config value (dict, or anything else).
    :param harness: Which harness it came from.
    :returns: A dict with no secret values — env/header KEY NAMES only.
    """
    if not isinstance(entry, dict):
        return {"name": name, "harness": harness, "transport": "unknown", "status": "unknown"}

    command = entry.get("command")
    url = entry.get("url")
    transport = entry.get("type") or ("stdio" if command else "http" if url else "unknown")
    safe: dict[str, Any] = {
        "name": name,
        "harness": harness,
        "transport": str(transport),
    }
    if command:
        safe["command"] = str(command)
        safe["args"] = _sanitize_args(entry.get("args"))
        # Resolvability is a weak signal — it says the command exists, not
        # that the server works. The probe is the real answer; this just
        # flags the obvious "not installed" case cheaply.
        safe["status"] = "resolvable" if shutil.which(str(command)) is not None else "unresolvable"
    elif url:
        # Nothing to resolve for an http-family server: there is no local
        # command, and reporting "resolvable" would imply a reachability
        # check this listing does not perform.
        safe["status"] = "configured"
    else:
        safe["status"] = "unknown"
    if url:
        safe["url"] = _sanitize_url(str(url))
    env = entry.get("env")
    safe["env_keys"] = sorted(str(k) for k in env) if isinstance(env, dict) else []
    headers = entry.get("headers")
    safe["header_keys"] = sorted(str(k) for k in headers) if isinstance(headers, dict) else []
    return safe


def _read_raw(harness: str) -> dict[str, Any]:
    """Read *harness*'s raw MCP server table from its config file.

    :param harness: Harness CLI id.
    :returns: ``name -> raw entry``; empty when the file or key is absent
        (an unconfigured harness is not an error).
    :raises HarnessMcpError: 500 when the file exists but cannot be parsed.
    """
    meta = _harness_meta(harness)
    path = _config_path(harness)
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
        if meta["format"] == "toml":
            import tomllib

            data: Any = tomllib.loads(text)
        else:
            data = json.loads(text)
    except Exception as exc:
        _logger.debug("Unreadable MCP config for %r", harness, exc_info=True)
        raise HarnessMcpError(
            500,
            "unreadable_config",
            f"could not parse the {harness} config file",
        ) from exc
    servers = data.get(meta["key"]) if isinstance(data, dict) else None
    return servers if isinstance(servers, dict) else {}


def _build_entry(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Validate a server spec and shape it for the CLI's ``add-json``.

    :param name: Server name; must be non-blank.
    :param spec: Transport, command/url, args, env, headers.
    :returns: The entry dict to hand the CLI.
    :raises HarnessMcpError: 400 when the spec is incomplete for its
        transport — checked here, before any destructive step runs.
    """
    if not name or not str(name).strip():
        raise HarnessMcpError(400, "invalid_spec", "a server name is required")

    transport = str(spec.get("transport") or "stdio")
    entry: dict[str, Any] = {"type": transport}
    if transport == "stdio":
        command = spec.get("command")
        if not command:
            raise HarnessMcpError(400, "invalid_spec", "a stdio server requires a command")
        entry["command"] = str(command)
        entry["args"] = [str(a) for a in (spec.get("args") or [])]
        env = spec.get("env")
        if isinstance(env, dict) and env:
            entry["env"] = {str(k): str(v) for k, v in env.items()}
    else:
        url = spec.get("url")
        if not url:
            raise HarnessMcpError(400, "invalid_spec", f"a {transport} server requires a url")
        entry["url"] = str(url)
        headers = spec.get("headers")
        if isinstance(headers, dict) and headers:
            entry["headers"] = {str(k): str(v) for k, v in headers.items()}
    return entry


async def _run_cli(harness: str, args: list[str]) -> str:
    """Run ``<harness> mcp <args...>`` and return stdout.

    :param harness: Harness CLI id, which is also the executable name.
    :param args: Arguments after the ``mcp`` subcommand.
    :returns: Captured stdout.
    :raises HarnessMcpError: 503 when the CLI is missing or times out, 400
        when the CLI itself rejects the operation.
    """
    if shutil.which(harness) is None:
        raise HarnessMcpError(
            503,
            "harness_unavailable",
            f"the {harness!r} CLI is not installed on this host",
        )
    try:
        proc = await asyncio.create_subprocess_exec(
            harness,
            "mcp",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_CLI_TIMEOUT_S)
    except (TimeoutError, asyncio.TimeoutError) as exc:
        raise HarnessMcpError(
            503,
            "harness_unavailable",
            f"the {harness} CLI did not finish within {_CLI_TIMEOUT_S:g}s",
        ) from exc
    except OSError as exc:
        raise HarnessMcpError(
            503, "harness_unavailable", f"could not run the {harness} CLI"
        ) from exc

    if proc.returncode != 0:
        # The CLI's own message is the useful one. It echoes the name and its
        # own reason, never the spec we passed in, so it is safe to surface.
        detail = (stderr or b"").decode("utf-8", "replace").strip()
        raise HarnessMcpError(
            400,
            "harness_rejected",
            detail or f"the {harness} CLI rejected the operation",
        )
    return (stdout or b"").decode("utf-8", "replace")


def list_servers(harness: str | None = None) -> dict[str, Any]:
    """List configured MCP servers, with no secret values.

    :param harness: Restrict to one harness; ``None`` covers all known ones.
    :returns: ``{"servers": [sanitized entry, ...]}``.
    :raises HarnessMcpError: When a named harness is unsupported.
    """
    harnesses = [harness] if harness else sorted(_HARNESSES)
    servers: list[dict[str, Any]] = []
    for h in harnesses:
        for name, entry in sorted(_read_raw(h).items()):
            servers.append(_sanitize_entry(str(name), entry, h))
    return {"servers": servers}


async def add_server(harness: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Register an MCP server with a harness CLI.

    The spec is handed to the CLI as JSON so the CLI owns the on-disk
    format. Secret values in ``env`` / ``headers`` go inbound here and are
    never read back.

    :param harness: Harness CLI id.
    :param name: Server name, unique per harness.
    :param spec: Transport, command/url, args, env, headers.
    :returns: ``{"added": name, "harness": harness}``.
    :raises HarnessMcpError: On an invalid spec or a CLI refusal.
    """
    _harness_meta(harness)
    entry = _build_entry(name, spec)
    await _run_cli(harness, [*_scope_args(harness, "add-json"), str(name), json.dumps(entry)])
    return {"added": str(name), "harness": harness}


async def update_server(harness: str, name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Replace an existing MCP server's definition.

    The harness CLIs have no in-place edit — ``add`` refuses a name that
    already exists — so this removes and re-adds. The remove runs first and
    only after the new spec has been validated by :func:`add_server`'s own
    checks, so a rejected spec cannot leave the server deleted: validation
    happens on the way in, before anything is torn down.

    :param harness: Harness CLI id.
    :param name: Server name to replace.
    :param spec: The new definition, same shape as :func:`add_server`.
    :returns: ``{"updated": name, "harness": harness}``.
    :raises HarnessMcpError: 404 when absent, 400 on an invalid spec.
    """
    _harness_meta(harness)
    if str(name) not in _read_raw(harness):
        raise HarnessMcpError(
            404, "not_found", f"MCP server {name!r} is not configured for {harness}"
        )
    # Validate before destroying anything: _build_entry raises on a bad spec.
    entry = _build_entry(name, spec)
    await _run_cli(harness, [*_scope_args(harness, "remove"), str(name)])
    await _run_cli(harness, [*_scope_args(harness, "add-json"), str(name), json.dumps(entry)])
    return {"updated": str(name), "harness": harness}


async def remove_server(harness: str, name: str) -> dict[str, Any]:
    """Remove an MCP server from a harness CLI.

    :param harness: Harness CLI id.
    :param name: Server name to remove.
    :returns: ``{"removed": name, "harness": harness}``.
    :raises HarnessMcpError: 404 when the server is not configured.
    """
    _harness_meta(harness)
    if str(name) not in _read_raw(harness):
        raise HarnessMcpError(
            404, "not_found", f"MCP server {name!r} is not configured for {harness}"
        )
    await _run_cli(harness, [*_scope_args(harness, "remove"), str(name)])
    return {"removed": str(name), "harness": harness}


async def probe_server(harness: str, name: str) -> dict[str, Any]:
    """Launch one configured MCP server and prove it initializes.

    Spawns it the way a runner would, performs the MCP ``initialize``
    handshake, and lists tools — the definitive usability answer, unlike
    the ``command_on_path`` hint in a listing.

    :param harness: Harness CLI id.
    :param name: Server name to probe.
    :returns: ``{"name", "usable", "tool_count", "detail"}``.
    :raises HarnessMcpError: 404 when the server is not configured.
    """
    from omnigent.server.routes.mcp_probe import probe_mcp_server

    _harness_meta(harness)
    entry = _read_raw(harness).get(str(name))
    if entry is None:
        raise HarnessMcpError(
            404, "not_found", f"MCP server {name!r} is not configured for {harness}"
        )
    # probe_mcp_server never raises and never returns a secret.
    return await probe_mcp_server(str(name), entry if isinstance(entry, dict) else {})
