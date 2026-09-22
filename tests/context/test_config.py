"""Tests for project ``context`` config parsing and validation."""

from __future__ import annotations

import os

import pytest

from omnigent.context import parse_context_config
from omnigent.errors import OmnigentError


def test_absent_or_empty_context_is_none() -> None:
    """No ``context`` key (or an empty object) means no context configured."""
    assert parse_context_config(None) is None
    assert parse_context_config({}) is None
    assert parse_context_config({"workspace": "/x"}) is None
    assert parse_context_config({"context": {}}) is None
    assert parse_context_config({"context": None}) is None


def test_paths_are_normalised_and_tilde_expanded() -> None:
    """``~`` expands and redundant separators normalise."""
    cfg = parse_context_config(
        {
            "context": {
                "path": "~/context//projects/x/",
                "profile_path": "/tmp/me/../me",
                "repo_path": "",
            }
        }
    )
    assert cfg is not None
    assert cfg.path == os.path.join(os.path.expanduser("~"), "context", "projects", "x")
    assert cfg.profile_path == "/tmp/me"
    assert cfg.repo_path is None


@pytest.mark.parametrize(
    "context",
    [
        "not-an-object",
        {"profile_path": "/tmp/me"},
        {"path": "relative/dir"},
        {"path": "/tmp/bad\x00path"},
        {"path": 42},
        {"path": "/"},
        {"path": "/tmp/ok", "repo_path": "relative"},
    ],
)
def test_invalid_context_is_rejected(context: object) -> None:
    """Malformed blocks raise a 400 rather than being silently ignored."""
    with pytest.raises(OmnigentError) as excinfo:
        parse_context_config({"context": context})
    assert excinfo.value.http_status == 400
