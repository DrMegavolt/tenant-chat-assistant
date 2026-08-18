"""System-of-record behavior across pools, processes, restarts, and tenants."""

from __future__ import annotations

import asyncio
import multiprocessing
import tempfile
import uuid
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor

import psycopg
import pytest
from fastapi.testclient import TestClient

from tenantchat.api.app import create_app
from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresBookingStore,
    PostgresConversationStore,
    PostgresLeadStore,
)
from tenantchat.api.persistence.availability import (
    PostgresAvailabilityProvider,
    seed_demo_availability,
)
from tenantchat.api.registry import TenantRegistry, demo_offered_slots
from tenantchat.api.settings import Settings
from tenantchat.api.store import BookingAttempt, MessageRole
from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER
from tenantchat.core.commands import BookingCommand, LeadCommand
from tenantchat.core.errors import NotFoundError, SlotUnavailableError
from tenantchat.core.ports import IdempotencyKey
from tenantchat.orchestration.model import (
    AssembledPrompt,
    ModelResponse,
    ToolCall,
    ToolSpec,
)

TEST_POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=2)

BOOK_MESSAGE = "Please book my appointment."
LEAD_MESSAGE = "Please call me back."


class _ActionModel:
    """Proposes the action the visitor message names, and nothing else.

    The graph is the only ingress that writes a booking or a lead since
    `BUG-021` retired the direct routes, so a test about what the production
    composition *persists* has to go through it.
    """

    async def complete(
        self, prompt: AssembledPrompt, *, tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        message = prompt.messages[-1].content
        offered = {tool.name for tool in tools}
        if BOOK_MESSAGE in message and "book_appointment" in offered:
            return ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        call_id="call-book_appointment",
                        name="book_appointment",
                        arguments={
                            "service": "HVAC",
                            "slot": demo_offered_slots("hvac")[0].label,
                            "customer_name": "Dana Ruiz",
                            "customer_phone_or_email": "555-222-1919",
                            "address": "12 Alder Court, Portland, OR 97205",
                        },
                    ),
                ),
                model_name="scripted",
            )
        if LEAD_MESSAGE in message and "create_lead" in offered:
            return ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        call_id="call-create_lead",
                        name="create_lead",
                        arguments={
                            "customer_name": "Dana Ruiz",
                            "customer_phone_or_email": "dana@example.com",
                            "service": "HVAC",
                            "summary": "Furnace is making a grinding noise.",
                        },
                    ),
                ),
                model_name="scripted",
            )
        return ModelResponse(content="Noted, thank you.", model_name="scripted")


def _open(client: TestClient, tenant_id: str, purposes: list[str]) -> dict[str, str]:
    """A consented conversation, returning the header that names it."""
    opened = client.post("/api/chat/session", json={"tenant_id": tenant_id}).json()
    headers = {VISITOR_CREDENTIAL_HEADER: opened["credential"]}
    granted = client.post("/api/chat/consent", json={"purposes": purposes}, headers=headers)
    assert granted.status_code == 200, granted.text
    return headers


def _psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


async def _database(database_url: str, *, pool: DatabasePoolSettings = TEST_POOL) -> Database:
    database = Database.connect(database_url, pool)
    registry = TenantRegistry.seeded()
    await database.synchronize_tenants(
        (record.policy.tenant_id, record.policy.name) for record in registry.all().values()
    )
    # The booking reservation reads the database-backed fake calendar, so the
    # same seed production composition runs must be present for the tests too.
    await seed_demo_availability(database.engine, registry)
    return database


def _append_worker(
    database_url: str, tenant_id: str, session_id: str, ordinal: int
) -> tuple[int, str]:
    async def append() -> tuple[int, str]:
        database = Database.connect(
            database_url,
            DatabasePoolSettings(size=1, max_overflow=0, timeout_seconds=5),
        )
        try:
            message = await PostgresConversationStore(database.engine).append(
                tenant_id,
                uuid.UUID(session_id),
                role=MessageRole.VISITOR,
                content=f"message-{ordinal}",
            )
            return message.sequence_number, message.content
        finally:
            await database.dispose()

    return asyncio.run(append())


@pytest.mark.integration
def test_production_composition_persists_current_api_writes(
    repository_database_url: str,
) -> None:
    settings = Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=2048,
        docs_enabled=False,
        database_url=repository_database_url,
        database_pool_size=2,
        database_max_overflow=0,
        admin_gateway_token="gateway-token-for-tests",
        admin_csrf_secret="csrf-secret-for-tests",
        visitor_credential_signing_key="visitor-signing-key-for-tests-" + "x" * 16,
        # RAG-002: production composition requires the isolated upload root.
        ingestion_storage_root=tempfile.mkdtemp(prefix="tenantchat-ingestion-"),
    )
    with TestClient(create_app(settings, chat_model=_ActionModel())) as client:
        apex = _open(client, "apex", ["follow_up"])
        clearview = _open(client, "clearview", ["booking", "follow_up"])

        # Both actions pause for the visitor's confirmation before committing
        # (`AGENT-001`), so each takes a turn and then an approval.
        for headers, message in ((apex, LEAD_MESSAGE), (clearview, BOOK_MESSAGE)):
            proposed = client.post("/api/chat", json={"message": message}, headers=headers)
            assert proposed.status_code == 200, proposed.text
            assert proposed.json()["pending"], proposed.text
            approved = client.post(
                "/api/chat/confirmation", json={"decision": "approved"}, headers=headers
            )
            assert approved.status_code == 200, approved.text
            assert approved.json()["committed"], approved.text

    with psycopg.connect(_psycopg_url(repository_database_url)) as connection:
        counts = connection.execute(
            """
            SELECT
                (SELECT count(*) FROM leads WHERE tenant_id = 'apex'),
                (SELECT count(*) FROM bookings WHERE tenant_id = 'clearview')
            """
        ).fetchone()
    assert counts == (1, 1)


@pytest.mark.integration
def test_two_instances_share_state_and_restart_loses_nothing(
    repository_database_url: str,
) -> None:
    async def scenario() -> None:
        first_database = await _database(repository_database_url)
        second_database = await _database(repository_database_url)
        try:
            first = PostgresConversationStore(first_database.engine)
            second = PostgresConversationStore(second_database.engine)
            conversation = await first.create("apex")
            committed = await first.append(
                "apex",
                conversation.session_id,
                role=MessageRole.VISITOR,
                content="The furnace stopped.",
            )

            observed = await second.transcript("apex", conversation.session_id)
            assert observed == (committed,)
        finally:
            await first_database.dispose()
            await second_database.dispose()

        restarted_database = await _database(repository_database_url)
        try:
            restarted = PostgresConversationStore(restarted_database.engine)
            persisted = await restarted.get("apex", conversation.session_id)
            transcript = await restarted.transcript("apex", conversation.session_id)
            assert persisted.version == 2
            assert transcript[0].content == "The furnace stopped."
        finally:
            await restarted_database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_wrong_tenant_cannot_read_or_append_known_session_id(
    repository_database_url: str,
) -> None:
    async def scenario() -> None:
        database = await _database(repository_database_url)
        store = PostgresConversationStore(database.engine)
        try:
            conversation = await store.create("apex")
            original = await store.append(
                "apex",
                conversation.session_id,
                role=MessageRole.STAFF,
                content="A technician will follow up.",
            )

            with pytest.raises(NotFoundError):
                await store.get("clearview", conversation.session_id)
            with pytest.raises(NotFoundError):
                await store.transcript("clearview", conversation.session_id)
            with pytest.raises(NotFoundError):
                await store.append(
                    "clearview",
                    conversation.session_id,
                    role=MessageRole.VISITOR,
                    content="replace the staff answer",
                )

            assert await store.transcript("apex", conversation.session_id) == (original,)
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_the_operator_listing_shows_only_conversations_with_something_in_them(
    repository_database_url: str,
) -> None:
    """The console lists work, and an empty row is not work.

    The same table holds the write-only rows a booking or a lead correlates
    against, so without the filter an operator's queue fills with conversations
    that have no transcript and never had a visitor.
    """

    async def scenario() -> None:
        database = await _database(repository_database_url)
        store = PostgresConversationStore(database.engine)
        try:
            spoken = await store.create("apex")
            await store.create("apex")
            other_tenant = await store.create("clearview")
            await store.append(
                "apex", spoken.session_id, role=MessageRole.VISITOR, content="I need HVAC help."
            )
            await store.append(
                "clearview",
                other_tenant.session_id,
                role=MessageRole.VISITOR,
                content="Window cleaning?",
            )

            listed = await store.for_tenant("apex", limit=50)

            assert [record.session_id for record in listed] == [spoken.session_id]
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_the_operator_listing_is_ordered_and_bounded(repository_database_url: str) -> None:
    async def scenario() -> None:
        database = await _database(repository_database_url)
        store = PostgresConversationStore(database.engine)
        try:
            conversations = []
            for ordinal in range(3):
                conversation = await store.create("apex")
                await store.append(
                    "apex",
                    conversation.session_id,
                    role=MessageRole.VISITOR,
                    content=f"message {ordinal}",
                )
                conversations.append(conversation.session_id)

            listed = await store.for_tenant("apex", limit=2)

            assert [record.session_id for record in listed] == list(reversed(conversations))[:2]
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_transcript_is_server_appended_and_prior_messages_are_immutable(
    repository_database_url: str,
) -> None:
    async def scenario() -> None:
        database = await _database(repository_database_url)
        store = PostgresConversationStore(database.engine)
        try:
            conversation = await store.create("apex")
            visitor = await store.append(
                "apex",
                conversation.session_id,
                role=MessageRole.VISITOR,
                content="I need HVAC help.",
                metadata={"channel": "widget"},
            )
            assistant = await store.append(
                "apex",
                conversation.session_id,
                role=MessageRole.ASSISTANT,
                content="I can help collect the details.",
            )

            visitor.metadata["channel"] = "tampered-local-copy"
            transcript = await store.transcript("apex", conversation.session_id)
            assert [message.message_id for message in transcript] == [
                visitor.message_id,
                assistant.message_id,
            ]
            assert [message.sequence_number for message in transcript] == [1, 2]
            assert transcript[0].content == "I need HVAC help."
            assert transcript[0].metadata == {"channel": "widget"}
            assert not hasattr(store, "replace_transcript")
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_lead_and_booking_writes_are_durable_and_tenant_scoped(
    repository_database_url: str,
) -> None:
    async def scenario() -> None:
        registry = TenantRegistry.seeded()
        apex = registry.get("apex")
        clearview = registry.get("clearview")
        lead_command = LeadCommand.parse(
            apex.policy,
            customer_name="Dana Ruiz",
            contact="dana@example.com",
            service="HVAC",
            summary="Furnace is making a grinding noise.",
            urgency="this_week",
        )

        writer_database = await _database(repository_database_url)
        try:
            offers = await PostgresAvailabilityProvider(writer_database.engine).offered_slots(
                "clearview", "hvac"
            )
            booking_command = BookingCommand.parse(
                clearview.policy,
                customer_name="Dana Ruiz",
                contact="555-222-1919",
                address="12 Alder Court, Portland, OR 97205",
                service="HVAC",
                slot=offers[0].label,
                offered_slots=offers,
            )
            lead = await PostgresLeadStore(writer_database.engine).record(
                lead_command, session_id="visitor-correlation"
            )
            booking = await PostgresBookingStore(writer_database.engine).confirm(
                booking_command,
                session_id="visitor-correlation",
                attempt=BookingAttempt(
                    tenant_id="clearview",
                    scope="booking",
                    key=IdempotencyKey.derive("clearview", "visitor-correlation", "book", "1"),
                    request_hash="a" * 64,
                ),
            )
        finally:
            await writer_database.dispose()

        reader_database = await _database(repository_database_url)
        try:
            leads = PostgresLeadStore(reader_database.engine)
            bookings = PostgresBookingStore(reader_database.engine)
            assert await leads.for_tenant("apex") == (lead,)
            assert await bookings.for_tenant("clearview") == (booking.record,)
            assert await leads.for_tenant("clearview") == ()
            assert await bookings.for_tenant("apex") == ()
            assert uuid.UUID(lead.session_id) != uuid.UUID(booking.record.session_id)
        finally:
            await reader_database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_two_concurrent_attempts_on_one_slot_confirm_exactly_one(
    repository_database_url: str,
) -> None:
    """The durable reservation lets exactly one confirmed booking per slot.

    Two customers who read the same window and submit at once race on the unique
    confirmed-per-slot index. The loser surfaces a stable slot conflict, and the
    winner's booking is the only one the store holds.
    """

    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            clearview = TenantRegistry.seeded().get("clearview")
            offers = await PostgresAvailabilityProvider(database.engine).offered_slots(
                "clearview", "hvac"
            )
            command = BookingCommand.parse(
                clearview.policy,
                customer_name="Dana Ruiz",
                contact="555-222-1919",
                address="12 Alder Court, Portland, OR 97205",
                service="HVAC",
                slot=offers[0].label,
                offered_slots=offers,
            )

            def attempt(session: str, name: str) -> BookingAttempt:
                return BookingAttempt(
                    tenant_id="clearview",
                    scope="booking",
                    key=IdempotencyKey.derive("clearview", session, "book", "1"),
                    request_hash=name * 64,
                )

            store = PostgresBookingStore(database.engine)
            first, second = await asyncio.gather(
                store.confirm(command, session_id="session-a", attempt=attempt("session-a", "a")),
                store.confirm(command, session_id="session-b", attempt=attempt("session-b", "b")),
                return_exceptions=True,
            )

            outcomes = [r for r in (first, second) if not isinstance(r, BaseException)]
            refusals = [r for r in (first, second) if isinstance(r, SlotUnavailableError)]
            assert len(outcomes) == 1
            assert len(refusals) == 1
            assert await store.for_tenant("clearview") == (outcomes[0].record,)
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_repeating_a_key_after_the_slot_is_taken_replays_the_original(
    repository_database_url: str,
) -> None:
    """A retried key returns the original booking once the slot it owns is gone.

    Without the replay-before-revalidation step this would fail: after the
    first attempt books the window, that slot no longer reads as offered, so a
    second submission would be refused as if it were a brand-new booking.
    """

    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            clearview = TenantRegistry.seeded().get("clearview")
            offers = await PostgresAvailabilityProvider(database.engine).offered_slots(
                "clearview", "hvac"
            )
            command = BookingCommand.parse(
                clearview.policy,
                customer_name="Dana Ruiz",
                contact="555-222-1919",
                address="12 Alder Court, Portland, OR 97205",
                service="HVAC",
                slot=offers[0].label,
                offered_slots=offers,
            )
            attempt = BookingAttempt(
                tenant_id="clearview",
                scope="booking",
                key=IdempotencyKey.derive("clearview", "session-a", "book", "1"),
                request_hash="a" * 64,
            )
            store = PostgresBookingStore(database.engine)
            first = await store.confirm(command, session_id="session-a", attempt=attempt)
            second = await store.confirm(command, session_id="session-a", attempt=attempt)

            assert first.replayed is False
            assert second.replayed is True
            assert second.record == first.record
            assert await store.replay("clearview", "booking", attempt.key.value) == first.record
            assert len(await store.for_tenant("clearview")) == 1
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_slot_from_another_tenant_cannot_be_booked_here(
    repository_database_url: str,
) -> None:
    """The model cannot bind another tenant's window onto this tenant's booking.

    Availability is scoped to the tenant, so a cross-tenant slot is never
    offered to `clearview`; even if a caller fabricates the reference, the
    store's reservation lookup finds no such slot under this tenant.
    """

    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            apex_offers = await PostgresAvailabilityProvider(database.engine).offered_slots(
                "apex", "hvac"
            )
            clearview = TenantRegistry.seeded().get("clearview")
            forged = BookingCommand.parse(
                clearview.policy,
                customer_name="Dana Ruiz",
                contact="555-222-1919",
                address="12 Alder Court, Portland, OR 97205",
                service="HVAC",
                slot=apex_offers[0].label,
                offered_slots=apex_offers,
            )
            store = PostgresBookingStore(database.engine)
            with pytest.raises(SlotUnavailableError):
                await store.confirm(
                    forged,
                    session_id="session-a",
                    attempt=BookingAttempt(
                        tenant_id="clearview",
                        scope="booking",
                        key=IdempotencyKey.derive("clearview", "session-a", "book", "1"),
                        request_hash="a" * 64,
                    ),
                )
            assert await store.for_tenant("clearview") == ()
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_multi_process_concurrent_appends_have_total_order_and_no_lost_messages(
    repository_database_url: str,
) -> None:
    async def create_conversation() -> uuid.UUID:
        database = await _database(repository_database_url)
        try:
            return (await PostgresConversationStore(database.engine).create("apex")).session_id
        finally:
            await database.dispose()

    session_id = asyncio.run(create_conversation())
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        futures = [
            executor.submit(
                _append_worker,
                repository_database_url,
                "apex",
                str(session_id),
                ordinal,
            )
            for ordinal in range(24)
        ]
        committed = [future.result(timeout=30) for future in futures]

    async def verify() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresConversationStore(database.engine)
            transcript = await store.transcript("apex", session_id)
            conversation = await store.get("apex", session_id)
            assert sorted(sequence for sequence, _content in committed) == list(range(1, 25))
            assert [message.sequence_number for message in transcript] == list(range(1, 25))
            assert {message.content for message in transcript} == {
                f"message-{ordinal}" for ordinal in range(24)
            }
            assert conversation.version == 25
        finally:
            await database.dispose()

    asyncio.run(verify())
