"""Store current API actions without weakening future booking semantics.

Revision ID: 0002_repositories
Revises: 0001_normalized
Create Date: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_repositories"
down_revision: str | None = "0001_normalized"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the fields required by the DATA-002 persistence adapters."""
    op.execute(
        """
        ALTER TABLE chat_sessions ADD COLUMN client_correlation_id varchar(128);
        CREATE UNIQUE INDEX uq_chat_sessions_tenant_client_correlation
            ON chat_sessions (tenant_id, client_correlation_id)
            WHERE client_correlation_id IS NOT NULL;

        ALTER TYPE lead_urgency RENAME TO lead_urgency_data001;
        CREATE TYPE lead_urgency AS ENUM
            ('low', 'normal', 'high', 'emergency', 'today', 'this_week', 'flexible', 'unknown');
        ALTER TABLE leads ALTER COLUMN urgency DROP DEFAULT;
        ALTER TABLE leads ALTER COLUMN urgency TYPE lead_urgency
            USING urgency::text::lead_urgency;
        ALTER TABLE leads ALTER COLUMN urgency SET DEFAULT 'normal';
        DROP TYPE lead_urgency_data001;

        ALTER TABLE leads ADD COLUMN service_label varchar(120);
        UPDATE leads SET service_label = COALESCE(service_slug, 'Unspecified service');
        ALTER TABLE leads ALTER COLUMN service_label SET NOT NULL;
        ALTER TABLE leads ADD CONSTRAINT ck_leads_service_label_not_blank
            CHECK (btrim(service_label) <> '');

        ALTER TABLE bookings ADD COLUMN service_label varchar(120);
        UPDATE bookings SET service_label = service_slug;
        ALTER TABLE bookings ALTER COLUMN service_label SET NOT NULL;
        ALTER TABLE bookings ADD CONSTRAINT ck_bookings_service_label_not_blank
            CHECK (btrim(service_label) <> '');

        ALTER TABLE bookings ADD COLUMN slot_label varchar(512);
        UPDATE bookings SET slot_label = slot_start::text || ' - ' || slot_end::text;
        ALTER TABLE bookings ALTER COLUMN slot_label SET NOT NULL;
        ALTER TABLE bookings ADD CONSTRAINT ck_bookings_slot_label_not_blank
            CHECK (btrim(slot_label) <> '');
        ALTER TABLE bookings ALTER COLUMN slot_start DROP NOT NULL;
        ALTER TABLE bookings ALTER COLUMN slot_end DROP NOT NULL;
        ALTER TABLE bookings ADD CONSTRAINT ck_bookings_slot_time_pair
            CHECK ((slot_start IS NULL) = (slot_end IS NULL));
        """
    )


def downgrade() -> None:
    """Return to DATA-001; populated prototype-only values deliberately block."""
    op.execute(
        """
        ALTER TABLE bookings DROP CONSTRAINT ck_bookings_slot_time_pair;
        ALTER TABLE bookings ALTER COLUMN slot_start SET NOT NULL;
        ALTER TABLE bookings ALTER COLUMN slot_end SET NOT NULL;
        ALTER TABLE bookings DROP CONSTRAINT ck_bookings_slot_label_not_blank;
        ALTER TABLE bookings DROP COLUMN slot_label;
        ALTER TABLE bookings DROP CONSTRAINT ck_bookings_service_label_not_blank;
        ALTER TABLE bookings DROP COLUMN service_label;

        ALTER TABLE leads DROP CONSTRAINT ck_leads_service_label_not_blank;
        ALTER TABLE leads DROP COLUMN service_label;
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM leads
                WHERE urgency::text IN ('today', 'this_week', 'flexible', 'unknown')
            ) THEN
                RAISE EXCEPTION
                    'Cannot downgrade while leads use DATA-002 urgency values';
            END IF;
        END
        $$;
        ALTER TYPE lead_urgency RENAME TO lead_urgency_data002;
        CREATE TYPE lead_urgency AS ENUM ('low', 'normal', 'high', 'emergency');
        ALTER TABLE leads ALTER COLUMN urgency DROP DEFAULT;
        ALTER TABLE leads ALTER COLUMN urgency TYPE lead_urgency
            USING urgency::text::lead_urgency;
        ALTER TABLE leads ALTER COLUMN urgency SET DEFAULT 'normal';
        DROP TYPE lead_urgency_data002;

        DROP INDEX uq_chat_sessions_tenant_client_correlation;
        ALTER TABLE chat_sessions DROP COLUMN client_correlation_id;
        """
    )
