"""Durable tenant-scoped jobs, leases, retries, and immutable job events.

Revision ID: 0009_durable_jobs
Revises: 0008_privacy
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_durable_jobs"
down_revision: str | None = "0008_privacy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the outbox state machine and its append-only audit trail."""
    op.execute(
        """
        CREATE TYPE background_job_status AS ENUM (
            'pending', 'running', 'succeeded', 'dead_lettered', 'cancelled'
        );

        CREATE TABLE background_jobs (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            kind varchar(64) NOT NULL,
            payload jsonb NOT NULL,
            payload_hash varchar(64) NOT NULL,
            idempotency_key varchar(200) NOT NULL,
            status background_job_status NOT NULL DEFAULT 'pending',
            attempt_count integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 5,
            replay_count integer NOT NULL DEFAULT 0,
            available_at timestamptz NOT NULL DEFAULT now(),
            lease_owner varchar(200),
            lease_expires_at timestamptz,
            last_error_code varchar(100),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            CONSTRAINT fk_background_jobs_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE RESTRICT,
            CONSTRAINT uq_background_jobs_tenant_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_background_jobs_dedupe
                UNIQUE (tenant_id, kind, idempotency_key),
            CONSTRAINT ck_background_jobs_kind CHECK
                (kind IN ('ingestion', 'crm_delivery', 'notification',
                          'privacy_deletion', 'webhook')),
            CONSTRAINT ck_background_jobs_payload_object CHECK
                (jsonb_typeof(payload) = 'object'),
            CONSTRAINT ck_background_jobs_payload_hash CHECK
                (payload_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_background_jobs_idempotency_key CHECK
                (btrim(idempotency_key) <> ''),
            CONSTRAINT ck_background_jobs_attempts CHECK
                (attempt_count >= 0 AND max_attempts BETWEEN 1 AND 100
                 AND replay_count >= 0),
            CONSTRAINT ck_background_jobs_error_code CHECK
                (last_error_code IS NULL OR
                 last_error_code ~ '^[a-z0-9][a-z0-9_.-]{0,99}$'),
            CONSTRAINT ck_background_jobs_lease_state CHECK
                ((status = 'running') =
                 (lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)),
            CONSTRAINT ck_background_jobs_completion_state CHECK
                ((status IN ('succeeded', 'dead_lettered', 'cancelled')) =
                 (completed_at IS NOT NULL))
        );

        CREATE TABLE background_job_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            job_id uuid NOT NULL,
            event varchar(40) NOT NULL,
            actor_type varchar(16) NOT NULL,
            actor_id varchar(200),
            request_id varchar(128),
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_background_job_events_job
                FOREIGN KEY (tenant_id, job_id)
                REFERENCES background_jobs (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT ck_background_job_events_event CHECK
                (event IN ('enqueued', 'leased', 'lease_renewed',
                           'retry_scheduled', 'succeeded', 'dead_lettered',
                           'operator_retried', 'operator_cancelled')),
            CONSTRAINT ck_background_job_events_actor CHECK
                (actor_type IN ('service', 'worker', 'staff')),
            CONSTRAINT ck_background_job_events_details_object CHECK
                (jsonb_typeof(details) = 'object')
        );

        CREATE INDEX ix_background_jobs_available
            ON background_jobs (available_at, created_at)
            WHERE status IN ('pending', 'running');
        CREATE INDEX ix_background_jobs_tenant_status_created
            ON background_jobs (tenant_id, status, created_at DESC);
        CREATE INDEX ix_background_job_events_tenant_job
            ON background_job_events (tenant_id, job_id, id);
        """
    )


def downgrade() -> None:
    """Remove the durable job subsystem; completed effects are not reversed."""
    op.execute(
        """
        DROP TABLE background_job_events;
        DROP TABLE background_jobs;
        DROP TYPE background_job_status;
        """
    )
