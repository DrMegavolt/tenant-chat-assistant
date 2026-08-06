"""Quarantine safety state on knowledge versions (`RAG-007`).

Revision ID: 0013_quarantine
Revises: 0012_agent_routing
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0013_quarantine"
down_revision: str | None = "0012_agent_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the content-safety state the ingestion worker writes.

    Quarantine is a stored state decided asynchronously by the worker, never
    derivable from the rows an operator wrote, so it needs its own column and
    enum. ``clear`` is the default so existing rows are unaffected; the review
    queue index mirrors the one the worker's pending-scan uses.
    """
    op.execute("CREATE TYPE knowledge_safety_state AS ENUM ('clear', 'quarantined')")
    op.execute(
        """
        ALTER TABLE knowledge_document_versions
        ADD COLUMN safety_state knowledge_safety_state NOT NULL DEFAULT 'clear'
        """
    )
    op.execute(
        """
        CREATE INDEX ix_knowledge_versions_tenant_safety
        ON knowledge_document_versions (tenant_id, safety_state)
        """
    )


def downgrade() -> None:
    """Remove this revision. Production recovery should restore a backup instead."""
    op.execute("DROP INDEX ix_knowledge_versions_tenant_safety")
    op.execute("ALTER TABLE knowledge_document_versions DROP COLUMN safety_state")
    op.execute("DROP TYPE knowledge_safety_state")
