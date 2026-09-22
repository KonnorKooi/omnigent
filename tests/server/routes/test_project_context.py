"""Tests for the project context routes (``/v1/projects/{id}/context``).

Exercises the REST surface over a real SQLite project/conversation store and
a temporary context directory, in both auth setups:

- **Single-user** (no auth provider) — the omnidev default.
- **Multi-user** (header auth) — proves context is owner-private: another
  user can neither reach a project's context directly nor through a session
  that was shared with them (PRD §9.4).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.context.curator import CuratorRequest, ProposalDraft
from omnigent.context.jobs import ContextJobManager
from omnigent.entities import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.routes.project_context import create_project_context_router
from omnigent.server.routes.projects import create_projects_router
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import (
    SqlAlchemyPermissionStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.context._helpers import write_graph

ALICE = "alice@example.com"
BOB = "bob@example.com"
AGENT_ID = "087b7cb7ac30abf4debfaa578d052ec6"


def _ensure_agent(db_uri: str) -> None:
    agent_store = SqlAlchemyAgentStore(db_uri)
    if agent_store.get(AGENT_ID) is None:
        agent_store.create(agent_id=AGENT_ID, name="test-agent", bundle_location=f"{AGENT_ID}/b")


def _app(
    db_uri: str, *, multi_user: bool = False, job_manager: ContextJobManager | None = None
) -> FastAPI:
    """Mount projects + project-context routers on a bare app."""
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    auth = UnifiedAuthProvider(source="header") if multi_user else None
    permission_store = SqlAlchemyPermissionStore(db_uri) if multi_user else None
    project_store = SqlAlchemyProjectStore(db_uri)
    app.include_router(
        create_projects_router(project_store=project_store, auth_provider=auth), prefix="/v1"
    )
    app.include_router(
        create_project_context_router(
            project_store=project_store,
            conversation_store=SqlAlchemyConversationStore(db_uri),
            auth_provider=auth,
            permission_store=permission_store,
            job_manager=job_manager,
        ),
        prefix="/v1",
    )
    return app


def _hdr(user: str) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


@pytest.fixture()
def client(db_uri: str) -> TestClient:
    """Single-user client."""
    return TestClient(_app(db_uri))


def _configured_project(client: TestClient, ctx: Path, **headers: str) -> str:
    """Create a project whose config points at ``ctx`` and initialise it."""
    hdrs = dict(headers)
    project = client.post(
        "/v1/projects",
        json={"name": f"P-{ctx.name}", "config": {"context": {"path": str(ctx)}}},
        headers=hdrs,
    ).json()
    resp = client.post(f"/v1/projects/{project['id']}/context/init", headers=hdrs)
    assert resp.status_code == 200, resp.text
    return project["id"]


# ── config validation ────────────────────────────────────────────────────


def test_invalid_context_config_rejected_on_save(client: TestClient) -> None:
    """A relative context path is a 400 on create and on update."""
    resp = client.post(
        "/v1/projects", json={"name": "bad", "config": {"context": {"path": "rel/dir"}}}
    )
    assert resp.status_code == 400
    project = client.post("/v1/projects", json={"name": "ok"}).json()
    resp = client.patch(
        f"/v1/projects/{project['id']}", json={"config": {"context": {"path": "x\x00"}}}
    )
    assert resp.status_code == 400


def test_status_unconfigured(client: TestClient) -> None:
    """A project with no context config reports configured=false."""
    project = client.post("/v1/projects", json={"name": "plain"}).json()
    body = client.get(f"/v1/projects/{project['id']}/context").json()
    assert body["configured"] is False
    # Other context routes 404 until configured.
    assert client.get(f"/v1/projects/{project['id']}/context/preview").status_code == 404


def test_unknown_project_404(client: TestClient) -> None:
    """A missing project id is a 404."""
    assert client.get("/v1/projects/" + "f" * 32 + "/context").status_code == 404


# ── files ────────────────────────────────────────────────────────────────


def test_file_crud_flow(client: TestClient, tmp_path: Path) -> None:
    """Init → create → read → save (sha) → conflict → rename → delete."""
    pid = _configured_project(client, tmp_path / "ctx")
    status = client.get(f"/v1/projects/{pid}/context").json()
    assert status["configured"] and status["initialized"]

    created = client.post(
        f"/v1/projects/{pid}/context/files", json={"path": "wiki/a.md", "content": "v1"}
    )
    assert created.status_code == 200, created.text
    read = client.get(f"/v1/projects/{pid}/context/files", params={"path": "wiki/a.md"}).json()
    assert read["content"] == "v1"

    saved = client.put(
        f"/v1/projects/{pid}/context/files",
        params={"path": "wiki/a.md"},
        json={"content": "v2", "base_sha": read["sha"]},
    )
    assert saved.status_code == 200
    stale = client.put(
        f"/v1/projects/{pid}/context/files",
        params={"path": "wiki/a.md"},
        json={"content": "v3", "base_sha": read["sha"]},
    )
    assert stale.status_code == 409

    renamed = client.post(
        f"/v1/projects/{pid}/context/files/rename",
        json={"path": "wiki/a.md", "new_path": "wiki/b.md"},
    )
    assert renamed.status_code == 200
    deleted = client.delete(f"/v1/projects/{pid}/context/files", params={"path": "wiki/b.md"})
    assert deleted.status_code == 200
    assert (
        client.get(f"/v1/projects/{pid}/context/files", params={"path": "wiki/b.md"}).status_code
        == 404
    )


def test_path_traversal_and_readonly_over_http(client: TestClient, tmp_path: Path) -> None:
    """Traversal is 400 and raw/ writes are 403 through the API too."""
    (tmp_path / "secret.md").write_text("nope", encoding="utf-8")
    pid = _configured_project(client, tmp_path / "ctx")
    resp = client.get(f"/v1/projects/{pid}/context/files", params={"path": "../secret.md"})
    assert resp.status_code == 400
    resp = client.post(
        f"/v1/projects/{pid}/context/files", json={"path": "raw/x.md", "content": "x"}
    )
    assert resp.status_code == 403


def test_search_preview_graph_history(client: TestClient, tmp_path: Path) -> None:
    """Search, preview, both graph kinds, and history respond with data."""
    ctx = tmp_path / "ctx"
    pid = _configured_project(client, ctx)
    client.post(
        f"/v1/projects/{pid}/context/files",
        json={"path": "system/rules.md", "content": "Always use uv run."},
    )
    client.post(
        f"/v1/projects/{pid}/context/files",
        json={"path": "wiki/exp.md", "content": "---\ndescription: Exps\n---\nSee [[rules]]"},
    )
    write_graph(ctx / "graph" / "graph.json")

    hits = client.get(f"/v1/projects/{pid}/context/search", params={"q": "uv run"}).json()
    assert hits["results"][0]["path"] == "system/rules.md"

    preview = client.get(f"/v1/projects/{pid}/context/preview").json()
    assert "Always use uv run." in preview["text"]
    assert "wiki/exp.md — Exps" in preview["text"]

    knowledge = client.get(f"/v1/projects/{pid}/context/graph", params={"kind": "knowledge"})
    assert {"source": "wiki/exp.md", "target": "system/rules.md", "relation": "wikilink"} in (
        knowledge.json()["edges"]
    )
    code = client.get(f"/v1/projects/{pid}/context/graph", params={"kind": "code", "limit": 3})
    assert code.json()["truncated"] is True and len(code.json()["nodes"]) == 3
    assert (
        client.get(f"/v1/projects/{pid}/context/graph", params={"kind": "bogus"}).status_code
        == 422
    )
    assert client.get(f"/v1/projects/{pid}/context/history").json()["versioned"] is False


# ── session-scoped views ─────────────────────────────────────────────────


def _session_in_project(db_uri: str, project_id: str | None) -> str:
    _ensure_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="s", agent_id=AGENT_ID)
    if project_id is not None:
        store.set_conversation_project(conv.id, project_id)
    return conv.id


def test_session_views_single_user(client: TestClient, db_uri: str, tmp_path: Path) -> None:
    """A filed session reaches list/read/search/graph/injected views."""
    ctx = tmp_path / "ctx"
    pid = _configured_project(client, ctx)
    client.post(
        f"/v1/projects/{pid}/context/files",
        json={"path": "wiki/notes.md", "content": "line1\nline2\nline3\n"},
    )
    write_graph(ctx / "graph" / "graph.json")
    sid = _session_in_project(db_uri, pid)
    base = f"/v1/sessions/{sid}/context"

    listing = client.get(f"{base}/list").json()
    assert listing["has_code_graph"] is True
    assert any(f["path"] == "wiki/notes.md" for f in listing["files"])

    page = client.get(f"{base}/files", params={"path": "wiki/notes.md", "offset": 1, "limit": 1})
    assert page.json()["content"] == "line2\n"
    assert page.json()["total_lines"] == 3 and page.json()["truncated"] is True

    assert client.get(f"{base}/search", params={"q": "line3"}).json()["results"][0]["line"] == 3
    query = client.get(f"{base}/graph/query", params={"q": "psnr"}).json()
    assert "compute_psnr" in query["text"]
    near = client.get(f"{base}/graph/neighbors", params={"node": "Encoder"}).json()
    assert near["neighbors"][0]["id"] == "train_main"
    injected = client.get(f"{base}/injected").json()
    assert injected["project_id"] == pid and "context_search" in injected["text"]


def test_session_trace_records_injection_and_tool_calls(
    client: TestClient, db_uri: str, tmp_path: Path
) -> None:
    """Injected fetches and tool calls are traced; reading the trace is not."""
    ctx = tmp_path / "ctx"
    pid = _configured_project(client, ctx)
    client.post(
        f"/v1/projects/{pid}/context/files",
        json={"path": "wiki/notes.md", "content": "psnr notes"},
    )
    write_graph(ctx / "graph" / "graph.json")
    sid = _session_in_project(db_uri, pid)
    base = f"/v1/sessions/{sid}/context"
    client.get(f"{base}/injected")
    client.get(f"{base}/files", params={"path": "wiki/notes.md"})
    client.get(f"{base}/search", params={"q": "psnr"})
    client.get(f"{base}/graph/neighbors", params={"node": "Encoder"})

    trace = client.get(f"{base}/trace").json()
    assert trace["project_name"] == f"P-{ctx.name}"
    assert "Project context" in trace["injected"]["text"]
    events = trace["events"]
    assert [e.get("tool", e["kind"]) for e in events] == [
        "injected",
        "context_read",
        "context_search",
        "graph_neighbors",
    ]
    assert events[0]["sha256"] == trace["injected"]["sha256"]
    assert events[1]["paths"] == ["wiki/notes.md"]
    assert events[2]["args"] == {"query": "psnr", "limit": 10}
    assert set(events[3]["node_ids"]) == {"model_encoder", "train_main"}
    # The trace file is hidden bookkeeping, not an addressable context file.
    hidden = client.get(
        f"/v1/projects/{pid}/context/files", params={"path": f".traces/{sid}.jsonl"}
    )
    assert hidden.status_code == 400
    assert len(client.get(f"{base}/trace").json()["events"]) == 4


def test_injected_skips_natively_loaded_profile_files_per_harness(
    client: TestClient, db_uri: str, tmp_path: Path
) -> None:
    """Claude launches omit the profile CLAUDE.md; the preview says so."""
    profile = tmp_path / "me"
    profile.mkdir()
    (profile / "CLAUDE.md").write_text("Global rules.", encoding="utf-8")
    ctx = tmp_path / "ctx"
    project = client.post(
        "/v1/projects",
        json={
            "name": "P",
            "config": {"context": {"path": str(ctx), "profile_path": str(profile)}},
        },
    ).json()
    pid = project["id"]
    client.post(f"/v1/projects/{pid}/context/init")
    sid = _session_in_project(db_uri, pid)
    base = f"/v1/sessions/{sid}/context/injected"

    claude = client.get(base, params={"harness": "claude-native"}).json()
    assert "Global rules." not in claude["text"]
    assert claude["skipped"] == ["profile/CLAUDE.md"]
    assert "Global rules." in client.get(base).json()["text"]
    preview = client.get(f"/v1/projects/{pid}/context/preview").json()
    assert "Global rules." in preview["text"]
    assert preview["skipped_by_harness"] == {"claude-native": ["profile/CLAUDE.md"]}
    events = client.get(f"/v1/sessions/{sid}/context/trace").json()["events"]
    assert events[0]["skipped"] == ["profile/CLAUDE.md"]


def test_session_without_project_404(client: TestClient, db_uri: str) -> None:
    """A session outside any project has no context."""
    sid = _session_in_project(db_uri, None)
    assert client.get(f"/v1/sessions/{sid}/context/list").status_code == 404


def test_subagent_session_inherits_root_project(
    client: TestClient, db_uri: str, tmp_path: Path
) -> None:
    """A child session resolves its root session's project context."""
    pid = _configured_project(client, tmp_path / "ctx")
    root_id = _session_in_project(db_uri, pid)
    child = SqlAlchemyConversationStore(db_uri).create_conversation(
        kind="sub_agent", title="worker:a", parent_conversation_id=root_id, agent_id=AGENT_ID
    )
    assert client.get(f"/v1/sessions/{child.id}/context/list").status_code == 200


# ── multi-user: owner-private ────────────────────────────────────────────


def test_context_is_owner_private(db_uri: str, tmp_path: Path) -> None:
    """Bob cannot reach Alice's project context, even via a session shared to him."""
    client = TestClient(_app(db_uri, multi_user=True))
    pid = _configured_project(client, tmp_path / "ctx", **_hdr(ALICE))
    client.post(
        f"/v1/projects/{pid}/context/files",
        json={"path": "system/private.md", "content": "alice only"},
        headers=_hdr(ALICE),
    )
    assert client.get(f"/v1/projects/{pid}/context", headers=_hdr(BOB)).status_code == 404
    assert client.get(f"/v1/projects/{pid}/context/preview", headers=_hdr(BOB)).status_code == 404

    sid = _session_in_project(db_uri, pid)
    perms = SqlAlchemyPermissionStore(db_uri)
    perms.ensure_user(ALICE)
    perms.ensure_user(BOB)
    perms.grant(ALICE, sid, LEVEL_OWNER)
    perms.grant(BOB, sid, LEVEL_READ)

    assert client.get(f"/v1/sessions/{sid}/context/list", headers=_hdr(ALICE)).status_code == 200
    for suffix in ("list", "injected", "search?q=alice", "trace"):
        resp = client.get(f"/v1/sessions/{sid}/context/{suffix}", headers=_hdr(BOB))
        assert resp.status_code == 404, suffix
    # A user with no grant at all is also refused.
    carol = client.get(f"/v1/sessions/{sid}/context/list", headers=_hdr("carol@example.com"))
    assert carol.status_code == 404


# ── update job + proposals ───────────────────────────────────────────────


class _RecordingCurator:
    """Fake curator: proposes one wiki page and records what it was shown."""

    def __init__(self) -> None:
        self.requests: list[CuratorRequest] = []

    async def propose(self, request: CuratorRequest) -> list[ProposalDraft]:
        self.requests.append(request)
        return [
            ProposalDraft(
                path="wiki/decisions.md",
                action="create",
                new_content="---\ndescription: Decisions\n---\nUse cosine LR.",
                rationale="Captured the LR decision",
                sources=[item.ref for item in request.inputs],
            )
        ]


def _poll_job(client: TestClient, pid: str, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = client.get(f"/v1/projects/{pid}/context/jobs/{job_id}").json()
        if job["status"] != "running":
            return job
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def test_update_job_proposal_review_flow(db_uri: str, tmp_path: Path) -> None:
    """Update gathers raw + session inputs, stores a proposal, apply writes it."""
    curator = _RecordingCurator()
    manager = ContextJobManager(lambda: curator)
    with TestClient(_app(db_uri, job_manager=manager)) as client:
        ctx = tmp_path / "ctx"
        pid = _configured_project(client, ctx)
        (ctx / "raw" / "meeting.md").write_text("Decided: cosine LR.", encoding="utf-8")
        sid = _session_in_project(db_uri, pid)
        SqlAlchemyConversationStore(db_uri).append(
            sid,
            [
                NewConversationItem(
                    type="message",
                    response_id="resp_1",
                    data=MessageData(
                        role="user", content=[{"type": "input_text", "text": "switch to cosine"}]
                    ),
                )
            ],
        )

        started = client.post(f"/v1/projects/{pid}/context/update", json={"code_graph": False})
        assert started.status_code == 200, started.text
        job = _poll_job(client, pid, started.json()["id"])
        assert job["status"] == "succeeded", job
        assert job["proposals_created"] == 1

        refs = {item.ref for item in curator.requests[0].inputs}
        assert refs == {"raw/meeting.md", f"session:{sid}"}
        session_input = next(i for i in curator.requests[0].inputs if i.kind == "session")
        assert "user: switch to cosine" in session_input.text

        status = client.get(f"/v1/projects/{pid}/context").json()
        assert status["pending_proposals"] == 1 and status["active_job"] is None
        proposals = client.get(f"/v1/projects/{pid}/context/proposals").json()["proposals"]
        assert proposals[0]["path"] == "wiki/decisions.md"
        assert proposals[0]["current_content"] is None and proposals[0]["stale"] is False

        applied = client.post(f"/v1/projects/{pid}/context/proposals/{proposals[0]['id']}/apply")
        assert applied.status_code == 200, applied.text
        read = client.get(
            f"/v1/projects/{pid}/context/files", params={"path": "wiki/decisions.md"}
        )
        assert "cosine" in read.json()["content"]
        assert client.get(f"/v1/projects/{pid}/context/proposals").json()["proposals"] == []

        # A second run sees nothing new (the stamp advanced), so no curator call.
        again = client.post(f"/v1/projects/{pid}/context/update", json={})
        assert _poll_job(client, pid, again.json()["id"])["status"] == "succeeded"
        assert len(curator.requests) == 1

        missing = client.post(f"/v1/projects/{pid}/context/proposals/{'a' * 32}/reject")
        assert missing.status_code == 404
        assert client.get(f"/v1/projects/{pid}/context/jobs/nope").status_code == 404


def test_update_requires_initialised_context(client: TestClient, tmp_path: Path) -> None:
    """Updating a configured but uninitialised folder is a 400."""
    project = client.post(
        "/v1/projects",
        json={"name": "raw", "config": {"context": {"path": str(tmp_path / "nothing")}}},
    ).json()
    resp = client.post(f"/v1/projects/{project['id']}/context/update", json={})
    assert resp.status_code == 400
