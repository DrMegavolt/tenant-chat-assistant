"""Reserve and confirm bookable slots with stable provider identity.

Revision ID: 0005_booking_reservation
Revises: 0004_agent_runtime
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_booking_reservation"
down_revision: str | None = "0004_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the database-backed fake calendar and make one confirmed booking per slot.

    ``slot_id`` is nullable to survive an upgrade of a database that still holds
    pre-`DATA-003` label-only bookings; every new booking writes it. The partial
    unique index is the reservation: two concurrent attempts on the same slot
    race for it, and exactly one wins. The composite foreign key makes it
    impossible to attach another tenant's slot to a booking, which is the
    database half of "the model cannot book a wrong-tenant slot".
    """
    op.execute(
        """
        CREATE TABLE availability_slots (
            id uuid PRIMARY KEY,
            tenant_id varchar(63) NOT NULL,
            service_slug varchar(100) NOT NULL,
            slot_start timestamptz NOT NULL,
            slot_end timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT fk_availability_slots_tenant FOREIGN KEY (tenant_id)
                REFERENCES tenants (id) ON DELETE RESTRICT,
            CONSTRAINT uq_availability_slots_tenant_service_window
                UNIQUE (tenant_id, service_slug, slot_start, slot_end),
            CONSTRAINT uq_availability_slots_tenant_id
                UNIQUE (tenant_id, id),
            CONSTRAINT ck_availability_slots_service_not_blank
                CHECK (btrim(service_slug) <> ''),
            CONSTRAINT ck_availability_slots_slot_order CHECK (slot_end > slot_start)
        );

        ALTER TABLE bookings ADD COLUMN slot_id uuid;
        ALTER TABLE bookings ADD CONSTRAINT fk_bookings_slot
            FOREIGN KEY (tenant_id, slot_id)
            REFERENCES availability_slots (tenant_id, id) ON DELETE RESTRICT;
        CREATE UNIQUE INDEX uq_bookings_one_confirmed_per_slot
            ON bookings (slot_id) WHERE status = 'confirmed';
        CREATE INDEX ix_availability_slots_tenant_service
            ON availability_slots (tenant_id, service_slug, slot_start);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP INDEX uq_bookings_one_confirmed_per_slot;
        ALTER TABLE bookings DROP CONSTRAINT fk_bookings_slot;
        ALTER TABLE bookings DROP COLUMN slot_id;
        DROP TABLE availability_slots;
        """
    )
