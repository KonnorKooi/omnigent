"""Governance audit store — records admin access to the governance surface.

The governance endpoints list and open *every* user's sessions, bypassing
the per-session ACLs that scope the normal sidebar. That power is only
acceptable if using it is itself on the record, so each access appends one
row here. The log is append-only: nothing in the application updates or
deletes a row once written.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class GovernanceAuditEntry:
    """
    One recorded admin access to the governance surface.

    :param id: Opaque access-record id (a bare 32-char hex uuid).
    :param user_id: The acting admin, e.g. ``"alice@example.com"``.
        ``None`` in single-user mode, where no identity is authenticated.
    :param action: ``"list"`` or ``"read"``.
    :param target_conversation_id: The transcript opened by a ``"read"``,
        e.g. ``"conv_abc123"``; ``None`` for a ``"list"``.
    :param filters_json: Serialized query filters for a ``"list"``, showing
        which slice of sessions was examined; ``None`` for a ``"read"``.
    :param created_at_us: Unix epoch **microseconds** the access happened —
        microseconds, not the seconds most timestamps in this codebase
        carry. The extra precision is what keeps a burst of accesses inside
        one second ordered. Callers rendering this for humans or for an API
        must divide by 1_000_000 (or format explicitly); the ``_us`` suffix
        is there so that conversion is never forgotten.
    """

    id: str
    user_id: str | None
    action: str
    target_conversation_id: str | None
    filters_json: str | None
    created_at_us: int


class GovernanceAuditStore(ABC):
    """Abstract base for governance access-log persistence.

    Append-only. Implementations expose no update or delete: a record of
    who read whose session must not be editable by the same admins the log
    exists to hold accountable.
    """

    def __init__(self, storage_location: str) -> None:
        """Initialize the governance audit store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def record(
        self,
        user_id: str | None,
        action: str,
        target_conversation_id: str | None,
        filters_json: str | None,
    ) -> None:
        """Append one access record.

        Callers record the access *after* authorization passes, so the log
        reflects accesses that actually served data.

        :param user_id: The acting admin, e.g. ``"alice@example.com"``, or
            ``None`` in single-user mode.
        :param action: ``"list"`` or ``"read"``.
        :param target_conversation_id: The session opened by a ``"read"``,
            e.g. ``"conv_abc123"``; ``None`` for a ``"list"``.
        :param filters_json: Serialized query filters for a ``"list"``;
            ``None`` for a ``"read"``.
        :raises ValueError: If *action* is not ``"list"`` or ``"read"``.
        """
        ...

    @abstractmethod
    def list_recent(self, limit: int = 100) -> list[GovernanceAuditEntry]:
        """Return the most recent access records, newest first.

        :param limit: Maximum number of records to return. Must be
            positive; implementations may additionally cap it, since the
            log grows without bound.
        :returns: Up to *limit* :class:`GovernanceAuditEntry` objects,
            ordered newest first.
        :raises ValueError: If *limit* is not positive.
        """
        ...
