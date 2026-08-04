"""Handoff persistence, and the conversation state that has to move with it."""

from __future__ import annotations

import asyncio

import psycopg
import pytest

from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresHandoffStore,
)
from tenantchat.api.registry import TenantRegistry
from tenantchat.core.commands import HandoffCommand, HandoffReason

TEST_POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=5)


def psycopg_url(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


async def _database(database_url: str) -> Database:
    database = Database.connect(database_url, TEST_POOL)
    registry = TenantRegistry.seeded()
    await database.synchronize_tenants(
        (record.policy.tenant_id, record.policy.name) for record in registry.all().values()
    )
    return database


def command(**overrides: str) -> HandoffCommand:
    policy = TenantRegistry.seeded().get("apex").policy
    arguments = {
        "reason": "customer_request",
        "summary": "Customer asked to speak to a person about a warranty claim.",
    } | overrides
    return HandoffCommand.parse(policy, **arguments)


@pytest.mark.integration
def test_a_handoff_moves_the_conversation_out_of_the_assistant_s_hands(
    repository_database_url: str,
) -> None:
    """The ticket and the session state change together, or neither does.

    A queued handoff whose session still reads ``active`` is a conversation the
    assistant would keep answering while a staff member believes they own it.
    Committing both in one transaction is what makes that unrepresentable.
    """

    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            record = await PostgresHandoffStore(database.engine).record(
                command(), session_id="visitor-correlation"
            )
        finally:
            await database.dispose()

        assert record.reason is HandoffReason.CUSTOMER_REQUEST
        assert record.handoff_id.startswith("HO-")

        with psycopg.connect(psycopg_url(repository_database_url)) as connection:
            row = connection.execute(
                """
                SELECT s.status, s.outcome, h.summary
                FROM chat_sessions s JOIN handoffs h ON h.chat_session_id = s.id
                WHERE s.tenant_id = 'apex'
                """
            ).fetchone()

        assert row is not None
        assert row[0] == "waiting_for_staff"
        assert row[1] == "handoff"
        assert row[2] == command().summary

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_handoff_is_readable_only_within_its_tenant(
    repository_database_url: str,
) -> None:
    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresHandoffStore(database.engine)
            recorded = await store.record(command(), session_id="visitor-correlation")

            assert await store.for_tenant("apex") == (recorded,)
            assert await store.for_tenant("clearview") == ()
        finally:
            await database.dispose()

    asyncio.run(scenario())
