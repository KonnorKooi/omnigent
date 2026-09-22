"""Curator interface for the Update-context proposals step.

The curator reads what changed since the last update (new ``raw/`` sources and
recent session transcripts) together with the current context files, and
proposes *per-file* edits for human review (``designs/PROJECT_CONTEXT.md``
§4.5). It never writes: proposals are stored by the job and applied only when
the user accepts them.

:class:`ContextCurator` is the seam. The v1 implementation,
:class:`ClaudeCliCurator`, runs a headless ``claude -p`` subprocess — the design
allows this when routing the pass through an Omnigent session is too heavy for
v1. Tests substitute a fake curator.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import shutil
import tempfile
from typing import Any, Protocol

_logger = logging.getLogger(__name__)

#: Upper bound on the curator subprocess.
CLAUDE_CURATOR_TIMEOUT_S = 900.0
#: Characters of existing context and inputs placed in the prompt.
MAX_PROMPT_CONTEXT_CHARS = 60_000
MAX_PROMPT_INPUT_CHARS = 80_000

CURATOR_RULES = """\
You maintain a project's context repository: plain markdown that coding agents
read at the start of every session (system/) or on demand (wiki/).

Rules:
- Propose changes as whole-file contents, one entry per file. Only touch
  files under system/ or wiki/, with a .md extension.
- Edit incrementally: keep existing information unless it is wrong, and say
  why in the rationale when you remove anything.
- Keep system/ minimal: only non-standard rules and conventions an agent
  would otherwise get wrong. Put overviews, history, decisions and
  experiment results in wiki/ (one topic per file).
- Every wiki/ file starts with YAML frontmatter containing a one-line
  `description:`.
- Cite sources (raw/ paths or session ids) for each proposal.
- Propose nothing when the inputs contain nothing durable worth keeping.
"""

OUTPUT_INSTRUCTIONS = """\
Respond with ONLY a JSON array (no prose), each element:
{"path": "wiki/topic.md", "action": "create" | "edit",
 "new_content": "<entire new file content>",
 "rationale": "<one or two sentences>", "sources": ["raw/x.md", "session:<id>"]}
Return [] when nothing should change.
"""


@dataclasses.dataclass(frozen=True)
class CuratorInput:
    """One new piece of evidence for the curator.

    :param kind: ``"raw"`` for a ``raw/`` file, ``"session"`` for a transcript.
    :param ref: Citation handle, e.g. ``"raw/paper.md"`` or ``"session:conv_1"``.
    :param title: Human title (file path or session title).
    :param text: The content (already truncated by the gatherer).
    """

    kind: str
    ref: str
    title: str
    text: str


@dataclasses.dataclass(frozen=True)
class CuratorRequest:
    """Everything the curator sees for one run.

    :param context_md: The repository's ``CONTEXT.md`` guide.
    :param files: Current ``system/`` and ``wiki/`` files, path → content.
    :param inputs: New evidence since the last update.
    """

    context_md: str
    files: dict[str, str]
    inputs: list[CuratorInput]


@dataclasses.dataclass(frozen=True)
class ProposalDraft:
    """A curator's proposed change to one file.

    :param path: Target relative path under ``system/`` or ``wiki/``.
    :param action: ``"create"`` or ``"edit"``.
    :param new_content: Entire proposed file content.
    :param rationale: Why the change is worth making.
    :param sources: Citations (``raw/…`` paths or ``session:<id>``).
    """

    path: str
    action: str
    new_content: str
    rationale: str
    sources: list[str]


class ContextCurator(Protocol):
    """Produces per-file proposals from new inputs."""

    async def propose(self, request: CuratorRequest) -> list[ProposalDraft]:
        """Return proposed file changes for ``request``.

        :param request: Current context plus new inputs.
        :returns: Zero or more drafts (validated by the caller).
        """
        ...


def _clip(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` characters with an explicit marker.

    :param text: Source text.
    :param limit: Maximum characters kept.
    :returns: The possibly-truncated text.
    """
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... truncated {len(text) - limit} characters]"


def build_curator_prompt(request: CuratorRequest) -> str:
    """Render the full curator prompt.

    :param request: The run's inputs.
    :returns: Prompt text (rules, current files, new inputs, output format).
    """
    parts = [CURATOR_RULES, "## CONTEXT.md\n", request.context_md.strip() or "(none)", ""]
    parts.append("## Current context files\n")
    budget = MAX_PROMPT_CONTEXT_CHARS
    for path in sorted(request.files):
        content = request.files[path]
        block = f"### {path}\n```markdown\n{content}\n```\n"
        if len(block) > budget:
            parts.append(f"### {path}\n(omitted: prompt budget exhausted)\n")
            continue
        parts.append(block)
        budget -= len(block)
    if not request.files:
        parts.append("(no files yet)\n")
    parts.append("## New inputs since the last update\n")
    budget = MAX_PROMPT_INPUT_CHARS
    for item in request.inputs:
        text = _clip(item.text, max(0, min(budget, 20_000)))
        parts.append(f"### [{item.ref}] {item.title}\n{text}\n")
        budget -= len(text)
        if budget <= 0:
            parts.append("(remaining inputs omitted: prompt budget exhausted)\n")
            break
    parts.append(OUTPUT_INSTRUCTIONS)
    return "\n".join(parts)


_FENCE_RE = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)


def parse_curator_output(text: str) -> list[ProposalDraft]:
    """Parse a curator's JSON array (optionally fenced) into drafts.

    Malformed entries are dropped; shape validation of paths happens in the job.

    :param text: Raw model output.
    :returns: Parsed drafts.
    :raises ValueError: When no JSON array can be found.
    """
    candidate = text.strip()
    match = _FENCE_RE.search(candidate)
    if match:
        candidate = match.group(1)
    elif not candidate.startswith("["):
        start, end = candidate.find("["), candidate.rfind("]")
        if start < 0 or end <= start:
            raise ValueError("curator output contains no JSON array")
        candidate = candidate[start : end + 1]
    try:
        data = json.loads(candidate)
    except ValueError as exc:
        raise ValueError(f"curator output is not valid JSON: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError("curator output is not a JSON array")
    drafts: list[ProposalDraft] = []
    for raw in data:
        if not isinstance(raw, dict):
            continue
        path, content = raw.get("path"), raw.get("new_content")
        action = raw.get("action", "edit")
        if not isinstance(path, str) or not isinstance(content, str):
            continue
        if action not in ("create", "edit"):
            continue
        sources = raw.get("sources")
        drafts.append(
            ProposalDraft(
                path=path,
                action=action,
                new_content=content,
                rationale=str(raw.get("rationale") or "").strip(),
                sources=[str(s) for s in sources] if isinstance(sources, list) else [],
            )
        )
    return drafts


class ClaudeCliCurator:
    """Curator backed by a headless ``claude -p`` subprocess.

    The prompt goes over stdin; the subprocess runs in an empty temporary
    directory so no project ``CLAUDE.md`` or repository files leak into the
    pass, and it is killed after :data:`CLAUDE_CURATOR_TIMEOUT_S`.

    :param executable: Resolved path to the ``claude`` CLI.
    :param model: Optional ``--model`` override.
    :param timeout_s: Subprocess timeout.
    """

    def __init__(
        self,
        executable: str,
        *,
        model: str | None = None,
        timeout_s: float = CLAUDE_CURATOR_TIMEOUT_S,
    ) -> None:
        self.executable = executable
        self.model = model
        self.timeout_s = timeout_s

    @classmethod
    def from_path(cls) -> ClaudeCliCurator | None:
        """Build a curator when ``claude`` is on ``PATH``.

        ``OMNIGENT_CONTEXT_CURATOR_MODEL`` optionally pins the model.

        :returns: The curator, or ``None`` when the CLI is unavailable.
        """
        executable = shutil.which("claude")
        if executable is None:
            return None
        return cls(executable, model=os.environ.get("OMNIGENT_CONTEXT_CURATOR_MODEL") or None)

    def argv(self) -> list[str]:
        """The subprocess argv (never passed through a shell).

        :returns: e.g. ``["/usr/bin/claude", "-p", "--output-format", "json"]``.
        """
        args = [self.executable, "-p", "--output-format", "json"]
        if self.model:
            args += ["--model", self.model]
        return args

    async def propose(self, request: CuratorRequest) -> list[ProposalDraft]:
        """Run the headless pass and parse its proposals.

        :param request: Current context plus new inputs.
        :returns: Parsed drafts.
        :raises RuntimeError: When the CLI fails, times out, or returns
            unparseable output.
        """
        prompt = build_curator_prompt(request)
        with tempfile.TemporaryDirectory(prefix="omnigent-curator-") as workdir:
            proc = await asyncio.create_subprocess_exec(
                *self.argv(),
                cwd=workdir,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(prompt.encode("utf-8")), timeout=self.timeout_s
                )
            except TimeoutError as exc:
                proc.kill()
                await proc.wait()
                raise RuntimeError(
                    f"claude curator timed out after {self.timeout_s:.0f}s"
                ) from exc
        if proc.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise RuntimeError(f"claude curator exited {proc.returncode}: {detail}")
        text = stdout.decode("utf-8", errors="replace")
        try:
            envelope: Any = json.loads(text)
        except ValueError:
            envelope = None
        if isinstance(envelope, dict):
            if envelope.get("is_error"):
                raise RuntimeError(f"claude curator error: {envelope.get('result')}")
            text = str(envelope.get("result") or "")
        try:
            return parse_curator_output(text)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
