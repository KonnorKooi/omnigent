"""Tests for per-session context traces."""

from __future__ import annotations

from omnigent.context import ContextService
from omnigent.context.trace import MAX_TRACE_BYTES, append_trace, read_trace


def test_append_and_read_in_order(service: ContextService) -> None:
    """Events round-trip in order with a timestamp; malformed lines are skipped."""
    append_trace(service, "conv_1", {"kind": "injected", "sha256": "abc", "chars": 10})
    append_trace(service, "conv_1", {"kind": "tool", "tool": "context_read", "paths": ["a"]})
    path = service.root / ".traces" / "conv_1.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    events = read_trace(service, "conv_1")
    assert [e["kind"] for e in events] == ["injected", "tool"]
    assert all(isinstance(e["ts"], float) for e in events)
    assert read_trace(service, "conv_other") == []


def test_unsafe_session_ids_are_ignored(service: ContextService) -> None:
    """Session ids that are not plain file names never touch the filesystem."""
    append_trace(service, "../../escape", {"kind": "tool"})
    assert not (service.root.parent / "escape.jsonl").exists()
    assert read_trace(service, "../../escape") == []


def test_trace_size_cap(service: ContextService) -> None:
    """A trace past the cap stops growing."""
    path = service.root / ".traces" / "conv_big.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x" * (MAX_TRACE_BYTES + 1), encoding="utf-8")
    append_trace(service, "conv_big", {"kind": "tool"})
    assert path.stat().st_size == MAX_TRACE_BYTES + 1
