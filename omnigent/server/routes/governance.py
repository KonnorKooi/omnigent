"""Admin-only governance routes: every session on the server, audited.

The normal session list is scoped to what the caller may see. This
surface deliberately is not — it lists every user's sessions so an
administrator can audit what agents did, for whom, and in which repo.
That power is why the gate here fails closed and why each access appends
a row to the governance audit log.
"""

from __future__ import annotations

import asyncio
import json
import typing
from typing import Annotated

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import attribution_user, require_user
from omnigent.session_import import IMPORT_SOURCE_LABEL_KEY, ImportSource
from omnigent.stores import ConversationStore
from omnigent.stores.governance_audit_store import GovernanceAuditStore
from omnigent.stores.permission_store import PermissionStore

# Marks a row whose provenance is this server rather than an imported
# transcript. Not a label value — it is the *absence* of the import label.
SOURCE_LIVE = "live"

_IMPORT_SOURCE_PREFIX = "import:"
# Tracks the import sources the server actually supports, so a typo in the
# query string is a 400 rather than a silently empty audit result.
_VALID_SOURCES = frozenset(
    {SOURCE_LIVE, *(f"{_IMPORT_SOURCE_PREFIX}{s}" for s in typing.get_args(ImportSource))}
)

_MAX_PAGE_SIZE = 200


class GovernanceSessionItem(BaseModel):
    """One session as the governance table renders it.

    :param total_cost_usd: ``None`` means the session was never priced,
        which is a different answer from a genuine ``0.0`` — the UI shows
        an em dash for the former.
    """

    id: str
    title: str | None = None
    source: str
    external_session_id: str | None = None
    owner: str | None = None
    agent_id: str | None = None
    workspace: str | None = None
    git_branch: str | None = None
    item_count: int
    total_cost_usd: float | None = None
    created_at: int
    updated_at: int


class GovernanceSessionList(BaseModel):
    """One cursor-paged slice of the server-wide session list."""

    data: list[GovernanceSessionItem]
    first_id: str | None = None
    last_id: str | None = None
    has_more: bool = False


def _source_of(conversation: Conversation) -> str:
    """Derive a row's provenance badge from its import label.

    :param conversation: The session to classify.
    :returns: ``"live"``, or ``"import:<source>"`` when the session
        carries an import provenance label.
    """
    imported = conversation.labels.get(IMPORT_SOURCE_LABEL_KEY)
    return f"{_IMPORT_SOURCE_PREFIX}{imported}" if imported else SOURCE_LIVE


def _total_cost_usd(conversation: Conversation) -> float | None:
    """Read the session's cost, treating an unpriced session as ``None``.

    Absence of the key is what distinguishes "never priced" from a priced
    ``$0.00``, so a missing or unreadable value stays ``None`` rather than
    collapsing to zero.

    :param conversation: The session whose usage to read.
    :returns: The recorded cost, or ``None`` when never priced.
    """
    value = conversation.session_usage.get("total_cost_usd")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _validate_sources(sources: list[str]) -> None:
    """Reject provenance values the server cannot answer for.

    An unknown value would otherwise return an empty page that reads like
    "no such sessions" — a dangerous answer on an audit surface.

    :param sources: Requested provenance values, already de-duplicated.
    :raises OmnigentError: 400 naming the offending value(s).
    """
    unknown = sorted(set(sources) - _VALID_SOURCES)
    if unknown:
        raise OmnigentError(
            f"Unknown source filter(s): {', '.join(unknown)}. "
            f"Expected one of: {', '.join(sorted(_VALID_SOURCES))}",
            code=ErrorCode.INVALID_INPUT,
        )


def _label_filters_for(sources: list[str]) -> dict[str, str] | None:
    """Push a source filter into the store query when it can be expressed.

    Only a single import source maps onto a label filter. ``live`` is the
    *absence* of the import label, and label filters AND together, so a
    request naming several sources cannot be answered in SQL — those fall
    back to the in-Python pass in :func:`_matches_sources`.

    :param sources: Requested provenance values, already de-duplicated.
    :returns: The label filter to push into the query, or ``None`` when
        the set has to be applied in Python instead.
    """
    if len(sources) != 1 or sources[0] == SOURCE_LIVE:
        return None
    return {IMPORT_SOURCE_LABEL_KEY: sources[0].removeprefix(_IMPORT_SOURCE_PREFIX)}


def _matches_sources(conversation: Conversation, sources: list[str]) -> bool:
    """Whether a row satisfies the requested provenance set.

    :param conversation: The candidate row.
    :param sources: Requested provenance values; empty means "any".
    :returns: ``True`` when the row should be kept.
    """
    return not sources or _source_of(conversation) in sources


async def _require_admin(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> str | None:
    """Return the acting admin's id, or fail the request.

    Governance lists every user's sessions, so this gate fails closed:
    unlike the policy admin gate, a missing permission store does not wave
    the request through when authentication is configured.

    :param request: The incoming request, carrying the caller's identity.
    :param auth_provider: The auth provider, or ``None`` when auth is off.
    :param permission_store: The permission store, or ``None``.
    :returns: The acting admin's id, or ``None`` when auth is disabled.
    :raises OmnigentError: 401 unauthenticated, 403 not an admin.
    """
    user_id = require_user(request, auth_provider)
    if permission_store is None:
        if auth_provider is not None:
            raise OmnigentError(
                "Governance requires a permission store",
                code=ErrorCode.FORBIDDEN,
            )
        return None
    if user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    if not await asyncio.to_thread(permission_store.is_admin, user_id):
        raise OmnigentError(
            "Admin privileges required to view governance data",
            code=ErrorCode.FORBIDDEN,
        )
    return user_id


def create_governance_router(
    conversation_store: ConversationStore,
    governance_audit_store: GovernanceAuditStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Create the admin-gated governance router.

    :param conversation_store: Store the listing reads sessions from.
    :param governance_audit_store: Append-only log every access records to.
    :param auth_provider: The auth provider, or ``None`` when auth is off.
    :param permission_store: Store the admin check consults, or ``None``.
    :returns: The router, to mount under ``/v1``.
    """
    router = APIRouter()

    async def build_items(conversations: list[Conversation]) -> list[GovernanceSessionItem]:
        """Decorate a page of conversations with owner and item count.

        Owners resolve in one bulk query — a per-row lookup would be N+1
        across the page.

        :param conversations: The page of sessions to render.
        :returns: One response item per conversation, in the same order.
        """
        ids = [c.id for c in conversations]
        if not ids:
            return []
        owners = await asyncio.to_thread(conversation_store.resolve_owners, ids)
        # One hop to the worker thread for the whole page rather than one
        # per row; the store exposes no bulk item count.
        counts = await asyncio.to_thread(
            lambda: {cid: conversation_store.count_items(cid) for cid in ids}
        )
        return [
            GovernanceSessionItem(
                id=c.id,
                title=c.title,
                source=_source_of(c),
                external_session_id=c.external_session_id,
                owner=owners.get(c.id),
                agent_id=c.agent_id,
                workspace=c.workspace,
                git_branch=c.git_branch,
                item_count=counts.get(c.id, 0),
                total_cost_usd=_total_cost_usd(c),
                created_at=c.created_at,
                updated_at=c.updated_at,
            )
            for c in conversations
        ]

    @router.get("/governance/sessions", response_model=GovernanceSessionList)
    async def list_governance_sessions(
        request: Request,
        # Repeatable (`?source=a&source=b`), so these take the Annotated
        # form — a bare `Query(...)` default on a list parameter is the
        # mutable-default footgun the linter rejects.
        source: Annotated[list[str] | None, Query()] = None,
        owner: Annotated[list[str] | None, Query()] = None,
        agent_id: str | None = Query(default=None),
        workspace_prefix: str | None = Query(default=None),
        # No `0` default: the store reads `is not None` as "bound set", and
        # 0 is a real epoch, so a cleared date box must arrive as None.
        updated_after: int | None = Query(default=None),
        updated_before: int | None = Query(default=None),
        search_query: str | None = Query(default=None),
        limit: int = Query(default=25, ge=1, le=_MAX_PAGE_SIZE),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
        sort_by: str = Query(default="updated_at", pattern="^(created_at|updated_at)$"),
    ) -> GovernanceSessionList:
        """List every user's top-level sessions, newest first.

        Sub-agent sessions are omitted — they are audited through the
        parent session that spawned them.

        Cursors track the store's page, not the rows left after the
        in-Python source trim. A page the trim empties completely must
        still hand back an id to resume from, or the caller sees
        ``has_more`` with nothing to page on and stops early — silently
        hiding later matches on a surface whose whole job is to hide
        nothing. Rows the trim dropped were evaluated and rejected, so
        resuming past them skips nothing.

        :returns: One cursor-paged slice of the server-wide list.
        """
        user_id = await _require_admin(request, auth_provider, permission_store)
        # Cleared inputs arrive as empty strings and mean "filter
        # disabled"; deduplicated so `?source=x&source=x` stays one value.
        sources = sorted({s for s in (source or []) if s})
        owners = sorted({o for o in (owner or []) if o})
        _validate_sources(sources)
        label_filters = _label_filters_for(sources)
        # A source set SQL cannot express is trimmed in Python below, so
        # the page may come back short — or empty — with `has_more` true.
        post_filter = bool(sources) and label_filters is None

        page = await asyncio.to_thread(
            conversation_store.list_conversations,
            limit=limit,
            after=after,
            before=before,
            # Sub-agents are audited through the parent session they run
            # under; listing them flat would bury it.
            kind="default",
            agent_id=agent_id,
            order=order,
            sort_by=sort_by,
            search_query=search_query or None,
            # An archived session is still evidence of what an agent did.
            include_archived=True,
            label_filters=label_filters,
            workspace_prefix=workspace_prefix or None,
            updated_after=updated_after,
            updated_before=updated_before,
            owned_by_any=owners or None,
        )
        rows = [c for c in page.data if not post_filter or _matches_sources(c, sources)]
        items = await build_items(rows)

        await asyncio.to_thread(
            governance_audit_store.record,
            attribution_user(user_id),
            "list",
            None,
            json.dumps(
                {
                    "source": sources,
                    "owner": owners,
                    "agent_id": agent_id,
                    "workspace_prefix": workspace_prefix or None,
                    "updated_after": updated_after,
                    "updated_before": updated_before,
                    "search_query": search_query or None,
                    "limit": limit,
                    "after": after,
                    "before": before,
                    "order": order,
                    "sort_by": sort_by,
                },
                sort_keys=True,
            ),
        )
        return GovernanceSessionList(
            data=items,
            # The store's page, not the trimmed rows — see the docstring.
            first_id=page.first_id,
            last_id=page.last_id,
            has_more=page.has_more,
        )

    @router.get("/governance/sessions/{session_id}", response_model=GovernanceSessionItem)
    async def get_governance_session(
        session_id: str,
        request: Request,
    ) -> GovernanceSessionItem:
        """Return one session's governance row, whoever owns it.

        :param session_id: The session to inspect, e.g. ``"conv_abc123"``.
        :returns: The same row the listing would show for this session.
        """
        user_id = await _require_admin(request, auth_provider, permission_store)
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)
        items = await build_items([conversation])
        await asyncio.to_thread(
            governance_audit_store.record,
            attribution_user(user_id),
            "read",
            session_id,
            None,
        )
        return items[0]

    return router
