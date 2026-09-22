"""Per-session trace of the project context a session loaded and read.

Each session gets an append-only JSONL file at
``<context>/.traces/<session_id>.jsonl`` (git-ignored by ``init``). Events:

- ``{"kind": "injected", "sha256", "chars", "files"}`` — startup injection.
- ``{"kind": "tool", "tool", "args", "paths" | "node_ids"}`` — a context tool
  call and what it returned.

Files live next to the context they describe, so the trace inherits the
context's owner-only access and needs no database table
(``designs/PROJECT_CONTEXT.md`` §4.6 leaves the storage to the implementer).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from omnigent.context.service import TRACES_DIR, ContextService

_logger = logging.getLogger(__name__)

#: Traces stop growing past this size (a runaway loop must not fill the disk).
MAX_TRACE_BYTES = 5 * 1024 * 1024
#: Events returned by :func:`read_trace` at most.
MAX_TRACE_EVENTS = 1000

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def _trace_path(service: ContextService, session_id: str) -> Path | None:
    """Location of a session's trace file.

    :param service: The project's context service.
    :param session_id: Session id (validated to a safe file name).
    :returns: The path, or ``None`` for an unsafe id.
    """
    if not _SESSION_ID_RE.match(session_id):
        return None
    return service.root / TRACES_DIR / f"{session_id}.jsonl"


def append_trace(service: ContextService, session_id: str, event: dict[str, Any]) -> None:
    """Append one event to a session's trace; failures are logged, never raised.

    :param service: The project's context service.
    :param session_id: The session the event belongs to.
    :param event: Event fields (``kind`` plus kind-specific data).
    """
    path = _trace_path(service, session_id)
    if path is None or not service.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file() and path.stat().st_size > MAX_TRACE_BYTES:
            return
        line = json.dumps({"ts": time.time(), **event}, default=str)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        _logger.warning("could not append context trace for %s: %s", session_id, exc)


def read_trace(service: ContextService, session_id: str) -> list[dict[str, Any]]:
    """Read a session's trace events in order (most recent :data:`MAX_TRACE_EVENTS`).

    :param service: The project's context service.
    :param session_id: Session id.
    :returns: Parsed events; malformed lines are skipped.
    """
    path = _trace_path(service, session_id)
    if path is None or not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except OSError:
        return []
    return events[-MAX_TRACE_EVENTS:]
