"""The database-backed fake availability provider for the demo.

``registry.py``'s ``DemoAvailabilityProvider`` is the in-process test double;
this is the one production composition runs. Both speak the
:class:`~tenantchat.core.ports.AvailabilityProvider` port and both hand back
real :class:`~tenantchat.core.slots.OfferedSlot` values, so the graph, routes,
and reservation exercise identical rules over whichever source is installed.

It is a *fake* provider, not a real calendar integration: seeding drops the same
future window the demo provider synthesizes into ``availability_slots``, and
reading lists only slots that are still in the future and not already the
subject of a confirmed booking. A real provider (`FEAT-005`) replaces this
adapter, not the domain.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tenantchat.api.persistence.tenancy import require_active_tenant
from tenantchat.api.registry import TenantRegistry, demo_offered_slots
from tenantchat.core.slots import OfferedSlot


async def seed_demo_availability(engine: AsyncEngine, registry: TenantRegistry) -> None:
    """Idempotently populate ``availability_slots`` for the seeded tenants.

    Re-running a boot must not duplicate rows: the unique index on
    (tenant, service, start) is what makes the same absolute window a no-op.
    The window drifts forward across boots (it is generated from each run's
    clock), which is fine — the reader filters to future slots, and `FEAT-005`
    owns pruning and the real calendar.
    """
    async with engine.begin() as connection:
        for tenant_id, record in registry.all().items():
            for definition in record.policy.catalog.definitions:
                for slot in demo_offered_slots(definition.slug):
                    await connection.execute(
                        text(
                            """
                            INSERT INTO availability_slots
                                (id, tenant_id, service_slug, slot_start, slot_end)
                            VALUES (:id, :tenant_id, :service_slug, :slot_start, :slot_end)
                            ON CONFLICT (tenant_id, service_slug, slot_start, slot_end)
                                DO NOTHING
                            """
                        ),
                        {
                            "id": slot.id,
                            "tenant_id": tenant_id,
                            "service_slug": definition.slug,
                            "slot_start": slot.start,
                            "slot_end": slot.end,
                        },
                    )


class PostgresAvailabilityProvider:
    """Lists what the database-backed fake provider is currently offering."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def offered_slots(self, tenant_id: str, service_slug: str) -> tuple[OfferedSlot, ...]:
        """Slots still in the future and not already confirmed-booked.

        A slot with a confirmed booking is excluded so a customer who reads
        availability a second time never sees the window a competitor just
        took. The race — two readers both seeing it, both trying to book — is
        settled by the reservation's uniqueness constraint, not by this read.

        Raises:
            NotFoundError: the tenant does not exist or is not active.
        """
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    """
                    SELECT id, service_slug, slot_start, slot_end
                    FROM availability_slots
                    WHERE tenant_id = :tenant_id
                      AND service_slug = :service_slug
                      AND slot_end > now()
                      AND NOT EXISTS (
                          SELECT 1 FROM bookings
                          WHERE bookings.tenant_id = availability_slots.tenant_id
                            AND bookings.slot_id = availability_slots.id
                            AND bookings.status = 'confirmed'
                      )
                    ORDER BY slot_start
                    """
                ),
                {"tenant_id": tenant_id, "service_slug": service_slug},
            )
            rows = result.all()
        return tuple(
            OfferedSlot(
                id=str(row.id),
                service_slug=row.service_slug,
                start=row.slot_start,
                end=row.slot_end,
            )
            for row in rows
        )
