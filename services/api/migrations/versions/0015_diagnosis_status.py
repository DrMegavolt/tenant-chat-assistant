"""The diagnosis-status query dimension on the turn-record envelope.

Revision ID: 0015_diagnosis_status
Revises: 0014_inference_trace
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0015_diagnosis_status"
down_revision: str | None = "0014_inference_trace"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the diagnosis-status array `FEAT-015` filters the explorer on.

    The Gate B explorer filters on diagnosis *status* ("show me everything
    that is merely suspected") as well as cause, and status is part of the
    content-free projection: a bounded enum value, derived at write time from
    the diagnosis records, never text from the opaque content object. The
    array is unconstrained exactly like ``diagnosis_causes``: the status
    vocabulary is owned by the detector and grows by review, not by migration.
    """
    op.execute(
        """
        ALTER TABLE turn_records
            ADD COLUMN diagnosis_statuses varchar(64)[] NOT NULL DEFAULT '{}';

        CREATE INDEX ix_turn_records_tenant_statuses
            ON turn_records USING gin (diagnosis_statuses);
        """
    )


def downgrade() -> None:
    """Drop the query dimension; the content object is untouched."""
    op.execute(
        """
        DROP INDEX ix_turn_records_tenant_statuses;

        ALTER TABLE turn_records
            DROP COLUMN diagnosis_statuses;
        """
    )
