"""The `OBS-004` inference-trace query surface: content-free columns on the
turn-record envelope.

Revision ID: 0014_inference_trace
Revises: 0013_quarantine
Create Date: 2026-08-06
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0014_inference_trace"
down_revision: str | None = "0013_quarantine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the content-free projection `OBS-004` attributes failures with.

    ``turn_records.content`` stays the one home for prompt, evidence, and
    output (`PRIV-002`), and the schema still parses none of it. These columns
    are derived at write time and hold no content by construction — an outcome
    enum, a SHA-256 over versions, a bounded cause-code array, a turn ordinal,
    and a trace shape version — so the attribution queries an operator asks
    ("which manifest answered", "which causes fired") never need to scan the
    opaque object, and a content leak cannot enter a query dimension.

    ``trace_id`` is the `OBS-001` correlation id, which is how a trace query
    answers "everything this request did"; it existed on the envelope already,
    and gains the index that makes the lookup a seek instead of a scan.
    """
    op.execute(
        """
        ALTER TABLE turn_records
            ADD COLUMN outcome varchar(32) NOT NULL DEFAULT 'unknown',
            ADD COLUMN component_manifest_hash varchar(64) NOT NULL DEFAULT '',
            ADD COLUMN diagnosis_causes varchar(64)[] NOT NULL DEFAULT '{}',
            ADD COLUMN turn_index integer NOT NULL DEFAULT 0,
            ADD COLUMN trace_schema_version varchar(16) NOT NULL DEFAULT '1';

        ALTER TABLE turn_records
            ADD CONSTRAINT ck_turn_records_outcome CHECK
                (outcome IN ('answered', 'paused', 'escalated', 'abstained',
                             'clarified', 'unknown')),
            ADD CONSTRAINT ck_turn_records_trace_schema_version CHECK
                (btrim(trace_schema_version) <> ''),
            ADD CONSTRAINT ck_turn_records_turn_index_nonnegative CHECK
                (turn_index >= 0);

        -- The diagnosis-cause array is deliberately unconstrained: the Gate B
        -- cause set is owned by the detector (`OBS-004`) and grows by review,
        -- not by migration, and the array is a query dimension, not a column
        -- of record. GIN cannot take a tenant prefix (varchar has no GIN
        -- operator class), so the tenant filter is served by the session and
        -- recorded-at indexes and the array predicate by this one.
        CREATE INDEX ix_turn_records_tenant_manifest
            ON turn_records (tenant_id, component_manifest_hash);
        CREATE INDEX ix_turn_records_tenant_causes
            ON turn_records USING gin (diagnosis_causes);
        CREATE INDEX ix_turn_records_tenant_trace
            ON turn_records (tenant_id, trace_id);
        CREATE INDEX ix_turn_records_tenant_outcome
            ON turn_records (tenant_id, outcome, recorded_at DESC);
        """
    )


def downgrade() -> None:
    """Drop the query surface; the content object is untouched."""
    op.execute(
        """
        DROP INDEX ix_turn_records_tenant_outcome;
        DROP INDEX ix_turn_records_tenant_trace;
        DROP INDEX ix_turn_records_tenant_causes;
        DROP INDEX ix_turn_records_tenant_manifest;

        ALTER TABLE turn_records
            DROP CONSTRAINT ck_turn_records_turn_index_nonnegative,
            DROP CONSTRAINT ck_turn_records_trace_schema_version,
            DROP CONSTRAINT ck_turn_records_outcome,
            DROP COLUMN trace_schema_version,
            DROP COLUMN turn_index,
            DROP COLUMN diagnosis_causes,
            DROP COLUMN component_manifest_hash,
            DROP COLUMN outcome;
        """
    )
