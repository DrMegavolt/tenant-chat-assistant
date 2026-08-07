"""Staff ownership of the handoff queue: release and resolution columns.

Revision ID: 0018_handoff_ownership
Revises: 0017_source_generation_trace
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0018_handoff_ownership"
down_revision: str | None = "0017_source_generation_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the fields a staff takeover lifecycle writes.

    ``released_at`` marks the handoff being released back to the queue: a
    release clears the assignment (so the schema's existing assignment
    consistency checks keep holding) and records when it happened. Its check
    constraint forbids a release stamp on a handoff that is still in a staff
    member's hands, so a `requested`/`assigned` row can never read as released.

    ``resolved_by_principal_id`` pins who closed the conversation, the
    accountability half of a resolution. Its constraint ties it to the resolved
    status, exactly like ``resolved_at`` already is.
    """
    op.execute(
        """
        ALTER TABLE handoffs
            ADD COLUMN released_at timestamptz,
            ADD COLUMN resolved_by_principal_id varchar(200);

        -- A release only exists once the handoff has been released back to the
        -- queue (or has since moved to a terminal state).
        ALTER TABLE handoffs ADD CONSTRAINT ck_handoffs_released_state
            CHECK (released_at IS NULL OR status IN ('queued', 'resolved', 'cancelled'));

        -- Who closed the conversation, recorded only on the closed row.
        ALTER TABLE handoffs ADD CONSTRAINT ck_handoffs_resolution_actor
            CHECK (resolved_by_principal_id IS NULL OR status = 'resolved');
        """
    )


def downgrade() -> None:
    """Drop the ownership columns; resolution history is preserved in audit rows."""
    op.execute(
        """
        ALTER TABLE handoffs DROP CONSTRAINT ck_handoffs_resolution_actor;
        ALTER TABLE handoffs DROP CONSTRAINT ck_handoffs_released_state;

        ALTER TABLE handoffs
            DROP COLUMN resolved_by_principal_id,
            DROP COLUMN released_at;
        """
    )
