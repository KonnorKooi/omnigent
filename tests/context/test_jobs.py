"""Tests for the Update-context job, proposal storage and the curator seam."""

from __future__ import annotations

import json
import os
import shutil
import stat
import time
from pathlib import Path

import pytest

from omnigent.context import ContextConfig, ContextService
from omnigent.context.curator import (
    ClaudeCliCurator,
    CuratorInput,
    CuratorRequest,
    ProposalDraft,
    build_curator_prompt,
    parse_curator_output,
)
from omnigent.context.jobs import (
    ContextJobManager,
    ProposalStore,
    gather_raw_inputs,
    run_graphify_code_graph,
)
from omnigent.errors import OmnigentError
from tests.context._helpers import init_git_repo


class FakeCurator:
    """Records requests and returns canned drafts."""

    def __init__(self, drafts: list[ProposalDraft] | None = None, error: bool = False) -> None:
        self.drafts = drafts or []
        self.error = error
        self.requests: list[CuratorRequest] = []

    async def propose(self, request: CuratorRequest) -> list[ProposalDraft]:
        self.requests.append(request)
        if self.error:
            raise RuntimeError("model unavailable")
        return self.drafts


async def _no_sessions(since: int | None) -> list[CuratorInput]:
    return []


def _manager(curator: FakeCurator | None, **kwargs: object) -> ContextJobManager:
    return ContextJobManager(lambda: curator, **kwargs)  # type: ignore[arg-type]


# ── curator parsing / prompt ─────────────────────────────────────────────


def test_parse_curator_output_handles_fences_and_bad_entries() -> None:
    """Fenced JSON parses; entries with missing fields or bad actions are dropped."""
    text = (
        "Here you go:\n```json\n"
        + json.dumps(
            [
                {
                    "path": "wiki/a.md",
                    "action": "create",
                    "new_content": "x",
                    "rationale": "new",
                    "sources": ["raw/p.md"],
                },
                {"path": "wiki/b.md", "action": "delete", "new_content": ""},
                {"path": 3, "new_content": "y"},
            ]
        )
        + "\n```"
    )
    drafts = parse_curator_output(text)
    assert drafts == [ProposalDraft("wiki/a.md", "create", "x", "new", ["raw/p.md"])]
    assert parse_curator_output("[]") == []
    with pytest.raises(ValueError):
        parse_curator_output("no json here")


def test_prompt_contains_rules_files_and_inputs() -> None:
    """The prompt carries rules, current files, citations and output format."""
    prompt = build_curator_prompt(
        CuratorRequest(
            context_md="guide",
            files={"system/rules.md": "Use uv."},
            inputs=[CuratorInput("session", "session:conv_1", "Debug run", "user: hi")],
        )
    )
    assert "Keep system/ minimal" in prompt
    assert "### system/rules.md" in prompt and "Use uv." in prompt
    assert "[session:conv_1] Debug run" in prompt
    assert "Respond with ONLY a JSON array" in prompt


@pytest.mark.asyncio
async def test_claude_cli_curator_runs_subprocess(tmp_path: Path) -> None:
    """The CLI curator pipes the prompt on stdin and unwraps the JSON envelope."""
    capture = tmp_path / "stdin.txt"
    script = tmp_path / "fake-claude"
    result = json.dumps([{"path": "wiki/a.md", "action": "create", "new_content": "hi"}])
    envelope = json.dumps({"type": "result", "is_error": False, "result": result})
    script.write_text(
        f"#!/bin/sh\ncat > \"{capture}\"\nprintf '%s' '{envelope}'\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    curator = ClaudeCliCurator(str(script), timeout_s=30)
    drafts = await curator.propose(CuratorRequest("guide", {}, []))
    assert [d.path for d in drafts] == ["wiki/a.md"]
    assert "Respond with ONLY a JSON array" in capture.read_text(encoding="utf-8")
    assert curator.argv()[1:] == ["-p", "--output-format", "json"]


@pytest.mark.asyncio
async def test_claude_cli_curator_reports_failures(tmp_path: Path) -> None:
    """A non-zero exit and a timeout both raise with a useful message."""
    failing = tmp_path / "fail"
    failing.write_text("#!/bin/sh\necho boom >&2\nexit 3\n", encoding="utf-8")
    failing.chmod(0o755)
    with pytest.raises(RuntimeError, match="exited 3: boom"):
        await ClaudeCliCurator(str(failing), timeout_s=30).propose(CuratorRequest("", {}, []))
    slow = tmp_path / "slow"
    slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    slow.chmod(0o755)
    with pytest.raises(RuntimeError, match="timed out"):
        await ClaudeCliCurator(str(slow), timeout_s=0.2).propose(CuratorRequest("", {}, []))


# ── inputs ───────────────────────────────────────────────────────────────


def test_gather_raw_inputs_respects_since_and_secrets(service: ContextService) -> None:
    """Only text files changed after ``since`` are gathered; secrets never are."""
    old = service.root / "raw" / "old.md"
    old.write_text("old", encoding="utf-8")
    past = time.time() - 1000
    os.utime(old, (past, past))
    (service.root / "raw" / "new.txt").write_text("new", encoding="utf-8")
    (service.root / "raw" / "api_secret.md").write_text("s", encoding="utf-8")
    (service.root / "raw" / "image.png").write_bytes(b"\x89PNG")
    assert {i.ref for i in gather_raw_inputs(service, None)} == {"raw/old.md", "raw/new.txt"}
    assert [i.ref for i in gather_raw_inputs(service, int(past) + 10)] == ["raw/new.txt"]


# ── job manager ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_creates_valid_proposals_and_stamps(service: ContextService) -> None:
    """Valid drafts become proposals; invalid ones are logged and dropped."""
    service.create("wiki/existing.md", "old body")
    (service.root / "raw" / "notes.md").write_text("we switched to cosine LR", encoding="utf-8")
    curator = FakeCurator(
        [
            ProposalDraft("wiki/lr.md", "create", "---\ndescription: LR\n---\ncosine", "new", []),
            ProposalDraft("wiki/existing.md", "edit", "new body", "update", ["raw/notes.md"]),
            ProposalDraft("raw/notes.md", "edit", "tampered", "nope", []),
            ProposalDraft("../escape.md", "create", "x", "nope", []),
            ProposalDraft("wiki/existing.md", "create", "dup", "nope", []),
        ]
    )
    manager = _manager(curator)
    job = manager.start("p1", service, _no_sessions, code_graph=True)
    assert manager.active("p1") is job
    with pytest.raises(OmnigentError) as exc:
        manager.start("p1", service, _no_sessions)
    assert exc.value.http_status == 409
    await manager.wait(job.id)

    assert job.status == "succeeded", job.log
    assert job.step("code_graph").status == "skipped"
    assert job.proposals_created == 2
    assert sum("dropped proposal" in line for line in job.log) == 3
    assert service.last_update() is not None
    assert curator.requests[0].inputs[0].ref == "raw/notes.md"
    assert "wiki/existing.md" in curator.requests[0].files
    assert manager.get("p1", job.id) is job and manager.active("p1") is None


@pytest.mark.asyncio
async def test_job_with_nothing_new_skips_curator(service: ContextService) -> None:
    """No inputs means no curator call but the stamp still advances."""
    curator = FakeCurator()
    manager = _manager(curator)
    job = manager.start("p1", service, _no_sessions)
    await manager.wait(job.id)
    assert job.status == "succeeded"
    assert job.step("proposals").detail == "nothing new since the last update"
    assert curator.requests == []
    assert service.last_update() is not None


@pytest.mark.asyncio
async def test_curator_failure_fails_job_without_stamp(service: ContextService) -> None:
    """A curator error fails the job and keeps inputs for the next run."""
    (service.root / "raw" / "notes.md").write_text("x", encoding="utf-8")
    manager = _manager(FakeCurator(error=True))
    job = manager.start("p1", service, _no_sessions)
    await manager.wait(job.id)
    assert job.status == "failed"
    assert "model unavailable" in (job.error or "")
    assert service.last_update() is None


@pytest.mark.asyncio
async def test_missing_curator_is_skipped(service: ContextService) -> None:
    """Without a curator (no claude CLI) the proposals step is skipped, not failed."""
    (service.root / "raw" / "notes.md").write_text("x", encoding="utf-8")
    manager = _manager(None)
    job = manager.start("p1", service, _no_sessions, code_graph=False)
    await manager.wait(job.id)
    assert job.status == "succeeded"
    assert job.step("proposals").status == "skipped"
    assert job.step("stamp").status == "skipped"


@pytest.mark.asyncio
async def test_code_graph_step_uses_runner(tmp_path: Path) -> None:
    """With repo_path set, the (fake) graphify runner populates graph/."""
    repo = tmp_path / "repo"
    repo.mkdir()
    svc = ContextService(ContextConfig(path=str(tmp_path / "ctx"), repo_path=str(repo)))
    svc.init()
    calls: list[tuple[Path, Path]] = []

    def fake_runner(repo_path: Path, graph_dir: Path, log: object) -> int:
        calls.append((repo_path, graph_dir))
        (graph_dir / "graph.json").write_text('{"nodes": [], "links": []}', encoding="utf-8")
        return 1

    manager = _manager(FakeCurator(), code_graph_runner=fake_runner)
    job = manager.start("p1", svc, _no_sessions, proposals=False)
    await manager.wait(job.id)
    assert job.status == "succeeded"
    assert calls == [(repo, svc.root / "graph")]
    assert svc.code_graph()["available"] is True


@pytest.mark.skipif(shutil.which("graphify") is None, reason="graphify CLI not installed")
def test_real_graphify_code_graph(tmp_path: Path) -> None:
    """End-to-end against the real graphify CLI on a tiny throwaway repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(
        "def helper(x):\n    return x * 2\n\n\ndef main():\n    return helper(3)\n",
        encoding="utf-8",
    )
    graph_dir = tmp_path / "graph"
    lines: list[str] = []
    copied = run_graphify_code_graph(repo, graph_dir, lines.append)
    assert copied >= 1
    document = json.loads((graph_dir / "graph.json").read_text(encoding="utf-8"))
    assert any("helper" in node["label"] for node in document["nodes"])
    assert all("community" in node for node in document["nodes"])
    # graphify output never lands inside the source repo.
    assert sorted(p.name for p in repo.iterdir()) == ["a.py"]


# ── proposals ────────────────────────────────────────────────────────────


def test_proposal_apply_reject_and_stale_conflict(git_identity: None, tmp_path: Path) -> None:
    """Apply writes + commits; a file edited since the proposal is a 409."""
    repo = tmp_path / "repo"
    init_git_repo(repo)
    svc = ContextService(ContextConfig(path=str(repo)))
    svc.init()
    svc.create("wiki/a.md", "v1")
    store = ProposalStore(svc)
    fresh = store.save(
        {
            "path": "wiki/a.md",
            "action": "edit",
            "new_content": "v2",
            "rationale": "Record the new learning rate schedule",
            "sources": [],
            "base_sha": svc.current_sha("wiki/a.md"),
            "job_id": None,
        }
    )
    listed = store.list()
    assert listed[0]["current_content"] == "v1" and listed[0]["stale"] is False

    result = store.apply(fresh["id"])
    assert result["commit"]
    assert svc.read("wiki/a.md")["content"] == "v2"
    assert svc.history()["commits"][0]["subject"].startswith(
        f"context: apply proposal {fresh['id']} — Record the new"
    )
    assert store.list() == []

    stale = store.save(
        {
            "path": "wiki/a.md",
            "action": "edit",
            "new_content": "v3",
            "rationale": "",
            "sources": [],
            "base_sha": "0" * 64,
            "job_id": None,
        }
    )
    assert store.list()[0]["stale"] is True
    with pytest.raises(OmnigentError) as exc:
        store.apply(stale["id"])
    assert exc.value.http_status == 409
    store.delete(stale["id"])
    with pytest.raises(OmnigentError) as exc:
        store.delete(stale["id"])
    assert exc.value.http_status == 404
    with pytest.raises(OmnigentError):
        store.get("../../etc/passwd")
