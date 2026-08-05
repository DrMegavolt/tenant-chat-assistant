"""The PRIV-002 inference trace plane: turn records, projections, and the
trace-reader grant.

Revision ID: 0010_trace_privacy
Revises: 0009_durable_jobs
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0010_trace_privacy"
down_revision: str | None = "0009_durable_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the inference plane's governed envelope and its access role.

    ``turn_records`` is the `ADR-0010` second plane: one append-only row per
    conversation turn holding the content `OBS-004` will populate. PRIV-002
    owns the governance around it — retention purge, subject export/erasure,
    and role-gated audited reads — so the schema here is the *envelope* (the
    content is an opaque object the schema never parses) plus the governance
    surfaces:

    - ``recorded_at`` drives an independent retention cutoff, shorter than the
      transcript's (see ``RetentionPolicy`` defaults in ``core.privacy``).
    - ``turn_record_projections`` is where a derived dataset — an evaluation
      dataset promoted under `FEAT-008` — pins itself to the turn it was
      derived from. The foreign key cascades, so erasing a turn record removes
      every projection of it in the same statement.
    - ``trace_access_grants`` is the dedicated role for turn-record reads,
      deliberately separate from ``tenant_memberships``: an operator may hold
      it without any transcript role, and a tenant admin's membership row
      confers no trace access. Every read is additionally audited with a
      reason (see ``core.privacy.TurnRecordReadReason``).

    ``content`` is intentionally an opaque ``jsonb`` object: the schema must
    not grow columns for prompt/evidence/output fields, because `OBS-004` owns
    that shape and `PRIV-002` must not freeze it.
    """
    op.execute(
        """
        CREATE TABLE turn_records (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            chat_session_id uuid NOT NULL,
            trace_id varchar(128),
            content jsonb NOT NULL DEFAULT '{}'::jsonb,
            recorded_at timestamptz NOT NULL,
            CONSTRAINT fk_turn_records_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE RESTRICT,
            CONSTRAINT fk_turn_records_session FOREIGN KEY (tenant_id, chat_session_id)
                REFERENCES chat_sessions (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_turn_records_tenant_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_turn_records_content_object CHECK
                (jsonb_typeof(content) = 'object'),
            CONSTRAINT ck_turn_records_trace_id CHECK
                (trace_id IS NULL OR btrim(trace_id) <> '')
        );

        CREATE TABLE turn_record_projections (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            turn_record_id uuid NOT NULL,
            kind varchar(64) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_turn_record_projections_turn
                FOREIGN KEY (tenant_id, turn_record_id)
                REFERENCES turn_records (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT ck_turn_record_projections_kind CHECK
                (btrim(kind) <> '')
        );

        CREATE TABLE trace_access_grants (
            tenant_id varchar(63) NOT NULL,
            principal_subject varchar(200) NOT NULL,
            granted_at timestamptz NOT NULL DEFAULT now(),
            granted_by varchar(200) NOT NULL,
            CONSTRAINT fk_trace_access_grants_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE CASCADE,
            CONSTRAINT pk_trace_access_grants PRIMARY KEY (tenant_id, principal_subject),
            CONSTRAINT ck_trace_access_grants_subject_not_blank
                CHECK (btrim(principal_subject) <> ''),
            CONSTRAINT ck_trace_access_grants_granted_by_not_blank
                CHECK (btrim(granted_by) <> '')
        );

        CREATE INDEX ix_turn_records_tenant_session
            ON turn_records (tenant_id, chat_session_id, recorded_at DESC);
        CREATE INDEX ix_turn_records_tenant_recorded
            ON turn_records (tenant_id, recorded_at);
        CREATE INDEX ix_turn_record_projections_turn
            ON turn_record_projections (tenant_id, turn_record_id);
        CREATE INDEX ix_trace_access_grants_tenant
            ON trace_access_grants (tenant_id, principal_subject);
        CREATE INDEX ix_trace_access_grants_principal
            ON trace_access_grants (principal_subject);
        """
    )


def downgrade() -> None:
    """Remove the trace plane. Restoring from backup is the real recovery."""
    op.execute(
        """
        DROP TABLE trace_access_grants;
        DROP TABLE turn_record_projections;
        DROP TABLE turn_records;
        """
    )
