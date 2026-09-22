"""SQLAlchemy-backed governance audit store."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from omnigent.db.db_models import SqlGovernanceAccessLog, current_workspace_id
from omnigent.db.enum_codecs import (
    decode_governance_access_action,
    encode_governance_access_action,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
)
from omnigent.stores.governance_audit_store import (
    GovernanceAuditEntry,
    GovernanceAuditStore,
)

# Ceiling on one page of audit rows. The log grows without bound (append-only,
# one row per governance request), so a caller-influenced limit must not be
# able to pull the whole table into memory.
_MAX_LIST_LIMIT = 1000


def _to_entity(row: SqlGovernanceAccessLog) -> GovernanceAuditEntry:
    """Convert a :class:`SqlGovernanceAccessLog` ORM row to a domain entity.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`GovernanceAuditEntry` dataclass instance.
    """
    return GovernanceAuditEntry(
        id=row.id,
        user_id=row.user_id,
        action=decode_governance_access_action(row.action),
        target_conversation_id=row.target_conversation_id,
        filters_json=row.filters_json,
        created_at_us=row.created_at_us,
    )


class SqlAlchemyGovernanceAuditStore(GovernanceAuditStore):
    """SQLAlchemy-backed implementation of :class:`GovernanceAuditStore`.

    Persists governance access records in a relational database via
    SQLAlchemy ORM. Writes are inserts only — the class exposes no update
    or delete path.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the SQLAlchemy governance audit store.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.governance_audit_store",
        )

    def record(
        self,
        user_id: str | None,
        action: str,
        target_conversation_id: str | None,
        filters_json: str | None,
    ) -> None:
        """Append one access record. See base class for contract."""
        # Encode first: an unrecognised action must fail before anything is
        # written, so a rejected call leaves no partial row.
        code = encode_governance_access_action(action)
        row = SqlGovernanceAccessLog(
            id=uuid.uuid4().hex,
            user_id=user_id,
            action=code,
            target_conversation_id=target_conversation_id,
            filters_json=filters_json,
            created_at_us=now_epoch_us(),
        )
        with self._session("insert_governance_access") as session:
            session.add(row)

    def list_recent(self, limit: int = 100) -> list[GovernanceAuditEntry]:
        """Return the most recent access records, newest first. See base class."""
        # A non-positive limit is not "unbounded" here: SQLite reads
        # .limit(-1) as no limit, which would stream the whole table.
        if limit < 1:
            raise ValueError(f"limit must be a positive integer, got {limit!r}")
        # id is the tiebreaker only for the rare same-microsecond write;
        # created_at_us carries the real ordering.
        stmt = (
            select(SqlGovernanceAccessLog)
            .where(SqlGovernanceAccessLog.workspace_id == current_workspace_id())
            .order_by(
                SqlGovernanceAccessLog.created_at_us.desc(),
                SqlGovernanceAccessLog.id.desc(),
            )
            .limit(min(limit, _MAX_LIST_LIMIT))
        )
        with self._session("select_recent_governance_access") as session:
            rows = list(session.execute(stmt).scalars().all())
            return [_to_entity(r) for r in rows]
