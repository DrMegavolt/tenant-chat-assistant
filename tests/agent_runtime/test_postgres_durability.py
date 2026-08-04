"""The durability claim, checked against PostgreSQL instead of a dictionary.

The hermetic suite proves the runtime keeps nothing important in the objects it
builds. This one proves the thing that actually gets deployed: a conversation
paused by one process, with its own connection pool, is resumed by another that
shares nothing with it but the database.

It also pins the two operational facts a runbook depends on — the checkpoint
schema is created by ``make migrate-checkpoints`` and not at runtime, and
truncating every checkpoint table leaves the business records untouched.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tenantchat.api.agent import build_dispatch_runtime
from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresBookingStore,
    PostgresHandoffStore,
    PostgresIdempotencyStore,
    PostgresLeadStore,
)
from tenantchat.api.registry import TenantRegistry
from tenantchat.orchestration.checkpoints import (
    checkpoint_connection_string,
    postgres_checkpointer,
)
from tenantchat.orchestration.model import ModelResponse
from tenantchat.orchestration.runtime import DispatchRuntime, thread_id
from tests.agent_runtime.conftest import (
    BOOKING_TENANT,
    OFFERED_SLOT,
    ScriptedModel,
    booking_arguments,
    tool_call,
)

pytestmark = pytest.mark.integration

CHECKPOINT_TABLES = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")
TEST_POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=5)


def psycopg_url(sqlalchemy_url: str) -> str:
    return checkpoint_connection_string(sqlalchemy_url)


def proposal() -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=(tool_call("book_appointment", **booking_arguments()),),
        model_name="scripted",
    )


def confirmation() -> ModelResponse:
    return ModelResponse(content="You are booked for Monday at 2pm.", model_name="scripted")


@asynccontextmanager
async def _process(
    database_url: str, script: list[ModelResponse]
) -> AsyncIterator[DispatchRuntime]:
    """One runtime with its own pools, as a deployment would build it."""
    database = Database.connect(database_url, TEST_POOL)
    registry = TenantRegistry.seeded()
    await database.synchronize_tenants(
        (record.policy.tenant_id, record.policy.name) for record in registry.all().values()
    )
    try:
        async with postgres_checkpointer(database_url) as checkpointer:
            yield build_dispatch_runtime(
                registry=registry,
                model=ScriptedModel(script=script),
                bookings=PostgresBookingStore(database.engine),
                leads=PostgresLeadStore(database.engine),
                handoffs=PostgresHandoffStore(database.engine),
                idempotency=PostgresIdempotencyStore(database.engine),
                checkpointer=checkpointer,
            )
    finally:
        await database.dispose()


async def _bookings(engine: AsyncEngine) -> int:
    return len(await PostgresBookingStore(engine).for_tenant(BOOKING_TENANT))


def test_a_conversation_paused_by_one_process_is_resumed_by_another(
    agent_database_url: str,
) -> None:
    """Nothing but PostgreSQL crosses between the two runtimes.

    Each ``_process`` block opens its own engine and its own checkpointer pool
    and disposes of both on the way out, so the second one starts with no
    in-memory knowledge of the first — which is what a rolling deployment does
    to a customer who is mid-booking.
    """

    async def scenario() -> None:
        async with _process(agent_database_url, [proposal(), confirmation()]) as first:
            paused = await first.send(BOOKING_TENANT, "pg-restart", "book HVAC Monday")
            assert paused.is_paused

        async with _process(agent_database_url, [confirmation()]) as second:
            assert await second.pending(BOOKING_TENANT, "pg-restart") is not None
            resumed = await second.resume(BOOKING_TENANT, "pg-restart", "approved")

        assert resumed.answer == "You are booked for Monday at 2pm."
        assert [action["action"] for action in resumed.committed] == ["book_appointment"]

        database = Database.connect(agent_database_url, TEST_POOL)
        try:
            booked = await PostgresBookingStore(database.engine).for_tenant(BOOKING_TENANT)
        finally:
            await database.dispose()

        assert len(booked) == 1
        assert booked[0].slot == OFFERED_SLOT

    asyncio.run(scenario())


def test_truncating_every_checkpoint_table_loses_no_business_record(
    agent_database_url: str,
) -> None:
    """The operational claim, stated as the command an operator would actually run."""

    async def commit_a_booking() -> None:
        async with _process(agent_database_url, [proposal(), confirmation()]) as runtime:
            await runtime.send(BOOKING_TENANT, "pg-wipe", "book HVAC Monday")
            await runtime.resume(BOOKING_TENANT, "pg-wipe", "approved")

    asyncio.run(commit_a_booking())

    with psycopg.connect(psycopg_url(agent_database_url), autocommit=True) as connection:
        connection.execute(f"TRUNCATE {', '.join(CHECKPOINT_TABLES)}")

    async def verify() -> None:
        database = Database.connect(agent_database_url, TEST_POOL)
        try:
            assert await _bookings(database.engine) == 1
        finally:
            await database.dispose()

        async with _process(
            agent_database_url, [ModelResponse(content="Open until 7pm.", model_name="scripted")]
        ) as runtime:
            assert await runtime.pending(BOOKING_TENANT, "pg-wipe") is None
            started = await runtime.send(BOOKING_TENANT, "pg-after-wipe", "hours?")
            assert started.answer == "Open until 7pm."

    asyncio.run(verify())


def test_a_checkpoint_thread_is_tenant_qualified_in_storage(agent_database_url: str) -> None:
    """The isolation is a property of the stored key, not of a lookup in the runtime."""

    async def scenario() -> None:
        async with _process(
            agent_database_url, [ModelResponse(content="Open until 7pm.", model_name="scripted")]
        ) as runtime:
            await runtime.send(BOOKING_TENANT, "shared", "hours?")

    asyncio.run(scenario())

    with psycopg.connect(psycopg_url(agent_database_url)) as connection:
        stored = connection.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()

    assert [row[0] for row in stored] == [thread_id(BOOKING_TENANT, "shared")]


def test_the_idempotency_record_never_stores_the_raw_key(agent_database_url: str) -> None:
    """A readable key column is a table that can be read to replay someone's booking."""

    async def scenario() -> None:
        async with _process(agent_database_url, [proposal(), confirmation()]) as runtime:
            await runtime.send(BOOKING_TENANT, "pg-keys", "book HVAC Monday")
            await runtime.resume(BOOKING_TENANT, "pg-keys", "approved")

    asyncio.run(scenario())

    with psycopg.connect(psycopg_url(agent_database_url)) as connection:
        rows = connection.execute(
            "SELECT scope, key_hash, status, response FROM idempotency_keys"
        ).fetchall()

    assert len(rows) == 1
    scope, key_hash, status, response = rows[0]
    assert scope == "booking"
    assert len(key_hash) == 64
    assert status == "completed"
    assert response["reference"].startswith("BK-")
