"""REST routes for project context repositories.

Two families (``designs/PROJECT_CONTEXT.md`` §4.2):

- ``/projects/{project_id}/context/...`` — the Context page. Owner-scoped
  through the project store: a project the caller does not own is a 404.
- ``/sessions/{session_id}/context/...`` — read-only views used by the agent
  tools and the session Context tab. The caller needs read access to the
  session, *and* the session's project must be one the caller owns. A
  recipient of a shared session therefore gets a 404: project context never
  crosses the per-session ACL boundary (PRD §9.4).

All filesystem work runs in a worker thread; the service itself enforces path
confinement and write restrictions.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict

from omnigent.context import ContextService, parse_context_config
from omnigent.context.curator import ClaudeCliCurator, CuratorInput
from omnigent.context.jobs import ContextJobManager, ProposalStore
from omnigent.context.service import NATIVELY_LOADED_PROFILE_FILES
from omnigent.context.trace import append_trace, read_trace
from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore

#: Sessions and characters per transcript handed to the curator per run.
_MAX_CURATOR_SESSIONS = 20
_MAX_TRANSCRIPT_CHARS = 20_000

#: Default and maximum lines returned by the session-scoped file read.
_DEFAULT_READ_LINES = 400
_MAX_READ_LINES = 2000


class ContextFileWriteRequest(BaseModel):
    """Body for ``PUT /projects/{id}/context/files``.

    :param content: New file content.
    :param base_sha: Sha256 of the content the client loaded, or ``None`` when
        creating a file that should not exist yet.
    """

    model_config = ConfigDict(extra="forbid")

    content: str
    base_sha: str | None = None


class ContextFileCreateRequest(BaseModel):
    """Body for ``POST /projects/{id}/context/files``.

    :param path: Relative path under ``system/`` or ``wiki/``.
    :param content: Initial content.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    content: str = ""


class ContextFileRenameRequest(BaseModel):
    """Body for ``POST /projects/{id}/context/files/rename``.

    :param path: Existing relative path.
    :param new_path: Destination relative path.
    """

    model_config = ConfigDict(extra="forbid")

    path: str
    new_path: str


class ContextUpdateRequest(BaseModel):
    """Body for ``POST /projects/{id}/context/update``.

    :param code_graph: Run the graphify code-graph refresh step.
    :param proposals: Run the curator proposals step.
    """

    model_config = ConfigDict(extra="forbid")

    code_graph: bool = True
    proposals: bool = True


def _transcript_text(items: list[Any]) -> str:
    """Flatten a session's message items into a plain transcript.

    Only user/assistant message text is kept; tool calls and outputs are
    dropped so the curator sees the conversation, not raw tool noise.

    :param items: Conversation items in chronological order.
    :returns: ``"role: text"`` blocks joined by blank lines.
    """
    lines: list[str] = []
    for item in items:
        if getattr(item, "type", None) != "message":
            continue
        data = item.data
        role = getattr(data, "role", None)
        content = getattr(data, "content", None)
        if role not in ("user", "assistant") or getattr(data, "is_meta", False):
            continue
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                text = block.get("text") if isinstance(block, dict) else None
                if isinstance(text, str) and text.strip():
                    texts.append(text)
        if texts:
            lines.append(f"{role}: " + "\n".join(texts))
    return "\n\n".join(lines)


def _slice_lines(content: str, offset: int, limit: int) -> dict[str, Any]:
    """Page a text file by lines for agent consumption.

    :param content: Whole file text.
    :param offset: Zero-based first line.
    :param limit: Maximum lines.
    :returns: ``{"content", "offset", "lines", "total_lines", "truncated"}``.
    """
    lines = content.splitlines(keepends=True)
    total = len(lines)
    start = max(0, min(offset, total))
    end = min(total, start + limit)
    return {
        "content": "".join(lines[start:end]),
        "offset": start,
        "lines": end - start,
        "total_lines": total,
        "truncated": end < total,
    }


def create_project_context_router(
    project_store: ProjectStore,
    conversation_store: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    job_manager: ContextJobManager | None = None,
) -> APIRouter:
    """Build the project context router (mounted under ``/v1``).

    :param project_store: Owner-scoped project persistence.
    :param conversation_store: Used to resolve a session's project.
    :param auth_provider: Identifies the requesting user; ``None`` in
        single-user mode.
    :param permission_store: Session ACLs for the session-scoped routes;
        ``None`` disables session permission checks (single-user).
    :param job_manager: Runs Update-context jobs; defaults to one using the
        headless ``claude`` CLI curator when it is on ``PATH``.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()
    jobs = job_manager or ContextJobManager(ClaudeCliCurator.from_path)

    async def _owned_project(request: Request, project_id: str) -> tuple[Project, str | None]:
        """Load a project the caller owns.

        :param request: Incoming request.
        :param project_id: Project id from the path.
        :returns: ``(project, user_id)``.
        :raises OmnigentError: 401 unauthenticated, 404 not found / not owned.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return project, user_id

    async def _project_service(request: Request, project_id: str) -> ContextService:
        """Build the context service for an owned, configured project.

        :param request: Incoming request.
        :param project_id: Project id.
        :returns: The service.
        :raises OmnigentError: 404 when the project has no context configured.
        """
        project, _ = await _owned_project(request, project_id)
        config = parse_context_config(project.config)
        if config is None:
            raise OmnigentError("Project has no context configured", code=ErrorCode.NOT_FOUND)
        return ContextService(config)

    async def _session_service(
        request: Request, session_id: str
    ) -> tuple[ContextService, Project, str | None]:
        """Resolve a session to its owner-visible project context.

        :param request: Incoming request.
        :param session_id: Session id from the path.
        :returns: ``(service, project, user_id)``.
        :raises OmnigentError: 404 when the session is not readable, has no
            project, the project is not the caller's, or has no context.
        """
        user_id = require_user(request, auth_provider)
        await require_access(user_id, session_id, LEVEL_READ, permission_store, conversation_store)
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)
        project_id = conversation.project_id
        if project_id is None and conversation.root_conversation_id != conversation.id:
            # Sub-agent sessions inherit their root session's project.
            root = await asyncio.to_thread(
                conversation_store.get_conversation, conversation.root_conversation_id
            )
            project_id = root.project_id if root is not None else None
        if project_id is None:
            raise OmnigentError("Session has no project context", code=ErrorCode.NOT_FOUND)
        # Owner-scoped lookup: a shared-session recipient does not own the
        # project, so this is where context stays owner-private.
        project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
        if project is None:
            raise OmnigentError("Session has no project context", code=ErrorCode.NOT_FOUND)
        config = parse_context_config(project.config)
        if config is None:
            raise OmnigentError("Session has no project context", code=ErrorCode.NOT_FOUND)
        return ContextService(config), project, user_id

    async def _trace_tool(
        service: ContextService,
        session_id: str,
        tool: str,
        args: dict[str, Any],
        *,
        paths: list[str] | None = None,
        node_ids: list[str] | None = None,
    ) -> None:
        """Record a context tool call in the session's trace.

        :param service: The project's context service.
        :param session_id: Calling session.
        :param tool: Tool name, e.g. ``"context_read"``.
        :param args: The request arguments.
        :param paths: Files the call returned.
        :param node_ids: Code-graph nodes the call returned (capped at 200).
        """
        event: dict[str, Any] = {"kind": "tool", "tool": tool, "args": args}
        if paths is not None:
            event["paths"] = paths[:200]
        if node_ids is not None:
            event["node_ids"] = node_ids[:200]
        await asyncio.to_thread(append_trace, service, session_id, event)

    # ── Project-scoped (Context page) ───────────────────────────────

    @router.get("/projects/{project_id}/context")
    async def get_context_status(request: Request, project_id: str) -> dict[str, Any]:
        """Return the project's context status, tree, and injected size.

        :param request: Incoming request.
        :param project_id: Project id.
        :returns: ``{"configured": False}`` when unset, otherwise the service
            status (paths, exists, git, graphify availability, tree, ...).
        """
        project, _ = await _owned_project(request, project_id)
        config = parse_context_config(project.config)
        if config is None:
            return {"configured": False, "project_id": project.id, "project_name": project.name}
        service = ContextService(config)
        status = await asyncio.to_thread(service.status)
        active = jobs.active(project.id)
        return {
            "project_id": project.id,
            "project_name": project.name,
            **status,
            "pending_proposals": await asyncio.to_thread(ProposalStore(service).count),
            "active_job": active.to_dict() if active is not None else None,
        }

    @router.post("/projects/{project_id}/context/init")
    async def init_context(request: Request, project_id: str) -> dict[str, Any]:
        """Scaffold the context directory layout (idempotent).

        :param request: Incoming request.
        :param project_id: Project id.
        :returns: ``{"created": [...]}`` plus the fresh status.
        """
        service = await _project_service(request, project_id)
        created = await asyncio.to_thread(service.init)
        status = await asyncio.to_thread(service.status)
        return {"created": created, **status}

    @router.get("/projects/{project_id}/context/files")
    async def read_context_file(
        request: Request, project_id: str, path: Annotated[str, Query()]
    ) -> dict[str, Any]:
        """Read one context file (content + sha).

        :param request: Incoming request.
        :param project_id: Project id.
        :param path: Relative path, e.g. ``wiki/a.md``.
        :returns: See :meth:`ContextService.read`.
        """
        service = await _project_service(request, project_id)
        return await asyncio.to_thread(service.read, path)

    @router.put("/projects/{project_id}/context/files")
    async def write_context_file(
        request: Request,
        project_id: str,
        path: Annotated[str, Query()],
        body: ContextFileWriteRequest,
    ) -> dict[str, Any]:
        """Save a context file with optimistic concurrency (409 on conflict).

        :param request: Incoming request.
        :param project_id: Project id.
        :param path: Relative path under ``system/`` or ``wiki/``.
        :param body: New content and the loaded sha.
        :returns: ``{"path", "sha", "size", "commit"}``.
        """
        service = await _project_service(request, project_id)
        return await asyncio.to_thread(service.write, path, body.content, body.base_sha)

    @router.post("/projects/{project_id}/context/files")
    async def create_context_file(
        request: Request, project_id: str, body: ContextFileCreateRequest
    ) -> dict[str, Any]:
        """Create a new markdown file under ``system/`` or ``wiki/``.

        :param request: Incoming request.
        :param project_id: Project id.
        :param body: Path and initial content.
        :returns: ``{"path", "sha", "size", "commit"}``.
        """
        service = await _project_service(request, project_id)
        return await asyncio.to_thread(service.create, body.path, body.content)

    @router.post("/projects/{project_id}/context/files/rename")
    async def rename_context_file(
        request: Request, project_id: str, body: ContextFileRenameRequest
    ) -> dict[str, Any]:
        """Rename a markdown file within ``system/`` / ``wiki/``.

        :param request: Incoming request.
        :param project_id: Project id.
        :param body: Source and destination paths.
        :returns: ``{"path", "old_path", "commit"}``.
        """
        service = await _project_service(request, project_id)
        return await asyncio.to_thread(service.rename, body.path, body.new_path)

    @router.delete("/projects/{project_id}/context/files")
    async def delete_context_file(
        request: Request, project_id: str, path: Annotated[str, Query()]
    ) -> dict[str, Any]:
        """Delete a markdown file under ``system/`` or ``wiki/``.

        :param request: Incoming request.
        :param project_id: Project id.
        :param path: Relative path.
        :returns: ``{"path", "deleted", "commit"}``.
        """
        service = await _project_service(request, project_id)
        return await asyncio.to_thread(service.delete, path)

    @router.get("/projects/{project_id}/context/search")
    async def search_context(
        request: Request,
        project_id: str,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Annotated[int, Query(ge=1, le=200)] = 20,
    ) -> dict[str, Any]:
        """Search markdown/text files.

        :param request: Incoming request.
        :param project_id: Project id.
        :param q: Query text.
        :param limit: Maximum hits.
        :returns: See :meth:`ContextService.search`.
        """
        service = await _project_service(request, project_id)
        return await asyncio.to_thread(service.search, q, limit)

    @router.get("/projects/{project_id}/context/preview")
    async def preview_injected_context(request: Request, project_id: str) -> dict[str, Any]:
        """Return the exact startup text sessions in this project receive.

        :param request: Incoming request.
        :param project_id: Project id.
        :returns: See :meth:`ContextService.injected_context`, plus
            ``skipped_by_harness``: profile files each harness loads natively
            and so does not receive here.
        """
        service = await _project_service(request, project_id)
        injected = await asyncio.to_thread(service.injected_context)
        skipped_by_harness = {
            harness: skipped
            for harness in sorted(NATIVELY_LOADED_PROFILE_FILES)
            if (skipped := service.natively_loaded_profile_files(harness))
        }
        return {**injected, "skipped_by_harness": skipped_by_harness}

    @router.get("/projects/{project_id}/context/graph")
    async def get_context_graph(
        request: Request,
        project_id: str,
        kind: Annotated[str, Query(pattern="^(knowledge|code)$")] = "knowledge",
        limit: Annotated[int, Query(ge=1, le=5000)] = 1500,
    ) -> dict[str, Any]:
        """Nodes and edges for the Graph view.

        :param request: Incoming request.
        :param project_id: Project id.
        :param kind: ``knowledge`` (markdown links) or ``code`` (graphify).
        :param limit: Node cap for the code graph.
        :returns: ``{"kind", "nodes", "edges", "truncated", ...}``.
        """
        service = await _project_service(request, project_id)
        if kind == "code":
            graph = await asyncio.to_thread(service.code_graph, limit)
        else:
            graph = await asyncio.to_thread(service.knowledge_graph)
        return {"kind": kind, **graph}

    @router.get("/projects/{project_id}/context/history")
    async def get_context_history(
        request: Request,
        project_id: str,
        limit: Annotated[int, Query(ge=1, le=500)] = 50,
    ) -> dict[str, Any]:
        """Git log of the context directory.

        :param request: Incoming request.
        :param project_id: Project id.
        :param limit: Maximum commits.
        :returns: ``{"versioned", "commits"}``.
        """
        service = await _project_service(request, project_id)
        return await asyncio.to_thread(service.history, limit)

    # ── Update context job + proposals ──────────────────────────────

    @router.post("/projects/{project_id}/context/update")
    async def start_context_update(
        request: Request,
        project_id: str,
        body: ContextUpdateRequest | None = None,
    ) -> dict[str, Any]:
        """Start the Update context job (one at a time per project).

        :param request: Incoming request.
        :param project_id: Project id.
        :param body: Which steps to run (all by default).
        :returns: The new job (poll ``jobs/{job_id}``).
        :raises OmnigentError: 409 when a job is already running.
        """
        project, user_id = await _owned_project(request, project_id)
        config = parse_context_config(project.config)
        if config is None:
            raise OmnigentError("Project has no context configured", code=ErrorCode.NOT_FOUND)
        options = body or ContextUpdateRequest()

        async def gather_sessions(since: int | None) -> list[CuratorInput]:
            """Transcripts of the owner's project sessions updated since ``since``.

            :param since: Unix time of the last update, or ``None``.
            :returns: One curator input per session with message text.
            """
            page = await asyncio.to_thread(
                conversation_store.list_conversations,
                limit=_MAX_CURATOR_SESSIONS,
                project=project.name,
                owned_by=user_id,
                updated_after=since,
                sort_by="updated_at",
                order="desc",
            )
            inputs: list[CuratorInput] = []
            for conversation in page.data:
                items = await asyncio.to_thread(
                    conversation_store.list_items, conversation.id, limit=400, order="desc"
                )
                text = _transcript_text(list(reversed(items.data)))
                if not text:
                    continue
                if len(text) > _MAX_TRANSCRIPT_CHARS:
                    # Keep the end of the session: conclusions come last.
                    text = "[... earlier messages omitted]\n" + text[-_MAX_TRANSCRIPT_CHARS:]
                inputs.append(
                    CuratorInput(
                        kind="session",
                        ref=f"session:{conversation.id}",
                        title=conversation.title or conversation.id,
                        text=text,
                    )
                )
            return inputs

        job = jobs.start(
            project.id,
            ContextService(config),
            gather_sessions,
            code_graph=options.code_graph,
            proposals=options.proposals,
        )
        return job.to_dict()

    @router.get("/projects/{project_id}/context/jobs/{job_id}")
    async def get_context_job(request: Request, project_id: str, job_id: str) -> dict[str, Any]:
        """Poll an Update context job.

        :param request: Incoming request.
        :param project_id: Project id.
        :param job_id: Job id.
        :returns: Job status, steps and log.
        """
        project, _ = await _owned_project(request, project_id)
        return jobs.get(project.id, job_id).to_dict()

    @router.get("/projects/{project_id}/context/proposals")
    async def list_context_proposals(request: Request, project_id: str) -> dict[str, Any]:
        """Pending proposals with current file content for diffing.

        :param request: Incoming request.
        :param project_id: Project id.
        :returns: ``{"proposals": [...]}``.
        """
        service = await _project_service(request, project_id)
        return {"proposals": await asyncio.to_thread(ProposalStore(service).list)}

    @router.post("/projects/{project_id}/context/proposals/{proposal_id}/apply")
    async def apply_context_proposal(
        request: Request, project_id: str, proposal_id: str
    ) -> dict[str, Any]:
        """Apply a proposal (write + commit) and remove it.

        :param request: Incoming request.
        :param project_id: Project id.
        :param proposal_id: Proposal id.
        :returns: The write result.
        :raises OmnigentError: 409 when the file changed since the proposal.
        """
        service = await _project_service(request, project_id)
        return await asyncio.to_thread(ProposalStore(service).apply, proposal_id)

    @router.post("/projects/{project_id}/context/proposals/{proposal_id}/reject")
    async def reject_context_proposal(
        request: Request, project_id: str, proposal_id: str
    ) -> dict[str, Any]:
        """Discard a proposal.

        :param request: Incoming request.
        :param project_id: Project id.
        :param proposal_id: Proposal id.
        :returns: ``{"id", "rejected": True}``.
        """
        service = await _project_service(request, project_id)
        await asyncio.to_thread(ProposalStore(service).delete, proposal_id)
        return {"id": proposal_id, "rejected": True}

    # ── Session-scoped (agent tools, session tab) ───────────────────

    @router.get("/sessions/{session_id}/context/list")
    async def session_context_list(request: Request, session_id: str) -> dict[str, Any]:
        """Index of ``system/`` and ``wiki/`` files for ``context_list``.

        :param request: Incoming request.
        :param session_id: Session id.
        :returns: ``{"project_id", "files", "has_code_graph"}``.
        """
        service, project, _ = await _session_service(request, session_id)
        index = await asyncio.to_thread(service.index)
        await _trace_tool(
            service, session_id, "context_list", {}, paths=[f["path"] for f in index["files"]]
        )
        return {"project_id": project.id, **index}

    @router.get("/sessions/{session_id}/context/files")
    async def session_context_read(
        request: Request,
        session_id: str,
        path: Annotated[str, Query()],
        offset: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=_MAX_READ_LINES)] = _DEFAULT_READ_LINES,
    ) -> dict[str, Any]:
        """Read a context file paged by lines for ``context_read``.

        :param request: Incoming request.
        :param session_id: Session id.
        :param path: Relative path.
        :param offset: First line (zero-based).
        :param limit: Maximum lines.
        :returns: ``{"path", "content", "offset", "lines", "total_lines",
            "truncated"}``.
        """
        service, project, _ = await _session_service(request, session_id)
        read = await asyncio.to_thread(service.read, path)
        if read["content"] is None:
            raise OmnigentError(f"{read['path']} is a binary file", code=ErrorCode.INVALID_INPUT)
        await _trace_tool(
            service,
            session_id,
            "context_read",
            {"path": path, "offset": offset, "limit": limit},
            paths=[read["path"]],
        )
        return {
            "project_id": project.id,
            "path": read["path"],
            **_slice_lines(read["content"], offset, limit),
        }

    @router.get("/sessions/{session_id}/context/search")
    async def session_context_search(
        request: Request,
        session_id: str,
        q: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Annotated[int, Query(ge=1, le=50)] = 10,
    ) -> dict[str, Any]:
        """Search context files for ``context_search``.

        :param request: Incoming request.
        :param session_id: Session id.
        :param q: Query text.
        :param limit: Maximum hits.
        :returns: See :meth:`ContextService.search`.
        """
        service, project, _ = await _session_service(request, session_id)
        result = await asyncio.to_thread(service.search, q, limit)
        await _trace_tool(
            service,
            session_id,
            "context_search",
            {"query": q, "limit": limit},
            paths=sorted({hit["path"] for hit in result["results"]}),
        )
        return {"project_id": project.id, **result}

    @router.get("/sessions/{session_id}/context/graph/query")
    async def session_graph_query(
        request: Request,
        session_id: str,
        q: Annotated[str, Query(min_length=1, max_length=1000)],
        budget: Annotated[int, Query(ge=100, le=8000)] = 2000,
    ) -> dict[str, Any]:
        """Query the code graph for ``graph_query``.

        :param request: Incoming request.
        :param session_id: Session id.
        :param q: Question text.
        :param budget: Approximate token budget.
        :returns: See :meth:`ContextService.graph_query`.
        """
        service, project, _ = await _session_service(request, session_id)
        result = await asyncio.to_thread(service.graph_query, q, budget)
        await _trace_tool(
            service,
            session_id,
            "graph_query",
            {"question": q, "budget": budget},
            node_ids=result["node_ids"],
        )
        return {"project_id": project.id, **result}

    @router.get("/sessions/{session_id}/context/graph/neighbors")
    async def session_graph_neighbors(
        request: Request,
        session_id: str,
        node: Annotated[str, Query(min_length=1, max_length=500)],
        depth: Annotated[int, Query(ge=1, le=2)] = 1,
    ) -> dict[str, Any]:
        """Neighbours of a code-graph node for ``graph_neighbors``.

        :param request: Incoming request.
        :param session_id: Session id.
        :param node: Node id or label.
        :param depth: 1 or 2 hops.
        :returns: See :meth:`ContextService.graph_neighbors`.
        """
        service, project, _ = await _session_service(request, session_id)
        result = await asyncio.to_thread(service.graph_neighbors, node, depth)
        resolved = result.get("node")
        await _trace_tool(
            service,
            session_id,
            "graph_neighbors",
            {"node": node, "depth": depth},
            node_ids=([resolved["id"]] if resolved else [])
            + [n["id"] for n in result["neighbors"]],
        )
        return {"project_id": project.id, **result}

    @router.get("/sessions/{session_id}/context/injected")
    async def session_injected_context(
        request: Request,
        session_id: str,
        harness: Annotated[str | None, Query(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Startup text for a session's harness launch.

        :param request: Incoming request.
        :param session_id: Session id.
        :param harness: Harness being launched, e.g. ``"claude-native"``; profile
            files it loads natively are left out.
        :returns: ``{"project_id", "text", "chars", "sha256", ...}``.
        """
        service, project, _ = await _session_service(request, session_id)
        injected = await asyncio.to_thread(service.injected_context, harness=harness)
        await asyncio.to_thread(
            append_trace,
            service,
            session_id,
            {
                "kind": "injected",
                "sha256": injected["sha256"],
                "chars": injected["chars"],
                "files": injected["files"],
                "skipped": injected["skipped"],
                "truncated": injected["truncated"],
            },
        )
        return {"project_id": project.id, **injected}

    @router.get("/sessions/{session_id}/context/trace")
    async def session_context_trace(request: Request, session_id: str) -> dict[str, Any]:
        """Injected context and recorded context tool calls for the session tab.

        Reading the trace is not itself traced.

        :param request: Incoming request.
        :param session_id: Session id.
        :returns: ``{"project_id", "project_name", "injected", "events"}`` where
            ``injected`` is the text sessions receive now and ``events`` the
            recorded injections and tool calls, oldest first.
        """
        service, project, _ = await _session_service(request, session_id)
        injected = await asyncio.to_thread(service.injected_context)
        events = await asyncio.to_thread(read_trace, service, session_id)
        return {
            "project_id": project.id,
            "project_name": project.name,
            "injected": injected,
            "events": events,
        }

    return router
