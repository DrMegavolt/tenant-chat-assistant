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
from tenantchat.core.errors import HandoffTransitionError
from tenantchat.core.handoffs import HandoffStatus

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


@pytest.mark.integration
def test_accept_moves_the_conversation_into_staff_hands_and_writes_the_notice(
    repository_database_url: str,
) -> None:
    """The takeover is visible to the visitor through the transcript, atomically.

    The session reads ``waiting_for_staff``, the handoff names one owner, and a
    server-authored system notice appears beside it — all in the accept's own
    transaction, so a crashed accept leaves none of it.
    """

    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresHandoffStore(database.engine)
            recorded = await store.record(command(), session_id="visitor-correlation")
            assigned = await store.accept(
                "apex", recorded.handoff_id, principal_id="operator-7"
            )
        finally:
            await database.dispose()

        assert assigned.status == HandoffStatus.ASSIGNED.value
        assert assigned.assigned_principal_id == "operator-7"
        assert assigned.assigned_at is not None

        with psycopg.connect(psycopg_url(repository_database_url)) as connection:
            session = connection.execute(
                "SELECT status FROM chat_sessions WHERE tenant_id = 'apex'"
            ).fetchone()
            notice = connection.execute(
                """
                SELECT content FROM messages
                WHERE tenant_id = 'apex' AND role = 'system'
                """
            ).fetchone()
        assert session is not None
        assert session[0] == "waiting_for_staff"
        assert notice is not None
        assert "joined" in notice[0]

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_race_to_accept_commits_exactly_one_owner_in_the_database(
    repository_database_url: str,
) -> None:
    """Two concurrent accepts have one winner; the loser sees a transition error.

    Both updates race on the same row. The database serializes them: the loser's
    conditional ``UPDATE`` re-checks the winner's committed assignment, matches
    nothing, and reads back the committed state for the refusal — the proof that
    ownership is decided by the database, not by a console.
    """

    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresHandoffStore(database.engine)
            recorded = await store.record(command(), session_id="visitor-correlation")

            outcomes = await asyncio.gather(
                store.accept("apex", recorded.handoff_id, principal_id="operator-7"),
                store.accept("apex", recorded.handoff_id, principal_id="operator-8"),
                return_exceptions=True,
            )

            committed = [o for o in outcomes if not isinstance(o, Exception)]
            refused = [o for o in outcomes if isinstance(o, HandoffTransitionError)]
            assert len(committed) == 1
            assert len(refused) == 1
            assert isinstance(refused[0], HandoffTransitionError)
            assert refused[0].current == "assigned"
        finally:
            await database.dispose()

        with psycopg.connect(psycopg_url(repository_database_url)) as connection:
            row = connection.execute(
                """
                SELECT assigned_principal_id, assigned_at, status
                FROM handoffs WHERE tenant_id = 'apex'
                """
            ).fetchone()

        assert row is not None
        assert row[0] in ("operator-7", "operator-8")
        assert row[1] is not None
        assert row[2] == "assigned"

    asyncio.run(scenario())


@pytest.mark.integration
def test_release_returns_the_conversation_to_the_queue_and_resumes_the_session(
    repository_database_url: str,
) -> None:
    """Release clears the owner, stamps it, and puts the session back to active.

    The agent may answer again after release, so the session must not read
    ``waiting_for_staff`` anymore — the handoff row still records the request.
    """

    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresHandoffStore(database.engine)
            recorded = await store.record(command(), session_id="visitor-correlation")
            await store.accept("apex", recorded.handoff_id, principal_id="operator-7")
            released = await store.release(
                "apex", recorded.handoff_id, principal_id="operator-7"
            )
        finally:
            await database.dispose()

        assert released.status == HandoffStatus.QUEUED.value
        assert released.assigned_principal_id is None
        assert released.assigned_at is None
        assert released.released_at is not None

        with psycopg.connect(psycopg_url(repository_database_url)) as connection:
            session_status = connection.execute(
                "SELECT status FROM chat_sessions WHERE tenant_id = 'apex'"
            ).fetchone()
        assert session_status is not None
        assert session_status[0] == "active"

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_non_owner_cannot_release_without_the_administrative_flag(
    repository_database_url: str,
) -> None:
    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresHandoffStore(database.engine)
            recorded = await store.record(command(), session_id="visitor-correlation")
            await store.accept("apex", recorded.handoff_id, principal_id="operator-7")

            with pytest.raises(HandoffTransitionError):
                await store.release("apex", recorded.handoff_id, principal_id="operator-8")
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_resolution_closes_the_session_and_audits_who_closed_it(
    repository_database_url: str,
) -> None:
    """Resolution is the terminal state: the session closes, the notice lands,
    and the closing principal is recorded on the handoff itself."""

    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresHandoffStore(database.engine)
            recorded = await store.record(command(), session_id="visitor-correlation")
            await store.accept("apex", recorded.handoff_id, principal_id="operator-7")
            resolved = await store.resolve(
                "apex", recorded.handoff_id, principal_id="operator-7"
            )
        finally:
            await database.dispose()

        assert resolved.status == HandoffStatus.RESOLVED.value
        assert resolved.resolved_by_principal_id == "operator-7"
        assert resolved.resolved_at is not None

        with psycopg.connect(psycopg_url(repository_database_url)) as connection:
            session = connection.execute(
                "SELECT status, closed_at FROM chat_sessions WHERE tenant_id = 'apex'"
            ).fetchone()
            notice = connection.execute(
                """
                SELECT content FROM messages
                WHERE tenant_id = 'apex' AND role = 'system'
                ORDER BY sequence_number DESC LIMIT 1
                """
            ).fetchone()
        assert session is not None
        assert session[0] == "closed"
        assert session[1] is not None
        assert notice is not None
        assert "closed" in notice[0]

    asyncio.run(scenario())


@pytest.mark.integration
def test_the_visitor_gate_reads_the_committed_handoff_state(
    repository_database_url: str,
) -> None:
    """``for_session`` is the gate's read: it sees exactly what the database holds."""

    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresHandoffStore(database.engine)
            recorded = await store.record(command(), session_id="visitor-correlation")

            await store.accept("apex", recorded.handoff_id, principal_id="operator-7")
            held = await store.for_session("apex", recorded.session_id)
            assert held is not None
            assert held.status == HandoffStatus.ASSIGNED.value

            await store.release("apex", recorded.handoff_id, principal_id="operator-7")
            released = await store.for_session("apex", recorded.session_id)
            assert released is not None
            assert released.status == HandoffStatus.QUEUED.value

            assert await store.for_session("clearview", recorded.session_id) is None
        finally:
            await database.dispose()

    asyncio.run(scenario())
