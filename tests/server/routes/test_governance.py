"""Tests for the admin-gated governance routes.

The governance surface lists *every* user's sessions, so these tests run
against an auth-enabled app (header identity + a real permission store)
rather than the shared single-user ``client`` fixture from
``tests/server/conftest.py``. The module-local ``client`` fixture shadows
that one so the non-admin case exercises a real authenticated identity
that simply lacks the admin flag.

Fixture wiring mirrors ``auth_app`` / ``auth_client`` in
``tests/server/integration/test_sessions_permissions.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlConversation
from omnigent.db.utils import get_or_create_engine
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.governance_audit_store.sqlalchemy_store import (
    SqlAlchemyGovernanceAuditStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import ControllableMockClient
from tests.server.routes.test_imports import _seed_claude_agent

ADMIN_EMAIL = "admin@test.com"
MEMBER_EMAIL = "member@test.com"


def _set_updated_at(db_uri: str, conversation_id: str, value: int) -> None:
    """Force a conversation's ``updated_at`` to an exact epoch value.

    The store has no setter — ``updated_at`` is bumped implicitly on
    append — so ordering-sensitive tests write the column directly rather
    than adding production API only tests would call. Mirrors the helper
    in ``tests/stores/test_conversation_store_governance.py``.

    :param db_uri: SQLAlchemy URI of the database holding the row.
    :param conversation_id: Conversation whose timestamp to set.
    :param value: Unix epoch seconds to write into ``updated_at``.
    """
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.execute(
            update(SqlConversation)
            .where(SqlConversation.id == conversation_id)
            .values(updated_at=value)
        )
        session.commit()


def _build_app(
    db_uri: str,
    tmp_path: Path,
    *,
    permission_store: SqlAlchemyPermissionStore | None,
    auth_provider: UnifiedAuthProvider | None,
) -> FastAPI:
    """Build an app whose only interesting axis is its auth wiring."""
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        permission_store=permission_store,
        auth_provider=auth_provider,
    )


@pytest.fixture()
def governance_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """An auth-enabled app so the admin gate has something to decide on.

    Strict header mode (``local_single_user=False``) is the deployed
    multi-user posture the governance page targets: a headerless request
    is a 401, and identity comes from ``X-Forwarded-Email``.
    """
    return _build_app(
        db_uri,
        tmp_path,
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )


@pytest_asyncio.fixture()
async def _governance_transport(
    governance_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.ASGITransport]:
    """Share one harness-process-manager lifecycle across both clients."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)
    yield httpx.ASGITransport(app=governance_app)
    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


@pytest_asyncio.fixture()
async def client(
    _governance_transport: httpx.ASGITransport, db_uri: str
) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in *non-admin*. Shadows the single-user fixture in conftest."""
    SqlAlchemyPermissionStore(db_uri).ensure_user(MEMBER_EMAIL)
    async with httpx.AsyncClient(
        transport=_governance_transport,
        base_url="http://test",
        headers={"X-Forwarded-Email": MEMBER_EMAIL},
    ) as c:
        yield c


@pytest_asyncio.fixture()
async def admin_client(
    _governance_transport: httpx.ASGITransport, db_uri: str
) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in admin — the only identity the governance routes serve."""
    SqlAlchemyPermissionStore(db_uri).ensure_user(ADMIN_EMAIL, is_admin=True)
    async with httpx.AsyncClient(
        transport=_governance_transport,
        base_url="http://test",
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
    ) as c:
        yield c


async def test_non_admin_is_forbidden_and_nothing_leaks(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """A non-admin gets 403 and no session data."""
    SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=None, title="secret")

    response = await client.get("/v1/governance/sessions")

    assert response.status_code == 403
    # Structural, not a substring scan: the denial message legitimately
    # contains the word "data", but the body must carry no listing payload.
    assert "data" not in response.json()
    assert "secret" not in response.text


async def test_unauthenticated_request_is_rejected(
    _governance_transport: httpx.ASGITransport,
) -> None:
    """No identity at all is a 401, never an anonymous full listing."""
    async with httpx.AsyncClient(
        transport=_governance_transport, base_url="http://test"
    ) as anonymous:
        response = await anonymous.get("/v1/governance/sessions")

    assert response.status_code == 401


async def test_authenticated_server_without_a_permission_store_fails_closed(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """No permission store plus auth configured is a 403, never a free pass.

    This is the deliberate divergence from the policy admin gate, which
    returns the caller's id when the store is missing. Governance lists
    every user's sessions, so "cannot check admin" must mean "deny" —
    reverting to the permissive fall-through has to fail a test.
    """
    app = _build_app(
        db_uri,
        tmp_path,
        permission_store=None,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=False),
    )
    SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=None, title="unguarded")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-Forwarded-Email": ADMIN_EMAIL},
    ) as c:
        response = await c.get("/v1/governance/sessions")

    assert response.status_code == 403
    assert "unguarded" not in response.text


async def test_single_user_mode_records_no_human_actor(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """The reserved ``local`` sentinel is not a person, so it is not logged.

    With no auth provider there is no identity to attribute, and
    ``attribution_user`` maps the sentinel to ``None`` — the audit log must
    not name ``local`` as the admin who looked.
    """
    app = _build_app(db_uri, tmp_path, permission_store=None, auth_provider=None)
    SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=None, title="solo")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.get("/v1/governance/sessions")

    assert response.status_code == 200
    entries = SqlAlchemyGovernanceAuditStore(db_uri).list_recent(limit=10)
    assert [e.user_id for e in entries] == [None]


async def test_local_operator_reaches_governance_in_the_default_deployment(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """A bare ``omnigent server`` must not lock its own operator out.

    The default local posture wires a real auth provider *and* a real
    permission store, and resolves a headerless request to the reserved
    ``local`` sentinel. That reaches the surface with no special case in
    the gate, because migration ``d4e5f6a7b8c9`` seeds ``local`` as an
    admin on every database — the ordinary admin check simply passes.
    This pins that, so a future change to the seed (or to the gate) that
    would make the default deployment 403 forever fails here instead of
    in someone's terminal. The audit row still lands, with the sentinel
    kept out of the actor field.
    """
    app = _build_app(
        db_uri,
        tmp_path,
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=True),
    )
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=None, title="mine")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.get("/v1/governance/sessions?limit=50")

    assert response.status_code == 200
    assert conv.id in {row["id"] for row in response.json()["data"]}
    entries = SqlAlchemyGovernanceAuditStore(db_uri).list_recent(limit=10)
    assert [(e.action, e.user_id) for e in entries] == [("list", None)]


async def test_demoting_the_local_sentinel_is_honoured(
    runtime_init: None, db_uri: str, tmp_path: Path
) -> None:
    """An operator who revokes the seeded local admin is obeyed.

    The sentinel reaches governance through the ordinary admin flag, not
    a carve-out, so clearing that flag — hardening a shared machine, or a
    multi-user deploy whose database was migrated up from a single-user
    one — actually locks the surface. A special case for ``local`` in the
    gate would silently override this.
    """
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.set_admin("local", False)
    app = _build_app(
        db_uri,
        tmp_path,
        permission_store=permissions,
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=True),
    )
    SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=None, title="locked")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.get("/v1/governance/sessions")

    assert response.status_code == 403
    assert "locked" not in response.text


async def test_local_sentinel_cannot_be_asserted_by_a_client(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A client cannot claim the seeded ``local`` admin identity.

    Every auth source rejects a client-supplied reserved name, so the
    sentinel's admin flag is only ever reachable by the local operator's
    own headerless request — not by anyone who can send headers.
    """
    response = await admin_client.get(
        "/v1/governance/sessions", headers={"X-Forwarded-Email": "local"}
    )

    assert response.status_code == 401


async def test_admin_lists_sessions_across_owners(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """The listing spans owners, unlike GET /v1/sessions."""
    store = SqlAlchemyConversationStore(db_uri)
    _seed_claude_agent(db_uri)
    alice = store.create_conversation(agent_id=None, title="alice session", workspace="/repos/a")
    bob = store.create_conversation(agent_id=None, title="bob session", workspace="/repos/b")
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user, conversation in (("alice@test.com", alice), ("bob@test.com", bob)):
        permissions.ensure_user(user)
        permissions.grant(user, conversation.id, 4)

    response = await admin_client.get("/v1/governance/sessions?limit=50")

    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["data"]}
    assert {alice.id, bob.id} <= set(rows)
    # The owner column is the point of the surface: an admin must be able
    # to see *whose* session each row is, not just that it exists.
    assert rows[alice.id]["owner"] == "alice@test.com"
    assert rows[bob.id]["owner"] == "bob@test.com"


async def test_source_badge_distinguishes_imported_from_live(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Imported sessions carry their provenance; others read as live."""
    _seed_claude_agent(db_uri)
    created = await admin_client.post(
        "/v1/imports",
        json={
            "source": "claude",
            "external_session_id": "gov-1",
            "workspace": "/repo",
            "items": [
                {
                    "type": "message",
                    "response_id": "claude:turn-1",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                }
            ],
        },
    )
    assert created.status_code == 201
    store = SqlAlchemyConversationStore(db_uri)
    live = store.create_conversation(agent_id=None, title="live one")

    response = await admin_client.get("/v1/governance/sessions?limit=50")
    by_id = {row["id"]: row for row in response.json()["data"]}

    assert by_id[created.json()["session_id"]]["source"] == "import:claude"
    assert by_id[created.json()["session_id"]]["external_session_id"] == "gov-1"
    assert by_id[created.json()["session_id"]]["item_count"] == 1
    assert by_id[live.id]["source"] == "live"
    assert by_id[live.id]["external_session_id"] is None


async def test_source_filter_selects_one_provenance(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """``source=import:claude`` narrows to imported rows; ``source=live`` excludes them."""
    _seed_claude_agent(db_uri)
    created = await admin_client.post(
        "/v1/imports",
        json={
            "source": "claude",
            "external_session_id": "gov-2",
            "items": [
                {
                    "type": "message",
                    "response_id": "claude:turn-1",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                }
            ],
        },
    )
    imported_id = created.json()["session_id"]
    live = SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=None, title="live two")

    imports_only = await admin_client.get("/v1/governance/sessions?source=import:claude&limit=50")
    live_only = await admin_client.get("/v1/governance/sessions?source=live&limit=50")

    assert [row["id"] for row in imports_only.json()["data"]] == [imported_id]
    assert [row["id"] for row in live_only.json()["data"]] == [live.id]


async def test_unknown_source_is_rejected(admin_client: httpx.AsyncClient) -> None:
    """A typo must not read as "no sessions match" on an audit surface."""
    response = await admin_client.get("/v1/governance/sessions?source=import:gemini")

    assert response.status_code == 400
    assert "import:gemini" in response.text


async def test_page_emptied_by_the_source_filter_still_pages_forward(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """A fully post-filtered page keeps its cursor so later matches surface.

    ``source=live`` cannot be pushed into SQL (it is the *absence* of the
    import label), so the store fills a page and this route trims it. When
    the trim removes everything, the response must still carry the cursor
    to resume from — otherwise the caller sees ``has_more`` with nothing to
    page on, stops, and silently misses every live session below.
    """
    _seed_claude_agent(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    live_ids = [store.create_conversation(agent_id=None, title=f"live {i}").id for i in range(2)]
    imported_ids = []
    for index in range(3):
        created = await admin_client.post(
            "/v1/imports",
            json={
                "source": "claude",
                "external_session_id": f"page-{index}",
                "items": [
                    {
                        "type": "message",
                        "response_id": "claude:turn-1",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "hi"}],
                        },
                    }
                ],
            },
        )
        assert created.status_code == 201
        imported_ids.append(created.json()["session_id"])
    # Pin the ordering: the 3 imports must occupy the whole first page so
    # the source trim empties it. Same-second creation would otherwise tie.
    for offset, conversation_id in enumerate(live_ids):
        _set_updated_at(db_uri, conversation_id, 1_000 + offset)
    for offset, conversation_id in enumerate(imported_ids):
        _set_updated_at(db_uri, conversation_id, 5_000 + offset)

    first = await admin_client.get("/v1/governance/sessions?source=live&limit=3")
    body = first.json()

    assert body["data"] == []
    assert body["has_more"] is True
    assert body["last_id"] is not None, "an emptied page must still expose its cursor"

    # Following that cursor must reach the live rows the first page hid.
    seen: set[str] = set()
    cursor = body["last_id"]
    for _ in range(5):
        page = (
            await admin_client.get(f"/v1/governance/sessions?source=live&limit=3&after={cursor}")
        ).json()
        seen.update(row["id"] for row in page["data"])
        if not page["has_more"]:
            break
        cursor = page["last_id"]
    assert set(live_ids) <= seen


async def test_workspace_prefix_filter(admin_client: httpx.AsyncClient, db_uri: str) -> None:
    """The workspace filter narrows to one subtree."""
    store = SqlAlchemyConversationStore(db_uri)
    inside = store.create_conversation(agent_id=None, title="in", workspace="/repos/alpha/svc")
    store.create_conversation(agent_id=None, title="out", workspace="/repos/beta")

    response = await admin_client.get(
        "/v1/governance/sessions?workspace_prefix=/repos/alpha&limit=50"
    )

    assert [row["id"] for row in response.json()["data"]] == [inside.id]


async def test_blank_filters_do_not_empty_the_listing(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Cleared filter inputs widen the listing rather than matching nothing.

    The UI sends ``?workspace_prefix=&search_query=`` when its boxes are
    cleared; an empty value must read as "filter disabled".
    """
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=None, title="kept")

    response = await admin_client.get(
        "/v1/governance/sessions?workspace_prefix=&search_query=&owner=&source=&limit=50"
    )

    assert response.status_code == 200
    assert conv.id in {row["id"] for row in response.json()["data"]}


async def test_owner_filter_narrows_to_named_users(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """``owner`` is a union over the named users, not an intersection."""
    store = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    owned = {}
    for user in ("alice@test.com", "bob@test.com", "carol@test.com"):
        conv = store.create_conversation(agent_id=None, title=f"{user} session")
        permissions.ensure_user(user)
        permissions.grant(user, conv.id, 4)
        owned[user] = conv.id

    response = await admin_client.get(
        "/v1/governance/sessions?owner=alice@test.com&owner=bob@test.com&limit=50"
    )

    ids = {row["id"] for row in response.json()["data"]}
    assert ids == {owned["alice@test.com"], owned["bob@test.com"]}


async def test_unpriced_session_reports_no_cost_rather_than_zero(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """``null`` (never priced) and ``0.0`` (priced at zero) stay distinct."""
    store = SqlAlchemyConversationStore(db_uri)
    unpriced = store.create_conversation(agent_id=None, title="unpriced")
    free = store.create_conversation(agent_id=None, title="free")
    store.set_session_usage(free.id, {"input_tokens": 5, "total_cost_usd": 0.0})

    response = await admin_client.get("/v1/governance/sessions?limit=50")
    by_id = {row["id"]: row for row in response.json()["data"]}

    assert by_id[unpriced.id]["total_cost_usd"] is None
    assert by_id[free.id]["total_cost_usd"] == 0.0


async def test_updated_bounds_filter_and_zero_is_a_real_bound(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """The date bounds narrow the window in both directions.

    ``updated_after=0`` is asserted only to show it is accepted and
    returns everything. It cannot distinguish "bound at the epoch" from
    "filter disabled" — both answers are identical here — so that branch
    does not pin the store's ``is not None`` gating. The ``± 3600``
    assertions are what prove the bounds are actually applied.
    """
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(agent_id=None, title="bounded")
    updated_at = store.get_conversation(conv.id).updated_at

    from_epoch = await admin_client.get("/v1/governance/sessions?updated_after=0&limit=50")
    future = await admin_client.get(
        f"/v1/governance/sessions?updated_after={updated_at + 3600}&limit=50"
    )
    past = await admin_client.get(
        f"/v1/governance/sessions?updated_before={updated_at - 3600}&limit=50"
    )

    assert conv.id in {row["id"] for row in from_epoch.json()["data"]}
    assert future.json()["data"] == []
    assert past.json()["data"] == []


async def test_every_access_writes_one_audit_row(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """List and read are both recorded, with filters captured on list."""
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(agent_id=None, title="watched")

    await admin_client.get("/v1/governance/sessions?workspace_prefix=/repos/alpha&limit=50")
    await admin_client.get(f"/v1/governance/sessions/{conv.id}")

    entries = SqlAlchemyGovernanceAuditStore(db_uri).list_recent(limit=10)
    assert [e.action for e in entries] == ["read", "list"]
    assert entries[0].target_conversation_id == conv.id
    assert entries[0].user_id == ADMIN_EMAIL
    assert "/repos/alpha" in (entries[1].filters_json or "")


async def test_forbidden_access_is_not_audited(client: httpx.AsyncClient, db_uri: str) -> None:
    """A rejected request served no data, so it writes no access row."""
    await client.get("/v1/governance/sessions")

    assert SqlAlchemyGovernanceAuditStore(db_uri).list_recent(limit=10) == []


async def test_detail_returns_the_same_shape_as_a_list_row(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """The detail endpoint answers with the row the list would have shown."""
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(
        agent_id=None, title="detail", workspace="/repos/gamma"
    )

    detail = await admin_client.get(f"/v1/governance/sessions/{conv.id}")
    listing = await admin_client.get("/v1/governance/sessions?limit=50")

    assert detail.status_code == 200
    row = next(r for r in listing.json()["data"] if r["id"] == conv.id)
    assert detail.json() == row


async def test_detail_404_for_unknown_session(admin_client: httpx.AsyncClient) -> None:
    """An unknown id is a 404, not a 500."""
    response = await admin_client.get("/v1/governance/sessions/conv_nope")

    assert response.status_code == 404


async def test_admin_can_open_the_transcript_of_a_session_they_do_not_own(
    admin_client: httpx.AsyncClient, db_uri: str
) -> None:
    """Governance rows are openable: the existing items route admits admins.

    The governance surface deliberately serves no transcript of its own —
    it links to ``GET /v1/sessions/{id}/items``. That only works because
    ``check_session_access`` short-circuits on the admin flag before any
    grant lookup, so this pins the cross-endpoint contract.
    """
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(agent_id=None, title="someone else's")
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("owner@test.com")
    permissions.grant("owner@test.com", conv.id, 4)

    response = await admin_client.get(f"/v1/sessions/{conv.id}/items")

    assert response.status_code == 200


async def test_non_admin_cannot_read_a_detail_row(client: httpx.AsyncClient, db_uri: str) -> None:
    """The detail route is gated too, not just the listing."""
    conv = SqlAlchemyConversationStore(db_uri).create_conversation(agent_id=None, title="private")

    response = await client.get(f"/v1/governance/sessions/{conv.id}")

    assert response.status_code == 403
    assert "private" not in response.text
