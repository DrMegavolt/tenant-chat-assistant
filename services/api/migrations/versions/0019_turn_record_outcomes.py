"""Permit `refused` and `failed` turn outcomes in the turn-record envelope.

Revision ID: 0019_turn_record_outcomes
Revises: 0018_handoff_ownership
Create Date: 2026-08-10
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0019_turn_record_outcomes"
down_revision: str | None = "0018_handoff_ownership"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Admit every terminal status the graph records.

    ``0014_inference_trace`` froze the outcome vocabulary before the graph
    learned to record ``refused`` (a `RAG-007` whole-answer refusal) and
    ``failed`` (a crash `OBS-006` records itself with). A refused or failed
    turn therefore violated the CHECK at insert time, and the store's
    IntegrityError mapping turned that into a misleading "session absent or
    outside tenant" 404 after the model had already answered.
    """
    op.execute(
        """
        ALTER TABLE turn_records DROP CONSTRAINT ck_turn_records_outcome;

        ALTER TABLE turn_records ADD CONSTRAINT ck_turn_records_outcome CHECK
            (outcome IN ('answered', 'paused', 'escalated', 'abstained',
                         'clarified', 'refused', 'failed', 'unknown'));
        """
    )


def downgrade() -> None:
    """Restore the pre-`OBS-006` vocabulary; existing refused/failed rows fail."""
    op.execute(
        """
        ALTER TABLE turn_records DROP CONSTRAINT ck_turn_records_outcome;

        ALTER TABLE turn_records ADD CONSTRAINT ck_turn_records_outcome CHECK
            (outcome IN ('answered', 'paused', 'escalated', 'abstained',
                         'clarified', 'unknown'));
        """
    )
