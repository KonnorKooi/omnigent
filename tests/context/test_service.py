"""Tests for :class:`omnigent.context.ContextService`.

Covers the security surface from ``designs/PROJECT_CONTEXT.md`` §6 (path
confinement including symlink escapes, write restrictions, size caps, secret
files) as well as tree/search/injection/graph/history behaviour.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.context import ContextConfig, ContextService
from omnigent.context.service import MAX_WRITE_BYTES, TOOL_USAGE_NOTE, sha256_bytes
from omnigent.errors import OmnigentError
from tests.context._helpers import init_git_repo, write_graph


def _status(exc: pytest.ExceptionInfo[OmnigentError]) -> int:
    return exc.value.http_status


# ── init ─────────────────────────────────────────────────────────────────


def test_init_scaffolds_and_is_idempotent(context_root: Path) -> None:
    """Init creates the layout once and never overwrites existing files."""
    svc = ContextService(ContextConfig(path=str(context_root)))
    created = svc.init()
    assert set(created) >= {"system/", "wiki/", "raw/", "graph/", "CONTEXT.md", ".gitignore"}
    (context_root / "CONTEXT.md").write_text("custom", encoding="utf-8")
    assert svc.init() == []
    assert (context_root / "CONTEXT.md").read_text(encoding="utf-8") == "custom"
    gitignore = (context_root / ".gitignore").read_text(encoding="utf-8")
    assert ".proposals/" in gitignore and ".traces/" in gitignore


def test_init_rejects_file_at_root(tmp_path: Path) -> None:
    """A context path that is a regular file cannot be initialised."""
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(OmnigentError) as exc:
        ContextService(ContextConfig(path=str(target))).init()
    assert _status(exc) == 400


# ── path confinement ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/etc/passwd",
        "../outside.md",
        "wiki/../../outside.md",
        "wiki/..",
        "C:/windows",
        "wiki\\a.md",
        "wiki/a\x00.md",
        ".git/config",
        ".proposals/p.json",
        "wiki/.hidden/a.md",
        "system/.secret.md",
        "notes/a.md",
        "graphify-out/graph.json",
    ],
)
def test_resolve_rejects_unsafe_paths(service: ContextService, path: str) -> None:
    """Traversal, absolute, hidden and out-of-layout paths are refused."""
    with pytest.raises(OmnigentError) as exc:
        service.resolve(path)
    assert _status(exc) == 400


def test_symlink_escape_is_rejected(service: ContextService, tmp_path: Path) -> None:
    """A symlink inside the root that points outside cannot be read or written."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("top secret", encoding="utf-8")
    os.symlink(outside, service.root / "wiki" / "link")
    os.symlink(outside / "secret.md", service.root / "wiki" / "file-link.md")

    for path in ("wiki/link/secret.md", "wiki/file-link.md"):
        with pytest.raises(OmnigentError) as exc:
            service.read(path)
        assert _status(exc) == 400
    with pytest.raises(OmnigentError):
        service.create("wiki/link/new.md", "x")
    assert not (outside / "new.md").exists()
    # The escaping links are not listed either.
    assert all("link" not in f["path"] for f in service.tree()["files"])


def test_symlinked_root_is_allowed(tmp_path: Path) -> None:
    """The configured root itself may be a symlink (e.g. into a synced folder)."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    os.symlink(real, link)
    svc = ContextService(ContextConfig(path=str(link)))
    svc.init()
    svc.create("wiki/a.md", "hello")
    assert svc.read("wiki/a.md")["content"] == "hello"


# ── read / write ────────────────────────────────────────────────────────


def test_create_read_write_delete_roundtrip(service: ContextService) -> None:
    """Create, read (with sha), update with base_sha, then delete."""
    created = service.create("wiki/topic.md", "v1")
    read = service.read("wiki/topic.md")
    assert read["content"] == "v1"
    assert read["sha"] == created["sha"] == sha256_bytes(b"v1")
    assert read["writable"] is True

    saved = service.write("wiki/topic.md", "v2", read["sha"])
    assert saved["sha"] == sha256_bytes(b"v2")
    assert service.read("wiki/topic.md")["content"] == "v2"

    assert service.delete("wiki/topic.md")["deleted"] is True
    with pytest.raises(OmnigentError) as exc:
        service.read("wiki/topic.md")
    assert _status(exc) == 404


def test_write_conflict_on_stale_sha(service: ContextService) -> None:
    """A save based on an outdated sha is a 409 and does not overwrite."""
    service.create("system/rules.md", "original")
    stale = service.read("system/rules.md")["sha"]
    service.write("system/rules.md", "someone else", stale)
    with pytest.raises(OmnigentError) as exc:
        service.write("system/rules.md", "mine", stale)
    assert _status(exc) == 409
    assert service.read("system/rules.md")["content"] == "someone else"


def test_create_existing_conflicts(service: ContextService) -> None:
    """Creating over an existing file is a 409."""
    service.create("wiki/a.md", "x")
    with pytest.raises(OmnigentError) as exc:
        service.create("wiki/a.md", "y")
    assert _status(exc) == 409


@pytest.mark.parametrize("path", ["raw/paper.md", "graph/GRAPH_REPORT.md", "CONTEXT.md"])
def test_writes_outside_system_and_wiki_are_forbidden(service: ContextService, path: str) -> None:
    """raw/, graph/ and root files are read-only."""
    with pytest.raises(OmnigentError) as exc:
        service.create(path, "x")
    assert _status(exc) == 403


def test_non_markdown_writes_rejected(service: ContextService) -> None:
    """Only .md/.txt may be written."""
    with pytest.raises(OmnigentError) as exc:
        service.create("wiki/script.py", "print(1)")
    assert _status(exc) == 400


def test_write_size_cap(service: ContextService) -> None:
    """Files over the 1 MB cap are refused."""
    with pytest.raises(OmnigentError) as exc:
        service.create("wiki/big.md", "x" * (MAX_WRITE_BYTES + 1))
    assert _status(exc) == 400
    assert not (service.root / "wiki" / "big.md").exists()


def test_rename(service: ContextService) -> None:
    """Rename moves a file within writable folders and refuses clobbering."""
    service.create("wiki/old.md", "content")
    service.create("wiki/taken.md", "x")
    with pytest.raises(OmnigentError) as exc:
        service.rename("wiki/old.md", "wiki/taken.md")
    assert _status(exc) == 409
    with pytest.raises(OmnigentError) as exc:
        service.rename("wiki/old.md", "raw/old.md")
    assert _status(exc) == 403
    result = service.rename("wiki/old.md", "system/new.md")
    assert result["path"] == "system/new.md"
    assert service.read("system/new.md")["content"] == "content"


def test_secret_files_listed_but_not_readable(service: ContextService) -> None:
    """``.env``-like files in raw/ appear in the tree but cannot be read or searched."""
    (service.root / "raw" / ".env").write_text("TOKEN=hunter2", encoding="utf-8")
    (service.root / "raw" / "api_secret.txt").write_text("hunter2", encoding="utf-8")
    listed = {f["path"]: f for f in service.tree()["files"]}
    assert listed["raw/.env"]["readable"] is False
    assert listed["raw/api_secret.txt"]["readable"] is False
    for path in ("raw/.env", "raw/api_secret.txt"):
        with pytest.raises(OmnigentError) as exc:
            service.read(path)
        assert _status(exc) == 403
    assert service.search("hunter2")["results"] == []


def test_binary_read(service: ContextService) -> None:
    """Binary files return no content and are not writable."""
    (service.root / "raw" / "blob.bin").write_bytes(b"\x00\x01\x02")
    read = service.read("raw/blob.bin")
    assert read["binary"] is True and read["content"] is None and read["writable"] is False


# ── tree / search ───────────────────────────────────────────────────────


def test_tree_metadata(service: ContextService) -> None:
    """Tree carries description, always_loaded and writable flags."""
    service.create("system/rules.md", "---\ndescription: House rules\n---\nUse uv.")
    service.create("wiki/exp.md", "---\ndescription: Experiments\n---\nbody")
    (service.root / "raw" / "paper.txt").write_text("abstract", encoding="utf-8")
    files = {f["path"]: f for f in service.tree()["files"]}
    assert files["system/rules.md"]["always_loaded"] is True
    assert files["system/rules.md"]["description"] == "House rules"
    assert files["wiki/exp.md"]["always_loaded"] is False
    assert files["wiki/exp.md"]["writable"] is True
    assert files["raw/paper.txt"]["writable"] is False
    assert "CONTEXT.md" in files


def test_search_ranks_phrase_matches(service: ContextService) -> None:
    """Whole-phrase hits outrank partial token hits; results carry line numbers."""
    service.create("wiki/a.md", "intro\nthe learning rate schedule is cosine\n")
    service.create("wiki/b.md", "rate limits\nschedule meeting\n")
    result = service.search("rate schedule", limit=10)
    top = result["results"][0]
    assert top["path"] == "wiki/a.md" and top["line"] == 2
    assert "cosine" in top["snippet"]
    assert service.search("nonexistent-term")["results"] == []


# ── injection ───────────────────────────────────────────────────────────


def test_injected_context_order_and_determinism(tmp_path: Path) -> None:
    """Profile, then sorted system files, then wiki index, then the tool note."""
    profile = tmp_path / "me"
    profile.mkdir()
    (profile / "about.md").write_text("---\ndescription: me\n---\nI like uv.", encoding="utf-8")
    svc = ContextService(ContextConfig(path=str(tmp_path / "ctx"), profile_path=str(profile)))
    svc.init()
    svc.create("system/b.md", "Rule B")
    svc.create("system/a.md", "---\ndescription: first\n---\nRule A")
    svc.create("wiki/topic.md", "---\ndescription: Topic summary\n---\nlong body not injected")

    first = svc.injected_context()
    text = first["text"]
    assert text.index("I like uv.") < text.index("Rule A") < text.index("Rule B")
    assert text.index("Rule B") < text.index("wiki/topic.md — Topic summary")
    assert "long body not injected" not in text
    assert "description: first" not in text
    assert text.rstrip().endswith(TOOL_USAGE_NOTE)
    assert first["files"] == ["profile/about.md", "system/a.md", "system/b.md"]
    assert svc.injected_context() == first


def test_injected_context_skips_profile_files_the_harness_loads(tmp_path: Path) -> None:
    """Claude loads ~/.claude/CLAUDE.md itself, so a profile CLAUDE.md is not sent to it."""
    profile = tmp_path / "me"
    profile.mkdir()
    (profile / "CLAUDE.md").write_text("Global rules.", encoding="utf-8")
    (profile / "about.md").write_text("I like uv.", encoding="utf-8")
    svc = ContextService(ContextConfig(path=str(tmp_path / "ctx"), profile_path=str(profile)))
    svc.init()

    claude = svc.injected_context(harness="claude-native")
    assert "Global rules." not in claude["text"]
    assert "I like uv." in claude["text"]
    assert claude["files"] == ["profile/about.md"]
    assert claude["skipped"] == ["profile/CLAUDE.md"]
    for harness in (None, "codex-native"):
        other = svc.injected_context(harness=harness)
        assert "Global rules." in other["text"]
        assert other["skipped"] == []


def test_injected_context_truncates_explicitly(service: ContextService) -> None:
    """Oversized context is cut with a notice, but the tool note survives."""
    service.create("system/huge.md", "\n".join(f"rule line {i}" for i in range(5000)))
    result = service.injected_context(max_chars=2000)
    assert result["truncated"] is True
    assert result["chars"] <= 2000
    assert "project context truncated" in result["text"]
    assert result["text"].rstrip().endswith(TOOL_USAGE_NOTE)


# ── graphs ──────────────────────────────────────────────────────────────


def test_knowledge_graph_links(service: ContextService) -> None:
    """Wikilinks (with alias/anchor) and relative md links become edges."""
    service.create("wiki/a.md", "See [[b]] and [[c|the C page]] and [d](../system/d.md).")
    service.create("wiki/b.md", "Back to [[a#intro]]. External [x](https://example.com).")
    service.create("wiki/c.md", "leaf")
    service.create("system/d.md", "rules")
    graph = service.knowledge_graph()
    edges = {(e["source"], e["target"], e["relation"]) for e in graph["edges"]}
    assert ("wiki/a.md", "wiki/b.md", "wikilink") in edges
    assert ("wiki/a.md", "wiki/c.md", "wikilink") in edges
    assert ("wiki/a.md", "system/d.md", "link") in edges
    assert ("wiki/b.md", "wiki/a.md", "wikilink") in edges
    groups = {n["id"]: n["group"] for n in graph["nodes"]}
    assert groups["system/d.md"] == "system"


def test_code_graph_truncation_and_queries(service: ContextService) -> None:
    """UI graph caps by degree; query and neighbours find the right nodes."""
    assert service.code_graph()["available"] is False
    write_graph(service.root / "graph" / "graph.json")

    ui = service.code_graph(limit=2)
    assert ui["available"] is True and ui["truncated"] is True
    assert ui["total_nodes"] == 4
    assert {n["id"] for n in ui["nodes"]} == {"train_main", "metrics_accumulator"}

    result = service.graph_query("where is psnr computed", budget=500)
    assert "metrics_psnr" in result["seeds"]
    assert "compute_psnr" in result["text"]
    assert "evaluation/metrics.py:L10" in result["text"]

    near = service.graph_neighbors("MetricAccumulator", depth=1)
    assert near["node"]["id"] == "metrics_accumulator"
    assert {n["id"] for n in near["neighbors"]} == {"metrics_psnr", "train_main"}
    two = service.graph_neighbors("metrics_psnr", depth=2)
    assert {n["id"] for n in two["neighbors"]} == {
        "metrics_accumulator",
        "train_main",
    }
    missing = service.graph_neighbors("nope-not-a-node")
    assert missing["node"] is None


# ── git ─────────────────────────────────────────────────────────────────


def test_git_commits_and_history(git_identity: None, tmp_path: Path) -> None:
    """In a git repo, edits are committed and show in history (paths relative)."""
    repo = tmp_path / "repo"
    init_git_repo(repo)
    svc = ContextService(ContextConfig(path=str(repo / "projects" / "x")))
    svc.init()
    created = svc.create("wiki/a.md", "one")
    assert created["commit"]
    sha = svc.read("wiki/a.md")["sha"]
    svc.write("wiki/a.md", "two", sha)
    # An unrelated dirty file in the repo is never swept into a context commit.
    (repo / "unrelated.txt").write_text("dirty", encoding="utf-8")
    svc.delete("wiki/a.md")

    history = svc.history()
    assert history["versioned"] is True
    subjects = [c["subject"] for c in history["commits"]]
    assert subjects[:3] == [
        "context: delete wiki/a.md",
        "context: update wiki/a.md",
        "context: create wiki/a.md",
    ]
    assert history["commits"][0]["files"] == ["wiki/a.md"]
    assert all("unrelated.txt" not in c["files"] for c in history["commits"])


def test_history_not_versioned(service: ContextService, git_identity: None) -> None:
    """Outside a git repo, history reports not versioned and writes still work."""
    assert service.history() == {"versioned": False, "commits": []}
    assert service.create("wiki/a.md", "x")["commit"] is None


def test_status_summary(service: ContextService) -> None:
    """Status reports existence, tree, and injected size."""
    service.create("system/a.md", "rule")
    status = service.status()
    assert status["configured"] and status["exists"] and status["initialized"]
    assert status["injected"]["chars"] > 0
    assert any(f["path"] == "system/a.md" for f in status["tree"]["files"])
