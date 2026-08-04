"""Carry the assistant's handoff summary into the staff queue.

Revision ID: 0004_agent_runtime
Revises: 0003_knowledge
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_agent_runtime"
down_revision: str | None = "0003_knowledge"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the summary the graph's handoff tool collects.

    Nullable, because every handoff written before this revision has none and
    inventing one would be worse than an empty field: a staff member reading
    "no summary" knows to open the transcript, while a fabricated one does not
    tell them that.
    """
    op.execute(
        """
        ALTER TABLE handoffs ADD COLUMN summary text;
        ALTER TABLE handoffs ADD CONSTRAINT ck_handoffs_summary_not_blank
            CHECK (summary IS NULL OR btrim(summary) <> '');
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE handoffs DROP CONSTRAINT ck_handoffs_summary_not_blank;
        ALTER TABLE handoffs DROP COLUMN summary;
        """
    )
