"""Content-free index-generation dimension on turn records.

Revision ID: 0017_source_generation_trace
Revises: 0016_review_queue
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0017_source_generation_trace"
down_revision: str | None = "0016_review_queue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the query dimension `FEAT-001` links findings to related turns with.

    ``source_generation_ids`` names the ingestion index generations a turn's
    retrieval actually cited, so an index-integrity finding can answer "which
    turns were grounded in this generation?" without the query scanning the
    opaque content object. Generation identifiers are the bounded, content-free
    `OBS-004` component pins — never document text — so this is a query
    dimension exactly like ``diagnosis_causes`` (0014), and it lives under the
    same rule: a content leak cannot enter it because the value is an identifier
    the ingestion worker derived, not something a visitor or operator typed.
    """
    op.execute(
        """
        ALTER TABLE turn_records
            ADD COLUMN source_generation_ids uuid[] NOT NULL DEFAULT '{}';

        -- GIN serves the containment predicate the related-turns query uses;
        -- the tenant filter is served by the existing session and recorded-at
        -- indexes, as with the cause array.
        CREATE INDEX ix_turn_records_tenant_generations
            ON turn_records USING gin (source_generation_ids);
        """
    )


def downgrade() -> None:
    """Drop the query dimension; the content object is untouched."""
    op.execute(
        """
        DROP INDEX ix_turn_records_tenant_generations;

        ALTER TABLE turn_records
            DROP COLUMN source_generation_ids;
        """
    )
