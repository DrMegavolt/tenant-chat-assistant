"""Consent records and the deletion-request queue.

Revision ID: 0005_privacy
Revises: 0004_agent_runtime
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_privacy"
down_revision: str | None = "0004_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the tables `PRIV-001` records consent and deletion requests in.

    ``consent_records`` is the proof a contact-bearing action requires. The
    unique key per session and purpose makes a re-recorded grant idempotent;
    withdrawal is a status flip, never a delete, so "consent was given and
    then withdrawn" stays answerable.

    ``privacy_requests`` is a queue, not a log: the erasure worker owns the
    lifecycle, and the app role is granted no ``DELETE`` on either table (see
    ``provision_app_role.sql``), so a request cannot be made to disappear.

    The audit ``resource_id`` is widened from ``uuid`` to ``varchar`` so a
    privacy event can name a resource that has no uuid — the retention purge
    names the data class (``transcript``) and an export names the contact kind
    (``phone``). An existing uuid resource id is carried through unchanged.
    """
    op.execute(
        "ALTER TABLE audit_events ALTER COLUMN resource_id TYPE varchar(200) "
        "USING resource_id::text"
    )
    op.execute(
        """
        CREATE TYPE consent_purpose AS ENUM ('booking', 'follow_up');
        CREATE TYPE consent_status AS ENUM ('granted', 'withdrawn');
        CREATE TYPE privacy_request_status AS ENUM ('pending', 'completed', 'failed');

        CREATE TABLE consent_records (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            chat_session_id uuid NOT NULL,
            purpose consent_purpose NOT NULL,
            status consent_status NOT NULL DEFAULT 'granted',
            statement text NOT NULL,
            granted_at timestamptz NOT NULL DEFAULT now(),
            withdrawn_at timestamptz,
            CONSTRAINT fk_consent_records_session FOREIGN KEY (tenant_id, chat_session_id)
                REFERENCES chat_sessions (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_consent_records_tenant_session_purpose
                UNIQUE (tenant_id, chat_session_id, purpose),
            CONSTRAINT ck_consent_records_statement_not_blank
                CHECK (btrim(statement) <> ''),
            CONSTRAINT ck_consent_records_withdrawn_state CHECK
                ((status = 'withdrawn') = (withdrawn_at IS NOT NULL))
        );

        CREATE TABLE privacy_requests (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            status privacy_request_status NOT NULL DEFAULT 'pending',
            contact_kind varchar(16) NOT NULL,
            contact_value varchar(320) NOT NULL,
            requested_by varchar(200) NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            processed_at timestamptz,
            CONSTRAINT fk_privacy_requests_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE RESTRICT,
            CONSTRAINT ck_privacy_requests_contact_kind CHECK
                (contact_kind IN ('email', 'phone')),
            CONSTRAINT ck_privacy_requests_contact_not_blank
                CHECK (btrim(contact_value) <> ''),
            CONSTRAINT ck_privacy_requests_completion_state CHECK
                ((status = 'pending') = (processed_at IS NULL))
        );

        CREATE INDEX ix_consent_records_tenant_session
            ON consent_records (tenant_id, chat_session_id);
        CREATE INDEX ix_privacy_requests_tenant_status
            ON privacy_requests (tenant_id, status, requested_at DESC);
        CREATE INDEX ix_privacy_requests_status_requested
            ON privacy_requests (status, requested_at);
        """
    )


def downgrade() -> None:
    """Remove the privacy tables. Restoring from backup is the real recovery."""
    op.execute(
        """
        DROP TABLE privacy_requests;
        DROP TABLE consent_records;
        DROP TYPE privacy_request_status;
        DROP TYPE consent_status;
        DROP TYPE consent_purpose;
        """
    )
