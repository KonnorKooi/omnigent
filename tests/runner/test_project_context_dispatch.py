"""Runner dispatch and native-relay coverage for the project context tools.

The five tools are schema-only; the runner proxies them to the session-scoped
``/v1/sessions/{id}/context`` routes. These tests pin the relay surface, the
request each tool issues, the rendered output, and the non-error notice when
the session has no project context.
"""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.native.orchestration import (
    _fetch_project_context_injection,
    _project_context_allowed_claude_tools,
)
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, execute_tool
from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins.project_context import PROJECT_CONTEXT_TOOL_NAMES

_NO_CONTEXT = {"error": {"code": "not_found", "message": "Session has no project context"}}


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_relay_exposes_context_tools(spec: AgentSpec | None) -> None:
    """Every native harness relay advertises all five tools."""
    names = {schema["name"] for schema in build_native_relay_tool_schemas(spec)}
    assert set(PROJECT_CONTEXT_TOOL_NAMES) <= names


async def _run(
    tool: str, args: dict[str, object], handler: httpx.MockTransport
) -> tuple[str, list[httpx.Request]]:
    """Execute ``tool`` against a mock server, returning output and requests."""
    seen: list[httpx.Request] = []
    inner = handler.handle_request

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return inner(request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(record), base_url="http://server"
    ) as client:
        output = await execute_tool(
            tool_name=tool,
            arguments=json.dumps(args),
            server_client=client,
            conversation_id="conv_1",
            agent_spec=AgentSpec(spec_version=1),
        )
    return output, seen


@pytest.mark.asyncio
async def test_context_list_renders_index() -> None:
    """context_list calls /list and renders descriptions and badges."""
    body = {
        "files": [
            {"path": "system/rules.md", "description": "Rules", "always_loaded": True},
            {"path": "wiki/exp.md", "description": None, "always_loaded": False},
        ],
        "has_code_graph": True,
    }
    output, seen = await _run(
        "context_list", {}, httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    )
    assert seen[0].url.path == "/v1/sessions/conv_1/context/list"
    assert "- system/rules.md [always loaded] — Rules" in output
    assert "- wiki/exp.md" in output
    assert "graph_query" in output


@pytest.mark.asyncio
async def test_context_read_passes_paging_and_renders_continuation() -> None:
    """context_read forwards path/offset/limit and tells the model how to continue."""
    body = {
        "path": "wiki/a.md",
        "content": "b\n",
        "offset": 1,
        "lines": 1,
        "total_lines": 3,
        "truncated": True,
    }
    output, seen = await _run(
        "context_read",
        {"path": "wiki/a.md", "offset": 1, "limit": 1},
        httpx.MockTransport(lambda r: httpx.Response(200, json=body)),
    )
    params = dict(seen[0].url.params)
    assert seen[0].url.path == "/v1/sessions/conv_1/context/files"
    assert params == {"path": "wiki/a.md", "offset": "1", "limit": "1"}
    assert output.startswith("wiki/a.md (lines 2-2 of 3)")
    assert "offset=2" in output


@pytest.mark.asyncio
async def test_search_query_and_neighbors_requests() -> None:
    """search, graph_query and graph_neighbors hit their routes with clamped args."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            return httpx.Response(
                200,
                json={
                    "query": "psnr",
                    "results": [{"path": "wiki/a.md", "line": 4, "snippet": "psnr is 30"}],
                },
            )
        if path.endswith("/graph/query"):
            return httpx.Response(200, json={"text": "Matching nodes:\n- compute_psnr"})
        return httpx.Response(
            200,
            json={
                "available": True,
                "node": {"id": "n1", "label": "Encoder", "source_file": "m.py"},
                "neighbors": [
                    {
                        "id": "n2",
                        "label": "train",
                        "relation": "uses",
                        "direction": "in",
                        "depth": 1,
                        "source_file": "t.py",
                        "source_location": "L5",
                    }
                ],
            },
        )

    transport = httpx.MockTransport(handler)
    output, seen = await _run("context_search", {"query": "psnr", "limit": 999}, transport)
    assert output == "wiki/a.md:4: psnr is 30"
    assert dict(seen[0].url.params) == {"q": "psnr", "limit": "50"}

    output, seen = await _run("graph_query", {"question": "psnr", "budget": 5}, transport)
    assert "compute_psnr" in output
    assert dict(seen[0].url.params) == {"q": "psnr", "budget": "100"}

    output, seen = await _run("graph_neighbors", {"node": "Encoder", "depth": 9}, transport)
    assert dict(seen[0].url.params) == {"node": "Encoder", "depth": "2"}
    assert "<-- uses train (n2) [t.py:L5]" in output


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", PROJECT_CONTEXT_TOOL_NAMES)
async def test_no_project_context_is_a_notice(tool: str) -> None:
    """A 404 'no project context' becomes a plain, non-error message."""
    args = {"context_read": {"path": "wiki/a.md"}, "context_search": {"query": "x"}}.get(
        tool, {"question": "x", "node": "x"}
    )
    if tool == "context_list":
        args = {}
    elif tool == "graph_query":
        args = {"question": "x"}
    elif tool == "graph_neighbors":
        args = {"node": "x"}
    output, _ = await _run(
        tool, args, httpx.MockTransport(lambda r: httpx.Response(404, json=_NO_CONTEXT))
    )
    assert output.startswith("No project context is available")


@pytest.mark.asyncio
async def test_context_read_missing_file_and_bad_args() -> None:
    """A missing file surfaces the server message; missing args are rejected locally."""
    missing = {"error": {"code": "not_found", "message": "wiki/nope.md not found"}}
    output, _ = await _run(
        "context_read",
        {"path": "wiki/nope.md"},
        httpx.MockTransport(lambda r: httpx.Response(404, json=missing)),
    )
    assert output == "context_read: wiki/nope.md not found"

    output, seen = await _run(
        "context_search", {}, httpx.MockTransport(lambda r: httpx.Response(500))
    )
    assert "requires a string 'query'" in output and seen == []


@pytest.mark.asyncio
async def test_long_output_is_truncated_with_notice() -> None:
    """Huge responses are capped with an explicit truncation notice."""
    output, _ = await _run(
        "graph_query",
        {"question": "x"},
        httpx.MockTransport(lambda r: httpx.Response(200, json={"text": "y" * 100_000})),
    )
    assert len(output) < 41_000
    assert output.endswith("narrow the request (offset/limit, query, depth)]")


# ── startup injection ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fetch_injection_returns_text() -> None:
    """A 200 from /context/injected yields the text for the startup channel."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "# Project context\nrules"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        text = await _fetch_project_context_injection(client, "conv_9")
    assert text == "# Project context\nrules"
    assert seen[0].url.path == "/v1/sessions/conv_9/context/injected"
    assert "harness" not in seen[0].url.params


@pytest.mark.asyncio
async def test_fetch_injection_names_the_harness() -> None:
    """The harness rides as a query param so the server can skip what it loads natively."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "ctx"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        await _fetch_project_context_injection(client, "conv_9", harness="claude-native")
    assert seen[0].url.params["harness"] == "claude-native"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [httpx.Response(404, json=_NO_CONTEXT), httpx.Response(200, json={"text": "  "})],
)
async def test_fetch_injection_none_when_absent(response: httpx.Response) -> None:
    """No project context (404) or empty text means nothing is injected."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: response), base_url="http://server"
    ) as client:
        assert await _fetch_project_context_injection(client, "conv_9") is None


@pytest.mark.asyncio
async def test_fetch_injection_swallows_transport_errors() -> None:
    """A dead server never blocks terminal launch."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        assert await _fetch_project_context_injection(client, "conv_9") is None
    assert await _fetch_project_context_injection(None, "conv_9") is None


def test_claude_allowed_tools_are_relay_prefixed() -> None:
    """Claude pre-approves the MCP-relay names of all five tools."""
    assert _project_context_allowed_claude_tools() == tuple(
        f"mcp__omnigent__{name}" for name in PROJECT_CONTEXT_TOOL_NAMES
    )
