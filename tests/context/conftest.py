"""Shared fixtures for project-context tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.context import ContextConfig, ContextService


@pytest.fixture()
def git_identity(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give git a throwaway identity and config so commits work in CI."""
    home = tmp_path / "git-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Context Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "ctx@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Context Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "ctx@example.com")


@pytest.fixture()
def context_root(tmp_path: Path) -> Path:
    """An empty (not yet initialised) context directory path."""
    return tmp_path / "ctx"


@pytest.fixture()
def service(context_root: Path) -> ContextService:
    """A service over an initialised, non-versioned context directory."""
    svc = ContextService(ContextConfig(path=str(context_root)))
    svc.init()
    return svc
