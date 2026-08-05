"""Tenant-scoped operator memberships (SEC-001 per-tenant role assignment).

Revision ID: 0005_tenant_memberships
Revises: 0004_agent_runtime
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_tenant_memberships"
down_revision: str | None = "0004_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the per-tenant operator role assignments.

    The four-column composite key is the record's identity: one operator holds
    exactly one role per tenant, and revoking deletes the row rather than
    flagging it, because the audit table already records who assigned and who
    revoked and when. ``platform_admin`` is deliberately not an assignable
    value: that role spans tenants and is decided by the identity provider's
    group membership, never by a row a tenant-facing admin could write.
    """
    op.execute(
        """
        CREATE TABLE tenant_memberships (
            tenant_id varchar(63) NOT NULL,
            principal_subject varchar(200) NOT NULL,
            role varchar(32) NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_tenant_memberships_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE CASCADE,
            CONSTRAINT pk_tenant_memberships PRIMARY KEY (tenant_id, principal_subject),
            CONSTRAINT ck_tenant_memberships_subject_not_blank
                CHECK (btrim(principal_subject) <> ''),
            CONSTRAINT ck_tenant_memberships_role CHECK
                (role IN ('viewer', 'support_agent', 'tenant_admin')),
            CONSTRAINT ck_tenant_memberships_timestamps CHECK (updated_at >= created_at)
        );

        CREATE INDEX ix_tenant_memberships_principal
            ON tenant_memberships (principal_subject);

        CREATE INDEX ix_tenant_memberships_tenant
            ON tenant_memberships (tenant_id);
        """
    )


def downgrade() -> None:
    """Remove this revision. Production recovery should restore a backup instead."""
    op.execute("DROP TABLE tenant_memberships")
