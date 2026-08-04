"""The tenants this deployment serves, and what each is currently offering.

Seed configuration, held in code until `FEAT-006` moves tenant records into the
database with a draft/publish workflow. It is an adapter, not a domain concern:
the domain consumes ``TenantPolicy``, and where those come from is this layer's
problem.

Availability is a plain per-service list of labels. `DATA-003` replaces it with a
provider interface over a real calendar, at which point slots gain stable IDs and
start times and the "not in the past" rule becomes enforceable. Until then this
list is the authority on what may be booked, which is what stops a caller from
booking a time nobody offered.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tenantchat.core.catalog import ServiceCatalog, ServiceDefinition
from tenantchat.core.errors import NotFoundError
from tenantchat.core.tenant import PricingPolicy, TenantPolicy


@dataclass(frozen=True, slots=True)
class TenantRecord:
    """A tenant's policy together with what it is currently offering."""

    policy: TenantPolicy
    # Keyed by service slug, so renaming a display name cannot orphan the slots.
    availability: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def offered_slots(self, service_slug: str) -> tuple[str, ...]:
        return self.availability.get(service_slug, ())


_APEX = TenantRecord(
    policy=TenantPolicy(
        tenant_id="apex",
        name="Apex Home Services",
        assistant_name="Apex assistant",
        tagline="Phone-first service desk",
        phone="(555) 214-0800",
        address="2100 Harbor Street, Seattle, WA 98101",
        hours="Mon-Fri 8:00 AM-6:00 PM, Sat 9:00 AM-2:00 PM",
        catalog=ServiceCatalog.from_definitions(
            [
                ServiceDefinition("hvac", "HVAC", frozenset({"heating", "cooling", "ac", "a/c"})),
                ServiceDefinition("electrical", "Electrical", frozenset({"electric", "wiring"})),
                ServiceDefinition("plumbing", "Plumbing", frozenset({"plumber", "leak"})),
            ]
        ),
        pricing_policy=PricingPolicy.NEVER,
        booking_enabled=False,
        lead_capture_enabled=True,
        proactive_lead_capture=True,
        quick_actions=(
            "What are your hours?",
            "Do you serve 98103?",
            "How much is HVAC repair?",
            "Can I book electrical?",
            "Have someone call me",
        ),
        served_zips=frozenset({"98101", "98102", "98103", "98104", "98105"}),
    ),
)

_CLEARVIEW = TenantRecord(
    policy=TenantPolicy(
        tenant_id="clearview",
        name="Clearview Property Care",
        assistant_name="Clearview assistant",
        tagline="Pricing and booking enabled",
        phone="(555) 816-4420",
        address="480 Lakeview Avenue, Portland, OR 97205",
        hours="Daily 7:00 AM-7:00 PM",
        catalog=ServiceCatalog.from_definitions(
            [
                ServiceDefinition(
                    "window-cleaning",
                    "Window Cleaning",
                    frozenset({"windows", "window wash"}),
                ),
                ServiceDefinition("hvac", "HVAC", frozenset({"heating", "cooling", "ac", "a/c"})),
                ServiceDefinition("electrical", "Electrical", frozenset({"electric", "wiring"})),
            ]
        ),
        pricing_policy=PricingPolicy.FIXED,
        booking_enabled=True,
        lead_capture_enabled=True,
        proactive_lead_capture=True,
        quick_actions=(
            "What does window cleaning cost?",
            "Do you serve 97205?",
            "Book HVAC",
            "Electrical availability",
            "Request a follow-up",
        ),
        approved_prices=(
            ("window-cleaning", "$150/hour, 2 hour minimum"),
            ("hvac", "$120 diagnostic visit, repairs quoted after inspection"),
            ("electrical", "$140 diagnostic visit, panel work quoted after inspection"),
        ),
        served_zips=frozenset({"97035", "97201", "97202", "97203", "97204", "97205"}),
    ),
    availability={
        "window-cleaning": ("Mon Jul 1, 9:00 AM", "Tue Jul 2, 1:30 PM", "Thu Jul 4, 10:30 AM"),
        "hvac": ("Mon Jul 1, 2:00 PM", "Wed Jul 3, 11:00 AM", "Fri Jul 5, 9:30 AM"),
        "electrical": ("Tue Jul 2, 8:30 AM", "Wed Jul 3, 3:00 PM", "Fri Jul 5, 1:00 PM"),
    },
)


class TenantRegistry:
    """Lookup of the tenants this deployment serves."""

    def __init__(self, records: dict[str, TenantRecord]) -> None:
        self._records = records

    @classmethod
    def seeded(cls) -> TenantRegistry:
        return cls({"apex": _APEX, "clearview": _CLEARVIEW})

    def get(self, tenant_id: str) -> TenantRecord:
        """Look up a tenant.

        Raises:
            NotFoundError: no such tenant. The message names no tenant ID, so the
                endpoint cannot be used to enumerate which ones exist.
        """
        record = self._records.get(tenant_id)
        if record is None:
            raise NotFoundError(detail=f"unknown tenant {tenant_id!r}")
        return record

    def all(self) -> dict[str, TenantRecord]:
        return dict(self._records)


class RegistryPolicySource:
    """Serves :class:`TenantPolicy` from the seeded registry.

    An adapter over a synchronous in-process lookup, so the ``await`` buys
    nothing today. It is here because the port is async and `FEAT-006` moves
    these records into the database, where it will.
    """

    def __init__(self, registry: TenantRegistry) -> None:
        self._registry = registry

    async def policy(self, tenant_id: str) -> TenantPolicy:
        """The tenant's current policy.

        Raises:
            NotFoundError: no such tenant.
        """
        return self._registry.get(tenant_id).policy


class RegistryAvailabilityProvider:
    """Serves the seeded slot labels. `DATA-003` replaces this with a calendar."""

    def __init__(self, registry: TenantRegistry) -> None:
        self._registry = registry

    async def offered_slots(self, tenant_id: str, service_slug: str) -> tuple[str, ...]:
        """Slot labels currently bookable for one service.

        Raises:
            NotFoundError: no such tenant.
        """
        return self._registry.get(tenant_id).offered_slots(service_slug)
