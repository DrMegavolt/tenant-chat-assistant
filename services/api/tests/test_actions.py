"""Idempotent application services: what a retry is allowed to change.

These are the guarantees the graph relies on, specified without a graph. A node
is replayed after a crash, a resume, or a deployment; the services below are the
reason that costs nothing.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from tenantchat.api.actions import (
    RecordedBookingService,
    RecordedHandoffService,
    RecordedLeadService,
)
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.store import (
    InMemoryBookingStore,
    InMemoryConsentStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryLeadStore,
)
from tenantchat.core.commands import BookingCommand, HandoffCommand, LeadCommand
from tenantchat.core.errors import ConflictError, NotFoundError
from tenantchat.core.ports import IdempotencyKey
from tenantchat.core.privacy import ConsentPurpose

BOOKING_TENANT = "clearview"
LEAD_TENANT = "apex"
SESSION = "session-1"
KEY = IdempotencyKey.derive("clearview", SESSION, "book_appointment", "1", "call-1")

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
        "slot": "Mon Jul 1, 2:00 PM",
    } | overrides
    return BookingCommand.parse(
        record.policy, offered_slots=record.offered_slots("hvac"), **arguments
    )


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
        consent = granted_consent(BOOKING_TENANT)
        return RecordedBookingService(store, InMemoryIdempotencyStore(), consent), store

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
        keys = InMemoryIdempotencyStore()
        consent = granted_consent(BOOKING_TENANT)

        async def scenario() -> None:
            await RecordedBookingService(store, keys, consent).confirm(
                booking_command(), session_id=SESSION, idempotency_key=KEY
            )
            replayed = await RecordedBookingService(store, keys, consent).confirm(
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

    def test_two_sessions_with_different_keys_both_book(
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
        service = RecordedBookingService(store, InMemoryIdempotencyStore(), consent)
        other = IdempotencyKey.derive("clearview", "session-2", "book_appointment", "1", "call-1")

        async def scenario() -> None:
            await service.confirm(booking_command(), session_id=SESSION, idempotency_key=KEY)
            await service.confirm(booking_command(), session_id="session-2", idempotency_key=other)

            assert len(await store.for_tenant(BOOKING_TENANT)) == 2

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
        first may still land. `DATA-003` removes the window by committing the
        claim and the booking in one transaction.
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
