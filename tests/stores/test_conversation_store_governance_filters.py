"""Filters and bulk lookups the governance surface adds to the conversation store.

The governance session list pages with cursors, so each of these filters has
to narrow the query itself rather than trim rows after the page is cut —
otherwise ``has_more`` describes a page the caller never sees. These tests pin
that the narrowing happens in the store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import MessageData
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


@pytest.fixture()
def omnigent_db(tmp_path: Path) -> Path:
    return tmp_path / "omnigent.db"


@pytest.fixture()
def conv_db(tmp_path: Path) -> Path:
    return tmp_path / "conversations.db"


@pytest.fixture()
def store(omnigent_db: Path, conv_db: Path) -> SqlAlchemyConversationStore:
    return SqlAlchemyConversationStore(
        f"sqlite:///{omnigent_db}",
        f"sqlite:///{conv_db}",
    )


@pytest.fixture()
def permissions(omnigent_db: Path) -> SqlAlchemyPermissionStore:
    return SqlAlchemyPermissionStore(f"sqlite:///{omnigent_db}")


# A well-formed id that was never created — ids are 16-byte uuids, so an
# unknown id still has to parse as one.
_UNKNOWN_ID = "f" * 32


def _ids(page) -> set[str]:
    return {c.id for c in page.data}


# ── label_filters ────────────────────────────────────────────


def test_label_filter_selects_only_matching_rows(store: SqlAlchemyConversationStore) -> None:
    """A label filter narrows to rows carrying that exact key/value."""
    imported = store.create_conversation(title="imported")
    live = store.create_conversation(title="live")
    store.set_labels(imported.id, {"omnigent.import_source": "claude"})

    page = store.list_conversations(label_filters={"omnigent.import_source": "claude"})

    assert _ids(page) == {imported.id}
    assert live.id not in _ids(page)


def test_label_filters_and_together(store: SqlAlchemyConversationStore) -> None:
    """Several pairs require every label, not any of them."""
    both = store.create_conversation(title="both")
    one = store.create_conversation(title="one")
    store.set_labels(both.id, {"a": "1", "b": "2"})
    store.set_labels(one.id, {"a": "1"})

    page = store.list_conversations(label_filters={"a": "1", "b": "2"})

    assert _ids(page) == {both.id}


def test_empty_label_filter_disables_it(store: SqlAlchemyConversationStore) -> None:
    """An empty mapping must not filter everything out."""
    c = store.create_conversation(title="x")

    assert _ids(store.list_conversations(label_filters={})) == {c.id}
    assert _ids(store.list_conversations(label_filters=None)) == {c.id}


# ── workspace_prefix ─────────────────────────────────────────


def test_workspace_prefix_matches_by_path_prefix(store: SqlAlchemyConversationStore) -> None:
    """Only sessions under the given directory come back."""
    inside = store.create_conversation(title="in", workspace="/home/alice/code/omnigent")
    sibling = store.create_conversation(title="sib", workspace="/home/alice/code")
    outside = store.create_conversation(title="out", workspace="/home/bob/code")

    page = store.list_conversations(workspace_prefix="/home/alice/code")

    assert _ids(page) == {inside.id, sibling.id}
    assert outside.id not in _ids(page)


def test_workspace_prefix_escapes_sql_wildcards(store: SqlAlchemyConversationStore) -> None:
    """A literal % or _ in the prefix matches literally, not as a wildcard."""
    literal = store.create_conversation(title="literal", workspace="/srv/a_b/project")
    decoy = store.create_conversation(title="decoy", workspace="/srv/axb/project")

    page = store.list_conversations(workspace_prefix="/srv/a_b")

    assert _ids(page) == {literal.id}
    assert decoy.id not in _ids(page)


def test_empty_workspace_prefix_disables_it(store: SqlAlchemyConversationStore) -> None:
    """An empty string means "no filter", not "match the empty path"."""
    c = store.create_conversation(title="x", workspace="/home/alice")

    assert _ids(store.list_conversations(workspace_prefix="")) == {c.id}


# ── updated_after / updated_before ───────────────────────────


def test_updated_bounds_are_inclusive(store: SqlAlchemyConversationStore) -> None:
    """Both bounds include a row sitting exactly on them."""
    c = store.create_conversation(title="x")
    at = store.get_conversation(c.id).updated_at

    assert _ids(store.list_conversations(updated_after=at)) == {c.id}
    assert _ids(store.list_conversations(updated_before=at)) == {c.id}
    assert _ids(store.list_conversations(updated_after=at + 1)) == set()
    assert _ids(store.list_conversations(updated_before=at - 1)) == set()


def test_zero_is_a_real_bound_not_unset(store: SqlAlchemyConversationStore) -> None:
    """``0`` is a real epoch, so it must act as a bound rather than read as off.

    A cleared date box has to send ``None``; if ``0`` were treated as unset,
    ``updated_before=0`` would return everything instead of nothing.
    """
    store.create_conversation(title="x")

    assert _ids(store.list_conversations(updated_before=0)) == set()


# ── owned_by_any ─────────────────────────────────────────────


def test_owned_by_any_unions_owners(
    store: SqlAlchemyConversationStore, permissions: SqlAlchemyPermissionStore
) -> None:
    """A session qualifies when ANY named user owns it."""
    a = store.create_conversation(title="a")
    b = store.create_conversation(title="b")
    c = store.create_conversation(title="c")
    permissions.grant("alice", a.id, LEVEL_OWNER)
    permissions.grant("bob", b.id, LEVEL_OWNER)
    permissions.grant("carol", c.id, LEVEL_OWNER)

    page = store.list_conversations(owned_by_any=["alice", "bob"])

    assert _ids(page) == {a.id, b.id}


def test_owned_by_any_ignores_non_owner_grants(
    store: SqlAlchemyConversationStore, permissions: SqlAlchemyPermissionStore
) -> None:
    """Being merely shared with a user is not owning it."""
    shared = store.create_conversation(title="shared")
    permissions.grant("alice", shared.id, LEVEL_READ)

    assert _ids(store.list_conversations(owned_by_any=["alice"])) == set()


def test_empty_owned_by_any_disables_it(store: SqlAlchemyConversationStore) -> None:
    """An empty list means "no owner filter", not "owned by nobody"."""
    c = store.create_conversation(title="x")

    assert _ids(store.list_conversations(owned_by_any=[])) == {c.id}
    assert _ids(store.list_conversations(owned_by_any=None)) == {c.id}


def test_filters_intersect_rather_than_widen(
    store: SqlAlchemyConversationStore, permissions: SqlAlchemyPermissionStore
) -> None:
    """Combining filters narrows: a row must satisfy every one of them."""
    match = store.create_conversation(title="match", workspace="/repo/x")
    wrong_workspace = store.create_conversation(title="ws", workspace="/other")
    permissions.grant("alice", match.id, LEVEL_OWNER)
    permissions.grant("alice", wrong_workspace.id, LEVEL_OWNER)

    page = store.list_conversations(owned_by_any=["alice"], workspace_prefix="/repo")

    assert _ids(page) == {match.id}


# ── bulk helpers ─────────────────────────────────────────────


def test_resolve_owners_returns_highest_grant_per_session(
    store: SqlAlchemyConversationStore, permissions: SqlAlchemyPermissionStore
) -> None:
    """The owner outranks lesser grants on the same session."""
    a = store.create_conversation(title="a")
    b = store.create_conversation(title="b")
    permissions.grant("reader", a.id, LEVEL_READ)
    permissions.grant("alice", a.id, LEVEL_OWNER)
    permissions.grant("bob", b.id, LEVEL_OWNER)

    assert store.resolve_owners([a.id, b.id]) == {a.id: "alice", b.id: "bob"}


def test_resolve_owners_omits_unknown_and_handles_empty(
    store: SqlAlchemyConversationStore,
) -> None:
    """Unknown ids are absent; an empty request is one empty answer."""
    assert store.resolve_owners([]) == {}
    assert store.resolve_owners([_UNKNOWN_ID]) == {}


def test_resolve_owners_matches_single_lookup(
    store: SqlAlchemyConversationStore, permissions: SqlAlchemyPermissionStore
) -> None:
    """The bulk form agrees with the per-session form it replaces."""
    a = store.create_conversation(title="a")
    permissions.grant("alice", a.id, LEVEL_OWNER)

    assert store.resolve_owners([a.id])[a.id] == store.get_session_owner(a.id)


def test_count_items_counts_appended_items(store: SqlAlchemyConversationStore) -> None:
    """The count tracks appends, and an empty or unknown session is 0."""
    c = store.create_conversation(title="x")
    assert store.count_items(c.id) == 0
    assert store.count_items(_UNKNOWN_ID) == 0

    store.append(
        c.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
            )
        ],
    )

    assert store.count_items(c.id) == 1
