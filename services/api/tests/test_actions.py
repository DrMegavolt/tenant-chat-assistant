"""Idempotent application services: what a retry is allowed to change.

These are the guarantees the graph relies on, specified without a graph. A node
is replayed after a crash, a resume, or a deployment; the services below are the
reason that costs nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from tenantchat.api.actions import (
    RecordedBookingService,
    RecordedHandoffService,
    RecordedLeadService,
)
from tenantchat.api.persistence.repositories import (
    _booking_integrity_error,
    _ReservationLostError,
)
from tenantchat.api.registry import DemoAvailabilityProvider, TenantRegistry, demo_offered_slots
from tenantchat.api.store import (
    InMemoryBookingStore,
    InMemoryConsentStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryLeadStore,
)
from tenantchat.core.commands import BookingCommand, HandoffCommand, LeadCommand
from tenantchat.core.errors import ConflictError, NotFoundError, SlotUnavailableError
from tenantchat.core.ports import IdempotencyKey
from tenantchat.core.privacy import ConsentPurpose
from tenantchat.core.slots import OfferedSlot

BOOKING_TENANT = "clearview"
LEAD_TENANT = "apex"
SESSION = "session-1"
KEY = IdempotencyKey.derive("clearview", SESSION, "book_appointment", "1", "call-1")

_OFFERS = demo_offered_slots("hvac")
_OFFERED_LABEL = _OFFERS[0].label
_OTHER_LABEL = _OFFERS[1].label
_BOOKING_PURPOSES = frozenset({ConsentPurpose.BOOKING, ConsentPurpose.FOLLOW_UP})
_LEAD_PURPOSES = frozenset({ConsentPurpose.FOLLOW_UP})


def granted_consent(tenant_id: str, session_id: str = SESSION) -> InMemoryConsentStore:
    """A consent store already holding a grant for the session and its action.

    Consent is a precondition, not the thing under test: tests that are about
    idempotency grant it up front so a refusal cannot mask a missing one.
    """
    store = InMemoryConsentStore()
    purposes = _BOOKING_PURPOSES if tenant_id == BOOKING_TENANT else _LEAD_PURPOSES

    async def grant() -> None:
        await store.record(tenant_id, session_id, purposes=purposes, statement="test")

    asyncio.run(grant())
    return store


def booking_command(**overrides: str) -> BookingCommand:
    record = TenantRegistry.seeded().get(BOOKING_TENANT)
    arguments = {
        "customer_name": "Dana Ruiz",
        "contact": "555-222-1919",
        "address": "12 Alder Court, Portland, OR 97205",
        "service": "HVAC",
        "slot": _OFFERED_LABEL,
    } | overrides
    return BookingCommand.parse(record.policy, offered_slots=_OFFERS, **arguments)


def booking_command_from(offers: tuple[OfferedSlot, ...], **overrides: str) -> BookingCommand:
    """Parse against a caller-supplied offer set, so slot IDs line up with the provider."""
    record = TenantRegistry.seeded().get(BOOKING_TENANT)
    arguments = {
        "customer_name": "Dana Ruiz",
        "contact": "555-222-1919",
        "address": "12 Alder Court, Portland, OR 97205",
        "service": "HVAC",
        "slot": offers[0].label,
    } | overrides
    return BookingCommand.parse(record.policy, offered_slots=offers, **arguments)


def lead_command(**overrides: str) -> LeadCommand:
    policy = TenantRegistry.seeded().get(LEAD_TENANT).policy
    arguments = {
        "customer_name": "Dana Ruiz",
        "contact": "dana@example.com",
        "service": "HVAC",
        "summary": "Furnace is making a grinding noise.",
    } | overrides
    return LeadCommand.parse(policy, **arguments)


def handoff_command(**overrides: str) -> HandoffCommand:
    policy = TenantRegistry.seeded().get(LEAD_TENANT).policy
    arguments = {"reason": "customer_request", "summary": "Asked for a person."} | overrides
    return HandoffCommand.parse(policy, **arguments)


@pytest.fixture
def bookings() -> Callable[[], tuple[RecordedBookingService, InMemoryBookingStore]]:
    def build() -> tuple[RecordedBookingService, InMemoryBookingStore]:
        store = InMemoryBookingStore()
        provider = DemoAvailabilityProvider(TenantRegistry.seeded(), taken=store.taken_slot_ids)
        service = RecordedBookingService(store, provider, granted_consent(BOOKING_TENANT))
        return service, store

    return build


class TestBookingIdempotency:
    def test_a_retry_returns_the_original_booking_and_writes_nothing(
        self, bookings: Callable[[], tuple[RecordedBookingService, InMemoryBookingStore]]
    ) -> None:
        service, store = bookings()

        async def scenario() -> None:
            first = await service.confirm(
                booking_command(), session_id=SESSION, idempotency_key=KEY
            )
            second = await service.confirm(
                booking_command(), session_id=SESSION, idempotency_key=KEY
            )

            assert first.reference == second.reference
            assert first.replayed is False
            assert second.replayed is True
            assert len(await store.for_tenant(BOOKING_TENANT)) == 1

        asyncio.run(scenario())

    def test_the_service_instance_is_not_what_remembers(self) -> None:
        """Idempotency lives in the store, so a restarted process still honours it.

        A replay after a deployment reaches a service object that has never seen
        the key. If the guard were per-instance, that is exactly when it would
        stop working.
        """
        store = InMemoryBookingStore()
        provider = DemoAvailabilityProvider(TenantRegistry.seeded(), taken=store.taken_slot_ids)
        consent = granted_consent(BOOKING_TENANT)

        async def scenario() -> None:
            await RecordedBookingService(store, provider, consent).confirm(
                booking_command(), session_id=SESSION, idempotency_key=KEY
            )
            replayed = await RecordedBookingService(store, provider, consent).confirm(
                booking_command(), session_id=SESSION, idempotency_key=KEY
            )

            assert replayed.replayed is True
            assert len(await store.for_tenant(BOOKING_TENANT)) == 1

        asyncio.run(scenario())

    def test_a_different_booking_under_the_same_key_is_refused(
        self, bookings: Callable[[], tuple[RecordedBookingService, InMemoryBookingStore]]
    ) -> None:
        """Committing the second one under the first one's identity would lose it.

        A recycled key is a caller bug. Returning the *first* booking silently
        would tell the customer their new address was accepted when the crew is
        going to the old one.
        """
        service, store = bookings()

        async def scenario() -> None:
            await service.confirm(booking_command(), session_id=SESSION, idempotency_key=KEY)

            with pytest.raises(ConflictError):
                await service.confirm(
                    booking_command(address="99 Other Street, Portland, OR 97205"),
                    session_id=SESSION,
                    idempotency_key=KEY,
                )

            assert len(await store.for_tenant(BOOKING_TENANT)) == 1

        asyncio.run(scenario())

    def test_two_sessions_with_different_keys_on_different_slots_both_book(
        self, bookings: Callable[[], tuple[RecordedBookingService, InMemoryBookingStore]]
    ) -> None:
        """The guard must not turn two real customers into one."""
        store = InMemoryBookingStore()
        consent = InMemoryConsentStore()

        async def grant() -> None:
            await consent.record(
                BOOKING_TENANT, SESSION, purposes=_BOOKING_PURPOSES, statement="test"
            )
            await consent.record(
                BOOKING_TENANT, "session-2", purposes=_BOOKING_PURPOSES, statement="test"
            )

        asyncio.run(grant())
        provider = DemoAvailabilityProvider(TenantRegistry.seeded(), taken=store.taken_slot_ids)
        service = RecordedBookingService(store, provider, consent)
        other = IdempotencyKey.derive("clearview", "session-2", "book_appointment", "1", "call-1")

        async def scenario() -> None:
            await service.confirm(booking_command(), session_id=SESSION, idempotency_key=KEY)
            await service.confirm(
                booking_command(slot=_OTHER_LABEL),
                session_id="session-2",
                idempotency_key=other,
            )

            assert len(await store.for_tenant(BOOKING_TENANT)) == 2

        asyncio.run(scenario())

    def test_two_concurrent_attempts_on_one_slot_produce_one_booking(self) -> None:
        """`DATA-003` acceptance: exactly one confirmed booking per slot.

        Two customers reading the same availability and submitting at once race
        for the slot; the uniqueness reservation lets exactly one through and
        the loser gets a stable ``slot_unavailable`` with the refreshed offers,
        from which the just-taken slot is gone.
        """
        store = InMemoryBookingStore()
        provider = DemoAvailabilityProvider(TenantRegistry.seeded(), taken=store.taken_slot_ids)
        consent = granted_consent(BOOKING_TENANT)
        asyncio.run(
            consent.record(
                BOOKING_TENANT, "session-2", purposes=_BOOKING_PURPOSES, statement="test"
            )
        )
        service = RecordedBookingService(store, provider, consent)
        other = IdempotencyKey.derive("clearview", "session-2", "book_appointment", "1", "call-2")

        async def scenario() -> None:
            offers = await provider.offered_slots(BOOKING_TENANT, "hvac")
            first, second = await asyncio.gather(
                service.confirm(
                    booking_command_from(offers), session_id=SESSION, idempotency_key=KEY
                ),
                service.confirm(
                    booking_command_from(offers),
                    session_id="session-2",
                    idempotency_key=other,
                ),
                return_exceptions=True,
            )

            outcomes = [r for r in (first, second) if not isinstance(r, BaseException)]
            refusals = [r for r in (first, second) if isinstance(r, SlotUnavailableError)]
            assert len(outcomes) == 1
            assert len(refusals) == 1
            assert refusals[0].offered and offers[0].label not in refusals[0].offered
            assert len(await store.for_tenant(BOOKING_TENANT)) == 1

        asyncio.run(scenario())


class TestBookingConfirmationEcho:
    """What the confirmation reports back, now that no HTTP route projects it.

    These moved here when the direct `POST /api/book` route was retired. The graph's
    `commit_booking` node reads the same fields to tell the customer what was
    booked, so the guarantees outlive the route that used to serve them.
    """

    def test_distinct_bookings_get_distinct_references(
        self, bookings: Callable[[], tuple[RecordedBookingService, InMemoryBookingStore]]
    ) -> None:
        """A confirmation number is what a customer quotes; two must never collide.

        Distinct keys, so this is the opposite of the replay guarantee above:
        the same key must return one reference, and different keys must not.
        """
        service, _ = bookings()
        other = IdempotencyKey.derive("clearview", SESSION, "book_appointment", "2", "call-2")

        async def scenario() -> None:
            first = await service.confirm(
                booking_command(), session_id=SESSION, idempotency_key=KEY
            )
            second = await service.confirm(
                booking_command(slot=_OTHER_LABEL), session_id=SESSION, idempotency_key=other
            )

            assert first.reference != second.reference

        asyncio.run(scenario())

    def test_the_confirmation_echoes_the_contact_the_business_will_dial(
        self, bookings: Callable[[], tuple[RecordedBookingService, InMemoryBookingStore]]
    ) -> None:
        """Echoing the typed string would confirm a number nobody can reach."""
        service, _ = bookings()

        async def scenario() -> None:
            confirmation = await service.confirm(
                booking_command(contact="+1 (555) 222-1919"),
                session_id=SESSION,
                idempotency_key=KEY,
            )

            assert confirmation.contact == "(555) 222-1919"

        asyncio.run(scenario())

    def test_the_confirmation_names_the_service_the_catalog_resolved(
        self, bookings: Callable[[], tuple[RecordedBookingService, InMemoryBookingStore]]
    ) -> None:
        """ "a/c" is what the customer typed; "HVAC" is what was booked."""
        service, _ = bookings()

        async def scenario() -> None:
            confirmation = await service.confirm(
                booking_command(service="a/c"), session_id=SESSION, idempotency_key=KEY
            )

            assert confirmation.service_name == "HVAC"

        asyncio.run(scenario())


class TestLeadIdempotency:
    def test_a_retry_returns_the_original_lead(self) -> None:
        store = InMemoryLeadStore()
        service = RecordedLeadService(
            store, InMemoryIdempotencyStore(), granted_consent(LEAD_TENANT)
        )
        key = IdempotencyKey.derive("apex", SESSION, "create_lead", "1", "call-1")

        async def scenario() -> None:
            first = await service.capture(lead_command(), session_id=SESSION, idempotency_key=key)
            second = await service.capture(lead_command(), session_id=SESSION, idempotency_key=key)

            assert first.reference == second.reference
            assert second.replayed is True
            assert len(await store.for_tenant(LEAD_TENANT)) == 1

        asyncio.run(scenario())


class TestHandoffIdempotency:
    def test_a_retry_does_not_queue_a_second_ticket(self) -> None:
        """A staff queue with a duplicate for every resumed run stops being read."""
        store = InMemoryHandoffStore()
        service = RecordedHandoffService(store, InMemoryIdempotencyStore())
        key = IdempotencyKey.derive("apex", SESSION, "handoff_to_human", "1", "escalation")

        async def scenario() -> None:
            first = await service.request(
                handoff_command(), session_id=SESSION, idempotency_key=key
            )
            second = await service.request(
                handoff_command(), session_id=SESSION, idempotency_key=key
            )

            assert first.reference == second.reference
            assert second.replayed is True
            assert len(await store.for_tenant(LEAD_TENANT)) == 1

        asyncio.run(scenario())


class TestIdempotencyStoreContract:
    def test_an_unclaimed_key_cannot_be_completed(self) -> None:
        """Completing without claiming would mean the claim never guarded anything."""
        store = InMemoryIdempotencyStore()

        async def scenario() -> None:
            with pytest.raises(NotFoundError):
                await store.complete(
                    BOOKING_TENANT, scope="booking", key=KEY, response={"reference": "BK-1"}
                )

        asyncio.run(scenario())

    def test_a_second_claim_before_completion_is_refused(self) -> None:
        """An attempt that crashed mid-flight must not be silently retried.

        Safe rather than convenient: the alternative is a second write while the
        first may still land. `DATA-003` removes the window for bookings by
        committing the claim and the booking in one transaction.
        """
        store = InMemoryIdempotencyStore()

        async def scenario() -> None:
            assert (
                await store.begin(BOOKING_TENANT, scope="booking", key=KEY, fingerprint="abc")
                is None
            )

            with pytest.raises(ConflictError):
                await store.begin(BOOKING_TENANT, scope="booking", key=KEY, fingerprint="abc")

        asyncio.run(scenario())

    def test_the_same_key_in_two_scopes_is_two_attempts(self) -> None:
        """Scope is part of the identity, so a lead and a booking never collide."""
        store = InMemoryIdempotencyStore()

        async def scenario() -> None:
            assert (
                await store.begin(BOOKING_TENANT, scope="booking", key=KEY, fingerprint="abc")
                is None
            )
            assert (
                await store.begin(BOOKING_TENANT, scope="lead", key=KEY, fingerprint="abc") is None
            )

        asyncio.run(scenario())

    def test_the_same_key_in_two_tenants_is_two_attempts(self) -> None:
        """Nothing one tenant sends may reach another tenant's committed action."""
        store = InMemoryIdempotencyStore()

        async def scenario() -> None:
            assert (
                await store.begin(BOOKING_TENANT, scope="booking", key=KEY, fingerprint="abc")
                is None
            )
            assert (
                await store.begin(LEAD_TENANT, scope="booking", key=KEY, fingerprint="abc") is None
            )

        asyncio.run(scenario())


class _FakeDriverError(Exception):
    """A stand-in for asyncpg's error object: the constraint name lives in `diag`."""

    def __init__(self, constraint: str) -> None:
        super().__init__(constraint)
        self.diag = SimpleNamespace(constraint_name=constraint)


class TestBookingIntegrityDiscrimination:
    """R-41: not every booking-insert IntegrityError is a slot race.

    The PostgreSQL insert can fail on several constraints; only the one-slot
    rule (and a slot row that vanished mid-transaction) means "someone else got
    the slot". Relabelling a session foreign-key failure as a slot race told a
    visitor to retry a conversation that does not exist, and an unknown
    constraint is schema drift that must surface as the database error it is."""

    @staticmethod
    def _integrity_error(constraint: str) -> IntegrityError:
        # The DBAPI exception SQLAlchemy wraps carries the constraint name in
        # its `diag`; the fake stands in for asyncpg's error object.
        return IntegrityError("INSERT INTO bookings", {}, _FakeDriverError(constraint))

    def test_the_one_booking_per_slot_rule_is_a_lost_slot_race(self) -> None:
        error = self._integrity_error("uq_bookings_one_confirmed_per_slot")
        mapped = _booking_integrity_error(error, tenant_id="t1", service_slug="hvac")
        assert isinstance(mapped, _ReservationLostError)
        assert mapped.tenant_id == "t1"
        assert mapped.service_slug == "hvac"

    def test_a_vanished_slot_row_is_a_lost_slot_race(self) -> None:
        error = self._integrity_error("fk_bookings_slot")
        mapped = _booking_integrity_error(error, tenant_id="t1", service_slug="hvac")
        assert isinstance(mapped, _ReservationLostError)

    def test_a_session_foreign_key_failure_is_not_a_slot_race(self) -> None:
        error = self._integrity_error("fk_bookings_session")
        mapped = _booking_integrity_error(error, tenant_id="t1", service_slug="hvac")
        assert isinstance(mapped, NotFoundError)
        assert mapped.detail is not None
        assert "slot" not in mapped.detail.lower()

    def test_an_unknown_constraint_surfaces_as_the_database_error(self) -> None:
        """A constraint this build does not know about means the schema moved
        without the mapping; silently calling it a slot race would hide that."""
        error = self._integrity_error("fk_bookings_future_constraint")
        mapped = _booking_integrity_error(error, tenant_id="t1", service_slug="hvac")
        assert mapped is error
