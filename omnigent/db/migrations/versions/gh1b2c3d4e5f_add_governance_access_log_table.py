"""add governance_access_log table

Revision ID: gh1b2c3d4e5f
Revises: gg1b2c3d4e5f
Create Date: 2026-09-11 00:00:00.000000

Adds the ``governance_access_log`` table: one append-only row per admin access
to the governance surface (the server-wide session list and the read-only
transcript view).

The governance endpoints list and open every user's sessions, bypassing the
per-session ACLs that scope the normal sidebar. That power is only acceptable
if using it is itself on the record, so each access appends one row here.
Nothing in the application updates or deletes a row once written.

``created_at_us`` is epoch MICROseconds, not the seconds most timestamps in
this schema carry — the extra precision is what keeps a burst of accesses
inside one second ordered. It therefore exceeds ``Integer`` range and is a
``BigInteger``.

The table is brand-new and created at the current schema state, so it carries
the tenant-partition ``workspace_id`` column as the leading primary-key member
(matching every other table after ``r1a2b3c4d5e6``). There are no foreign-key
constraints (schema Rule R032 — see ``p1a2b3c4d5e6``): in particular
``target_conversation_id`` deliberately has none, so an audit row survives
deletion of the session whose access it records.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "gh1b2c3d4e5f"
down_revision: str | None = "gg1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``governance_access_log`` table."""
    op.create_table(
        "governance_access_log",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # UUID PK stored as 16 raw bytes (Uuid16 → BINARY(16) on MySQL, BLOB/BYTEA
        # elsewhere).
        sa.Column("id", Uuid16(), nullable=False),
        # NULL in single-user mode, where no identity is authenticated.
        sa.Column("user_id", sa.String(128), nullable=True),
        # Action as a stable int code (see omnigent.db.enum_codecs
        # GOVERNANCE_ACCESS_ACTION: list=1, read=2).
        sa.Column("action", sa.SmallInteger(), nullable=False),
        # Relates to conversations.id; no FK (Rule R032) so the log outlives
        # the session it refers to.
        sa.Column("target_conversation_id", Uuid16(), nullable=True),
        # Opaque JSON blob of safe filter metadata, stored compressed
        # (CompressedText → LargeBinary).
        sa.Column("filters_json", sa.LargeBinary(), nullable=True),
        # Epoch MICROseconds — exceeds Integer range.
        sa.Column("created_at_us", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("action IN (1, 2)", name="ck_governance_access_log_action"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    # The only read path: newest-first paging of the log.
    op.create_index(
        "ix_governance_access_log_created_at_us",
        "governance_access_log",
        ["workspace_id", "created_at_us", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``governance_access_log`` table."""
    op.drop_index(
        "ix_governance_access_log_created_at_us",
        table_name="governance_access_log",
    )
    op.drop_table("governance_access_log")
