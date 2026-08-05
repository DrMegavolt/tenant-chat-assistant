"""The PRIV-002 governance stores against real PostgreSQL: the turn-record
envelope and the dedicated trace-read grant.

The envelope is the seam `OBS-004` will write through, so its contract here is
the durable part: tenant qualification, content round-tripping, bounded reads,
and a projection registry that cascades off its turn record. The grant store
is the access role: idempotent upsert, revoke-with-receipt, and per-tenant
scoping with no cross-tenant leak.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresTraceAccessStore,
    PostgresTurnRecordStore,
)
from tenantchat.core.errors import NotFoundError

POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=5)

pytestmark = pytest.mark.integration


@pytest.fixture
def database(repository_database_url: str) -> Iterator[Database]:
    db = Database.connect(repository_database_url, POOL)
    try:
        yield db
    finally:
        asyncio.run(db.dispose())


def _libpq(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _seed_tenants(database_url: str, *tenant_ids: str) -> None:
    with psycopg.connect(_libpq(database_url)) as connection, connection.cursor() as cursor:
        cursor.executemany(
            "INSERT INTO tenants (id, display_name) VALUES (%s, %s)",
            [(tenant_id, f"Tenant {tenant_id}") for tenant_id in tenant_ids],
        )


def _seed_session(database_url: str, tenant_id: str, session_id: uuid.UUID) -> None:
    with psycopg.connect(_libpq(database_url)) as connection:
        connection.execute(
            "INSERT INTO chat_sessions (id, tenant_id) VALUES (%s, %s)",
            (session_id, tenant_id),
        )


def test_a_turn_record_round_trips_through_the_envelope(
    database: Database, repository_database_url: str
) -> None:
    _seed_tenants(repository_database_url, "tenant-a")
    session_id = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    store = PostgresTurnRecordStore(database.engine)

    recorded = asyncio.run(
        store.record(
            "tenant-a",
            session_id,
            trace_id="trace-1",
            content={"prompt": "the question", "output": "the answer"},
        )
    )
    fetched = asyncio.run(store.get("tenant-a", recorded.turn_id))

    assert fetched == recorded
    assert fetched.content == {"prompt": "the question", "output": "the answer"}


def test_a_turn_record_cannot_be_read_across_tenants(
    database: Database, repository_database_url: str
) -> None:
    _seed_tenants(repository_database_url, "tenant-a", "tenant-b")
    session_id = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    store = PostgresTurnRecordStore(database.engine)
    recorded = asyncio.run(store.record("tenant-a", session_id, content={"prompt": "hi"}))

    with pytest.raises(NotFoundError):
        asyncio.run(store.get("tenant-b", recorded.turn_id))


def test_for_session_returns_oldest_first_and_bounded(
    database: Database, repository_database_url: str
) -> None:
    _seed_tenants(repository_database_url, "tenant-a")
    session_id = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    store = PostgresTurnRecordStore(database.engine)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    for offset in range(3):
        asyncio.run(
            store.record(
                "tenant-a",
                session_id,
                content={"sequence": offset},
                recorded_at=base + timedelta(minutes=offset),
            )
        )

    records = asyncio.run(store.for_session("tenant-a", session_id, limit=2))

    assert [record.content["sequence"] for record in records] == [0, 1]


def test_record_refuses_a_session_outside_the_tenant(
    database: Database, repository_database_url: str
) -> None:
    _seed_tenants(repository_database_url, "tenant-a", "tenant-b")
    session_id = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    store = PostgresTurnRecordStore(database.engine)

    with pytest.raises(NotFoundError):
        asyncio.run(store.record("tenant-b", session_id, content={"prompt": "hi"}))


def test_a_projection_cascades_off_its_turn_record(
    database: Database, repository_database_url: str
) -> None:
    """Deleting a turn record removes every derived projection in the same statement.

    This is the erasure extension point `FEAT-008` will populate: an evaluation
    dataset row is a projection, and no code needs to know its table exists for
    the subject's erasure to reach it.
    """
    _seed_tenants(repository_database_url, "tenant-a")
    session_id = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    store = PostgresTurnRecordStore(database.engine)
    recorded = asyncio.run(store.record("tenant-a", session_id, content={"prompt": "hi"}))
    with psycopg.connect(_libpq(repository_database_url)) as connection:
        connection.execute(
            "INSERT INTO turn_record_projections (id, tenant_id, turn_record_id, kind) "
            "VALUES (%s, 'tenant-a', %s, 'eval_dataset')",
            (uuid.uuid4(), recorded.turn_id),
        )

    with psycopg.connect(_libpq(repository_database_url)) as connection:
        connection.execute(
            "DELETE FROM turn_records WHERE tenant_id = %s AND id = %s",
            ("tenant-a", recorded.turn_id),
        )
        projections = connection.execute(
            "SELECT count(*) FROM turn_record_projections WHERE tenant_id = %s",
            ("tenant-a",),
        ).fetchone()

    assert projections == (0,)
    assert asyncio.run(store.projections_for_turn("tenant-a", recorded.turn_id)) == ()


def test_a_grant_is_an_idempotent_upsert_with_a_revoke_receipt(
    database: Database, repository_database_url: str
) -> None:
    _seed_tenants(repository_database_url, "tenant-a")
    store = PostgresTraceAccessStore(database.engine)

    first = asyncio.run(store.grant("tenant-a", "operator-1", granted_by="admin-1"))
    second = asyncio.run(store.grant("tenant-a", "operator-1", granted_by="admin-2"))

    assert second.principal_subject == "operator-1"
    assert second.granted_by == "admin-2"
    assert second.granted_at == first.granted_at
    assert asyncio.run(store.has_access("tenant-a", "operator-1"))

    revoked = asyncio.run(store.revoke("tenant-a", "operator-1"))
    second_revoke = asyncio.run(store.revoke("tenant-a", "operator-1"))

    assert revoked is True
    assert second_revoke is False
    assert not asyncio.run(store.has_access("tenant-a", "operator-1"))


def test_a_grant_never_leaks_across_tenants(
    database: Database, repository_database_url: str
) -> None:
    _seed_tenants(repository_database_url, "tenant-a", "tenant-b")
    store = PostgresTraceAccessStore(database.engine)
    asyncio.run(store.grant("tenant-a", "operator-1", granted_by="admin-1"))

    assert asyncio.run(store.has_access("tenant-a", "operator-1"))
    assert not asyncio.run(store.has_access("tenant-b", "operator-1"))

    listed = asyncio.run(store.for_tenant("tenant-b"))
    assert listed == ()
