"""The tenants this deployment serves, and what each is currently offering.

Seed configuration, held in code until `FEAT-006` moves tenant records into the
database with a draft/publish workflow. It is an adapter, not a domain concern:
the domain consumes ``TenantPolicy``, and where those come from is this layer's
problem.

Availability is no longer a fixed list of labels. `DATA-003` introduced the
:class:`~tenantchat.core.ports.AvailabilityProvider` port over a live calendar:
the demo provider here synthesizes a future window of structured slots (with
stable IDs and timezone-aware bounds), and production swaps it for the
database-backed adapter that seeds and reads the same shape from Postgres. The
"not in the past" and "belongs to this tenant" rules live in the domain, which
is what keeps them true no matter which source supplies the slots.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Set
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from tenantchat.core.budgets import TenantBudget
from tenantchat.core.catalog import ServiceCatalog, ServiceDefinition
from tenantchat.core.errors import NotFoundError
from tenantchat.core.slots import OfferedSlot
from tenantchat.core.tenant import PricingPolicy, TenantPolicy

# How far ahead the demo calendar runs, and the hours it offers each day. Kept
# in one place so the API route, the agent, and the hermetic tests all see the
# same window; `FEAT-005` replaces this generation with a real calendar.
_DEMO_DAYS = 5
_DEMO_HOURS = ((9, 0), (11, 0), (13, 0), (15, 0))


def demo_offered_slots(
    service_slug: str, *, now: datetime | None = None
) -> tuple[OfferedSlot, ...]:
    """A future window of bookable slots for one service.

    ``now`` is injectable so a test pins an exact offer instead of gambling on
    the clock. The window starts at the next midnight (never today), so the
    first slot is always clearly in the future and a run that crosses midnight
    does not flip which slots are offered. IDs are minted once per call and
    persist for the provider's lifetime, which is what makes a later reservation
    of a slot shown earlier stable.
    """
    anchor = now if now is not None else datetime.now(UTC)
    first_day = anchor.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    slots: list[OfferedSlot] = []
    for day in range(_DEMO_DAYS):
        for hour, minute in _DEMO_HOURS:
            start = first_day + timedelta(days=day, hours=hour, minutes=minute)
            slots.append(
                OfferedSlot(
                    id=str(uuid.uuid4()),
                    service_slug=service_slug,
                    start=start,
                    end=start + timedelta(hours=1),
                )
            )
    return tuple(slots)


@dataclass(frozen=True, slots=True)
class TenantRecord:
    """A tenant's policy. Its current offerings come from an availability provider."""

    policy: TenantPolicy


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
        budgets=TenantBudget(
            # A phone-first tenant that captures leads: generous, but the
            # spend thresholds make an operator notice a run-away demo.
            daily_token_budget=200_000,
            spend_warn_threshold_tokens=150_000,
            spend_critical_threshold_tokens=200_000,
        ),
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
        budgets=TenantBudget(
            # The paying booking tenant runs the platform default.
            daily_token_budget=200_000,
            spend_warn_threshold_tokens=150_000,
            spend_critical_threshold_tokens=200_000,
        ),
    ),
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


class DemoAvailabilityProvider:
    """The in-process availability source: a stable future window per offer.

    A test double in the sense that it needs no database, but it is not a stub —
    it returns real :class:`OfferedSlot` values with aware bounds and stable
    IDs, so the graph, the routes, and the reservation exercise the same rules
    they will against the production Postgres-backed provider. Production
    composition never constructs this; it is the default for in-memory test
    composition and a true database-backed adapter replaces it there.
    """

    def __init__(
        self,
        registry: TenantRegistry,
        *,
        now: datetime | None = None,
        taken: Callable[[str], Set[str]] | None = None,
    ) -> None:
        self._registry = registry
        self._now = now
        # ``taken`` names slot IDs that are no longer bookable, so a refresh
        # after a reservation conflict reports the real alternatives instead of
        # the set that just lost the race. In-memory composition wires the
        # booking store here; the Postgres provider excludes booked slots in SQL.
        self._taken = taken if taken is not None else (lambda _tenant: frozenset())
        self._cache: dict[tuple[str, str], tuple[OfferedSlot, ...]] = {}

    async def offered_slots(self, tenant_id: str, service_slug: str) -> tuple[OfferedSlot, ...]:
        """Slots currently bookable for one service, empty for an unoffered one.

        Raises:
            NotFoundError: no such tenant.
        """
        # The tenant lookup is what makes a guessed service on a real tenant
        # distinct from a real service on a guessed tenant.
        self._registry.get(tenant_id)
        cache_key = (tenant_id, service_slug)
        if cache_key not in self._cache:
            self._cache[cache_key] = demo_offered_slots(service_slug, now=self._now)
        taken_ids = self._taken(tenant_id)
        return tuple(slot for slot in self._cache[cache_key] if slot.id not in taken_ids)
