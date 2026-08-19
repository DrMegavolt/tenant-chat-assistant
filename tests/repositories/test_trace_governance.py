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


def test_the_derived_columns_round_trip_with_the_envelope(
    database: Database, repository_database_url: str
) -> None:
    """The `OBS-004` attribution projection: content-free columns ride the row."""
    _seed_tenants(repository_database_url, "tenant-a")
    session_id = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    store = PostgresTurnRecordStore(database.engine)

    recorded = asyncio.run(
        store.record(
            "tenant-a",
            session_id,
            trace_id="trace-1",
            content={"outcome": {"status": "answered"}},
            outcome="answered",
            component_manifest_hash="a" * 64,
            diagnosis_causes=("grounding_or_citation_error",),
            diagnosis_statuses=("detected",),
            turn_index=3,
            trace_schema_version="1",
        )
    )
    fetched = asyncio.run(store.get("tenant-a", recorded.turn_id))

    assert fetched.outcome == "answered"
    assert fetched.component_manifest_hash == "a" * 64
    assert fetched.diagnosis_causes == ("grounding_or_citation_error",)
    assert fetched.diagnosis_statuses == ("detected",)
    assert fetched.turn_index == 3
    assert fetched.trace_schema_version == "1"


def test_search_filters_by_manifest_hash_cause_and_outcome(
    database: Database, repository_database_url: str
) -> None:
    """The attribution query surface: newest first, each filter tenant-scoped."""
    _seed_tenants(repository_database_url, "tenant-a", "tenant-b")
    session_id = uuid.uuid4()
    other_session = uuid.uuid4()
    tenant_b_session = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    _seed_session(repository_database_url, "tenant-a", other_session)
    _seed_session(repository_database_url, "tenant-b", tenant_b_session)
    store = PostgresTurnRecordStore(database.engine)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    for index, fields in enumerate(
        (
            ("answered", "a" * 64, ()),
            ("abstained", "b" * 64, ("retrieval_miss",)),
            ("answered", "a" * 64, ("grounding_or_citation_error",)),
        )
    ):
        outcome, manifest, causes = fields
        asyncio.run(
            store.record(
                "tenant-a",
                session_id if index < 2 else other_session,
                content={},
                outcome=outcome,
                component_manifest_hash=manifest,
                diagnosis_causes=causes,
                recorded_at=base + timedelta(minutes=index),
            )
        )
    asyncio.run(
        store.record(
            "tenant-b",
            tenant_b_session,
            content={},
            outcome="answered",
            component_manifest_hash="a" * 64,
            diagnosis_causes=("grounding_or_citation_error",),
            recorded_at=base + timedelta(minutes=5),
        )
    )

    by_manifest = asyncio.run(store.search("tenant-a", manifest_hash="a" * 64))
    assert {record.outcome for record in by_manifest} == {"answered"}
    assert len(by_manifest) == 2

    by_cause = asyncio.run(store.search("tenant-a", causes=("grounding_or_citation_error",)))
    assert [record.diagnosis_causes for record in by_cause] == [("grounding_or_citation_error",)]

    by_outcome = asyncio.run(store.search("tenant-a", outcome="abstained"))
    assert [record.component_manifest_hash for record in by_outcome] == ["b" * 64]

    combined = asyncio.run(
        store.search(
            "tenant-a",
            manifest_hash="a" * 64,
            causes=("grounding_or_citation_error",),
        )
    )
    assert len(combined) == 1
    assert combined[0].turn_index == 0

    newest_first = asyncio.run(store.search("tenant-a"))
    assert [record.outcome for record in newest_first] == [
        "answered",
        "abstained",
        "answered",
    ]

    bounded = asyncio.run(store.search("tenant-a", limit=2))
    assert len(bounded) == 2


def test_search_filters_by_diagnosis_status_and_time_range(
    database: Database, repository_database_url: str
) -> None:
    """The `FEAT-015` explorer dimensions: status and recorded-time bounds."""
    _seed_tenants(repository_database_url, "tenant-a")
    session_id = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    store = PostgresTurnRecordStore(database.engine)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    asyncio.run(
        store.record(
            "tenant-a",
            session_id,
            content={},
            outcome="answered",
            diagnosis_causes=("grounding_or_citation_error",),
            diagnosis_statuses=("detected",),
            recorded_at=base,
        )
    )
    asyncio.run(
        store.record(
            "tenant-a",
            session_id,
            content={},
            outcome="answered",
            diagnosis_causes=("routing_error",),
            diagnosis_statuses=("suspected", "inconclusive"),
            recorded_at=base + timedelta(hours=1),
        )
    )

    by_status = asyncio.run(store.search("tenant-a", statuses=("suspected",)))
    assert [record.diagnosis_statuses for record in by_status] == [("suspected", "inconclusive")]

    since_only = asyncio.run(store.search("tenant-a", since=base + timedelta(minutes=30)))
    assert len(since_only) == 1
    assert since_only[0].diagnosis_statuses == ("suspected", "inconclusive")

    window = asyncio.run(store.search("tenant-a", since=base, until=base + timedelta(minutes=30)))
    assert [record.diagnosis_statuses for record in window] == [("detected",)]

    combined = asyncio.run(
        store.search("tenant-a", statuses=("detected",), until=base + timedelta(hours=1))
    )
    assert len(combined) == 1


def test_search_and_trace_lookup_never_leak_across_tenants(
    database: Database, repository_database_url: str
) -> None:
    """A filter that matches in one tenant matches nothing in another."""
    _seed_tenants(repository_database_url, "tenant-a", "tenant-b")
    session_id = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    store = PostgresTurnRecordStore(database.engine)
    recorded = asyncio.run(
        store.record(
            "tenant-a",
            session_id,
            trace_id="trace-abc",
            content={},
            outcome="answered",
            component_manifest_hash="c" * 64,
        )
    )

    assert asyncio.run(store.for_trace_id("tenant-a", "trace-abc")) == recorded
    assert asyncio.run(store.search("tenant-b", manifest_hash="c" * 64)) == ()
    with pytest.raises(NotFoundError):
        asyncio.run(store.for_trace_id("tenant-b", "trace-abc"))
    with pytest.raises(NotFoundError):
        asyncio.run(store.for_trace_id("tenant-a", "trace-unknown"))


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


def test_refused_and_failed_outcomes_are_recorded(
    database: Database, repository_database_url: str
) -> None:
    """A `RAG-007` refusal or an `OBS-006` crash is a legitimate terminal outcome.

    The outcome CHECK constraint must include every terminal state the graph can
    record. Otherwise a refused or failed turn fails its own
    insert and the IntegrityError was misreported as "session absent or outside
    tenant" after the model had already answered.
    """
    _seed_tenants(repository_database_url, "tenant-a")
    session_id = uuid.uuid4()
    _seed_session(repository_database_url, "tenant-a", session_id)
    store = PostgresTurnRecordStore(database.engine)

    refused = asyncio.run(
        store.record(
            "tenant-a",
            session_id,
            content={"outcome": {"status": "refused"}},
            outcome="refused",
            component_manifest_hash="a" * 64,
        )
    )
    failed = asyncio.run(
        store.record(
            "tenant-a",
            session_id,
            content={"outcome": {"status": "failed"}},
            outcome="failed",
            component_manifest_hash="b" * 64,
        )
    )

    assert asyncio.run(store.get("tenant-a", refused.turn_id)).outcome == "refused"
    assert asyncio.run(store.get("tenant-a", failed.turn_id)).outcome == "failed"


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
