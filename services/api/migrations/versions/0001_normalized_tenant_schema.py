"""Create the normalized, tenant-scoped domain schema.

Revision ID: 0001_normalized
Revises: None
Create Date: 2026-08-01
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import context, op
from sqlalchemy import inspect

revision: str = "0001_normalized"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ENUMS = (
    ("tenant_status", ("active", "suspended", "disabled")),
    ("chat_session_status", ("active", "waiting_for_staff", "closed")),
    (
        "chat_session_outcome",
        ("none", "abandoned", "lead", "booked", "handoff", "completed"),
    ),
    ("message_role", ("visitor", "assistant", "staff", "system", "tool")),
    ("tool_execution_status", ("requested", "running", "succeeded", "failed")),
    ("lead_status", ("new", "qualified", "contacted", "converted", "closed")),
    ("lead_urgency", ("low", "normal", "high", "emergency")),
    ("booking_status", ("pending", "confirmed", "cancelled", "failed")),
    ("handoff_status", ("requested", "queued", "assigned", "resolved", "cancelled")),
    ("idempotency_status", ("in_progress", "completed", "failed")),
    ("audit_actor_type", ("visitor", "staff", "service", "system")),
)


def upgrade() -> None:
    """Create all system-of-record tables and tenant-safe relationships."""
    bind = op.get_bind()
    if not context.is_offline_mode() and inspect(bind).has_table("chat_sessions"):
        msg = (
            "A pre-Alembic chat_sessions table was found. Stop the prototype, back it up, "
            "and follow docs/runbooks/database-migrations.md; this migration never deletes "
            "prototype snapshots automatically."
        )
        raise RuntimeError(msg)

    for name, values in ENUMS:
        quoted = ", ".join(f"'{value}'" for value in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({quoted})")

    op.execute(
        """
        CREATE TABLE tenants (
            id varchar(63) PRIMARY KEY,
            display_name varchar(160) NOT NULL,
            status tenant_status NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT ck_tenants_id_format CHECK (id ~ '^[a-z][a-z0-9-]{1,62}$'),
            CONSTRAINT ck_tenants_display_name_not_blank CHECK (btrim(display_name) <> ''),
            CONSTRAINT ck_tenants_timestamps CHECK (updated_at >= created_at)
        );

        CREATE TABLE chat_sessions (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            visitor_subject_id varchar(128),
            status chat_session_status NOT NULL DEFAULT 'active',
            outcome chat_session_outcome NOT NULL DEFAULT 'none',
            version bigint NOT NULL DEFAULT 1,
            started_at timestamptz NOT NULL DEFAULT now(),
            last_activity_at timestamptz NOT NULL DEFAULT now(),
            closed_at timestamptz,
            CONSTRAINT fk_chat_sessions_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE RESTRICT,
            CONSTRAINT uq_chat_sessions_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_chat_sessions_version_positive CHECK (version > 0),
            CONSTRAINT ck_chat_sessions_activity_order CHECK (last_activity_at >= started_at),
            CONSTRAINT ck_chat_sessions_closed_order CHECK
                (closed_at IS NULL OR closed_at >= started_at),
            CONSTRAINT ck_chat_sessions_closed_state CHECK
                ((status = 'closed') = (closed_at IS NOT NULL))
        );

        CREATE TABLE messages (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            chat_session_id uuid NOT NULL,
            sequence_number bigint NOT NULL,
            role message_role NOT NULL,
            content text NOT NULL,
            model_name varchar(160),
            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_messages_session FOREIGN KEY (tenant_id, chat_session_id)
                REFERENCES chat_sessions (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT uq_messages_tenant_session_sequence
                UNIQUE (tenant_id, chat_session_id, sequence_number),
            CONSTRAINT uq_messages_tenant_session_id
                UNIQUE (tenant_id, chat_session_id, id),
            CONSTRAINT ck_messages_sequence_positive CHECK (sequence_number > 0),
            CONSTRAINT ck_messages_content_not_blank CHECK (btrim(content) <> ''),
            CONSTRAINT ck_messages_metadata_object CHECK (jsonb_typeof(metadata) = 'object')
        );

        CREATE TABLE tool_executions (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            chat_session_id uuid NOT NULL,
            request_message_id uuid,
            tool_name varchar(100) NOT NULL,
            status tool_execution_status NOT NULL DEFAULT 'requested',
            arguments jsonb NOT NULL DEFAULT '{}'::jsonb,
            result jsonb,
            error_code varchar(100),
            started_at timestamptz,
            finished_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_tool_executions_session FOREIGN KEY (tenant_id, chat_session_id)
                REFERENCES chat_sessions (tenant_id, id) ON DELETE CASCADE,
            CONSTRAINT fk_tool_executions_request_message
                FOREIGN KEY (tenant_id, chat_session_id, request_message_id)
                REFERENCES messages (tenant_id, chat_session_id, id) ON DELETE RESTRICT,
            CONSTRAINT uq_tool_executions_tenant_session_id
                UNIQUE (tenant_id, chat_session_id, id),
            CONSTRAINT ck_tool_executions_name_not_blank CHECK (btrim(tool_name) <> ''),
            CONSTRAINT ck_tool_executions_arguments_object
                CHECK (jsonb_typeof(arguments) = 'object'),
            CONSTRAINT ck_tool_executions_result_object
                CHECK (result IS NULL OR jsonb_typeof(result) = 'object'),
            CONSTRAINT ck_tool_executions_time_order CHECK
                (finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)),
            CONSTRAINT ck_tool_executions_terminal_time CHECK
                ((status IN ('succeeded', 'failed')) = (finished_at IS NOT NULL)),
            CONSTRAINT ck_tool_executions_error_state CHECK
                (error_code IS NULL OR status = 'failed')
        );

        CREATE TABLE leads (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            chat_session_id uuid NOT NULL,
            status lead_status NOT NULL DEFAULT 'new',
            urgency lead_urgency NOT NULL DEFAULT 'normal',
            customer_name varchar(160) NOT NULL,
            contact_kind varchar(16) NOT NULL,
            contact_value varchar(320) NOT NULL,
            service_slug varchar(100),
            summary text NOT NULL,
            address_or_zip varchar(500),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_leads_session FOREIGN KEY (tenant_id, chat_session_id)
                REFERENCES chat_sessions (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT uq_leads_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_leads_customer_name_not_blank CHECK (btrim(customer_name) <> ''),
            CONSTRAINT ck_leads_contact_kind CHECK (contact_kind IN ('email', 'phone')),
            CONSTRAINT ck_leads_contact_value_not_blank CHECK (btrim(contact_value) <> ''),
            CONSTRAINT ck_leads_summary_not_blank CHECK (btrim(summary) <> ''),
            CONSTRAINT ck_leads_timestamps CHECK (updated_at >= created_at)
        );

        CREATE TABLE bookings (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            chat_session_id uuid NOT NULL,
            status booking_status NOT NULL DEFAULT 'pending',
            reference varchar(64) NOT NULL,
            service_slug varchar(100) NOT NULL,
            provider_name varchar(100) NOT NULL,
            provider_booking_id varchar(200),
            slot_start timestamptz NOT NULL,
            slot_end timestamptz NOT NULL,
            customer_name varchar(160) NOT NULL,
            contact_kind varchar(16) NOT NULL,
            contact_value varchar(320) NOT NULL,
            service_address varchar(500) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            confirmed_at timestamptz,
            cancelled_at timestamptz,
            CONSTRAINT fk_bookings_session FOREIGN KEY (tenant_id, chat_session_id)
                REFERENCES chat_sessions (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT uq_bookings_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT uq_bookings_tenant_reference UNIQUE (tenant_id, reference),
            CONSTRAINT uq_bookings_tenant_provider_booking
                UNIQUE (tenant_id, provider_name, provider_booking_id),
            CONSTRAINT ck_bookings_reference_not_blank CHECK (btrim(reference) <> ''),
            CONSTRAINT ck_bookings_service_not_blank CHECK (btrim(service_slug) <> ''),
            CONSTRAINT ck_bookings_provider_not_blank CHECK (btrim(provider_name) <> ''),
            CONSTRAINT ck_bookings_slot_order CHECK (slot_end > slot_start),
            CONSTRAINT ck_bookings_customer_name_not_blank CHECK (btrim(customer_name) <> ''),
            CONSTRAINT ck_bookings_contact_kind CHECK (contact_kind IN ('email', 'phone')),
            CONSTRAINT ck_bookings_contact_value_not_blank CHECK (btrim(contact_value) <> ''),
            CONSTRAINT ck_bookings_address_not_blank CHECK (btrim(service_address) <> ''),
            CONSTRAINT ck_bookings_timestamps CHECK (updated_at >= created_at),
            CONSTRAINT ck_bookings_confirmed_state CHECK
                (confirmed_at IS NULL OR status IN ('confirmed', 'cancelled')),
            CONSTRAINT ck_bookings_cancelled_state CHECK
                ((status = 'cancelled') = (cancelled_at IS NOT NULL))
        );

        CREATE TABLE handoffs (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            chat_session_id uuid NOT NULL,
            status handoff_status NOT NULL DEFAULT 'requested',
            reason text NOT NULL,
            assigned_principal_id varchar(200),
            requested_at timestamptz NOT NULL DEFAULT now(),
            assigned_at timestamptz,
            resolved_at timestamptz,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_handoffs_session FOREIGN KEY (tenant_id, chat_session_id)
                REFERENCES chat_sessions (tenant_id, id) ON DELETE RESTRICT,
            CONSTRAINT uq_handoffs_tenant_id_id UNIQUE (tenant_id, id),
            CONSTRAINT ck_handoffs_reason_not_blank CHECK (btrim(reason) <> ''),
            CONSTRAINT ck_handoffs_assignment_consistent CHECK
                ((assigned_principal_id IS NULL) = (assigned_at IS NULL)),
            CONSTRAINT ck_handoffs_assigned_state CHECK
                (assigned_at IS NULL OR status IN ('assigned', 'resolved')),
            CONSTRAINT ck_handoffs_resolved_state CHECK
                ((status = 'resolved') = (resolved_at IS NOT NULL)),
            CONSTRAINT ck_handoffs_time_order CHECK
                (assigned_at IS NULL OR assigned_at >= requested_at),
            CONSTRAINT ck_handoffs_resolution_order CHECK
                (resolved_at IS NULL OR resolved_at >= requested_at),
            CONSTRAINT ck_handoffs_updated_order CHECK (updated_at >= requested_at)
        );

        CREATE TABLE idempotency_keys (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            scope varchar(100) NOT NULL,
            key_hash char(64) NOT NULL,
            request_hash char(64) NOT NULL,
            status idempotency_status NOT NULL DEFAULT 'in_progress',
            resource_type varchar(100),
            resource_id uuid,
            response jsonb,
            created_at timestamptz NOT NULL DEFAULT now(),
            completed_at timestamptz,
            expires_at timestamptz NOT NULL,
            CONSTRAINT fk_idempotency_keys_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE RESTRICT,
            CONSTRAINT uq_idempotency_keys_tenant_scope_hash
                UNIQUE (tenant_id, scope, key_hash),
            CONSTRAINT ck_idempotency_keys_scope_not_blank CHECK (btrim(scope) <> ''),
            CONSTRAINT ck_idempotency_keys_key_hash CHECK (key_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_idempotency_keys_request_hash CHECK (request_hash ~ '^[0-9a-f]{64}$'),
            CONSTRAINT ck_idempotency_keys_response_object
                CHECK (response IS NULL OR jsonb_typeof(response) = 'object'),
            CONSTRAINT ck_idempotency_keys_completion_state CHECK
                ((status = 'completed') = (completed_at IS NOT NULL)),
            CONSTRAINT ck_idempotency_keys_expiry_order CHECK (expires_at > created_at),
            CONSTRAINT ck_idempotency_keys_resource_pair CHECK
                ((resource_type IS NULL) = (resource_id IS NULL))
        );

        CREATE TABLE audit_events (
            id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            actor_type audit_actor_type NOT NULL,
            principal_id varchar(200),
            action varchar(120) NOT NULL,
            resource_type varchar(100) NOT NULL,
            resource_id uuid,
            request_id varchar(64),
            details jsonb NOT NULL DEFAULT '{}'::jsonb,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_audit_events_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE RESTRICT,
            CONSTRAINT ck_audit_events_action_not_blank CHECK (btrim(action) <> ''),
            CONSTRAINT ck_audit_events_resource_not_blank CHECK (btrim(resource_type) <> ''),
            CONSTRAINT ck_audit_events_details_object CHECK (jsonb_typeof(details) = 'object')
        );

        CREATE INDEX ix_chat_sessions_tenant_activity
            ON chat_sessions (tenant_id, last_activity_at DESC);
        CREATE INDEX ix_chat_sessions_tenant_status_activity
            ON chat_sessions (tenant_id, status, last_activity_at DESC);
        CREATE INDEX ix_messages_tenant_session_created
            ON messages (tenant_id, chat_session_id, created_at);
        CREATE INDEX ix_tool_executions_tenant_session_created
            ON tool_executions (tenant_id, chat_session_id, created_at);
        CREATE INDEX ix_tool_executions_tenant_status_created
            ON tool_executions (tenant_id, status, created_at);
        CREATE INDEX ix_leads_tenant_status_created
            ON leads (tenant_id, status, created_at DESC);
        CREATE INDEX ix_bookings_tenant_status_slot
            ON bookings (tenant_id, status, slot_start);
        CREATE INDEX ix_bookings_tenant_session_created
            ON bookings (tenant_id, chat_session_id, created_at DESC);
        CREATE INDEX ix_handoffs_tenant_status_requested
            ON handoffs (tenant_id, status, requested_at);
        CREATE INDEX ix_idempotency_keys_tenant_expiry
            ON idempotency_keys (tenant_id, expires_at);
        CREATE INDEX ix_audit_events_tenant_occurred
            ON audit_events (tenant_id, occurred_at DESC);
        CREATE INDEX ix_audit_events_tenant_resource
            ON audit_events (tenant_id, resource_type, resource_id, occurred_at DESC);
        """
    )


def downgrade() -> None:
    """Remove this revision. Production recovery should restore a backup instead."""
    for table in (
        "audit_events",
        "idempotency_keys",
        "handoffs",
        "bookings",
        "leads",
        "tool_executions",
        "messages",
        "chat_sessions",
        "tenants",
    ):
        op.execute(f"DROP TABLE {table}")
    for name, _values in reversed(ENUMS):
        op.execute(f"DROP TYPE {name}")
