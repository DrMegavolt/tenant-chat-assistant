"""System-of-record behavior across pools, processes, restarts, and tenants."""

from __future__ import annotations

import asyncio
import multiprocessing
import uuid
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
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.settings import Settings
from tenantchat.api.store import MessageRole
from tenantchat.core.commands import BookingCommand, LeadCommand
from tenantchat.core.errors import NotFoundError

TEST_POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=2)


def _psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


async def _database(database_url: str, *, pool: DatabasePoolSettings = TEST_POOL) -> Database:
    database = Database.connect(database_url, pool)
    registry = TenantRegistry.seeded()
    await database.synchronize_tenants(
        (record.policy.tenant_id, record.policy.name) for record in registry.all().values()
    )
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
    )
    with TestClient(create_app(settings)) as client:
        lead = client.post(
            "/api/leads",
            json={
                "tenant_id": "apex",
                "session_id": "api-correlation",
                "customer_name": "Dana Ruiz",
                "contact": "dana@example.com",
                "service": "HVAC",
                "summary": "Furnace is making a grinding noise.",
            },
        )
        booking = client.post(
            "/api/book",
            json={
                "tenant_id": "clearview",
                "session_id": "api-correlation",
                "customer_name": "Dana Ruiz",
                "contact": "555-222-1919",
                "service": "HVAC",
                "slot": "Mon Jul 1, 2:00 PM",
                "address": "12 Alder Court, Portland, OR 97205",
            },
        )
    assert lead.status_code == 201
    assert booking.status_code == 201

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
        booking_command = BookingCommand.parse(
            clearview.policy,
            customer_name="Dana Ruiz",
            contact="555-222-1919",
            address="12 Alder Court, Portland, OR 97205",
            service="HVAC",
            slot="Mon Jul 1, 2:00 PM",
            offered_slots=clearview.offered_slots("hvac"),
        )

        writer_database = await _database(repository_database_url)
        try:
            lead = await PostgresLeadStore(writer_database.engine).record(
                lead_command, session_id="visitor-correlation"
            )
            booking = await PostgresBookingStore(writer_database.engine).record(
                booking_command, session_id="visitor-correlation"
            )
        finally:
            await writer_database.dispose()

        reader_database = await _database(repository_database_url)
        try:
            leads = PostgresLeadStore(reader_database.engine)
            bookings = PostgresBookingStore(reader_database.engine)
            assert await leads.for_tenant("apex") == (lead,)
            assert await bookings.for_tenant("clearview") == (booking,)
            assert await leads.for_tenant("clearview") == ()
            assert await bookings.for_tenant("apex") == ()
            assert uuid.UUID(lead.session_id) != uuid.UUID(booking.session_id)
        finally:
            await reader_database.dispose()

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
