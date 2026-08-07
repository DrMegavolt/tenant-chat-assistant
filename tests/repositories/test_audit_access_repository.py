"""The FEAT-016 read-side stores against real PostgreSQL: the filtered audit
trail and the per-tenant membership listing.

The hermetic fakes prove the contract; these prove the SQL. The trail read
must narrow by action, principal, and time window before applying its bound,
and a filter that matches in one tenant must match nothing in another. The
membership listing is what the permissions view renders: everyone with a role
inside one tenant, never another's.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest

from tenantchat.api.persistence import Database, DatabasePoolSettings
from tenantchat.api.persistence.repositories import (
    PostgresAuditStore,
    PostgresMembershipStore,
)

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


def _seed_audit(
    database_url: str,
    tenant_id: str,
    *,
    action: str,
    principal: str,
    occurred_at: datetime,
) -> None:
    """Insert one row with a controlled timestamp.

    The store stamps ``occurred_at`` itself, so the time-window filter is
    exercised through rows the store would only ever read.
    """
    with psycopg.connect(_libpq(database_url)) as connection:
        connection.execute(
            "INSERT INTO audit_events "
            "(tenant_id, actor_type, principal_id, action, resource_type, details, occurred_at) "
            "VALUES (%s, 'staff', %s, %s, 'chat_session', '{}'::jsonb, %s)",
            (tenant_id, principal, action, occurred_at),
        )


def test_the_trail_filters_before_its_bound(
    database: Database, repository_database_url: str
) -> None:
    _seed_tenants(repository_database_url, "tenant-a")
    store = PostgresAuditStore(database.engine)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    for offset, (action, principal) in enumerate(
        (
            ("staff_reply_sent", "operator-1"),
            ("membership_assigned", "operator-2"),
            ("trace.read_refused", "operator-1"),
        )
    ):
        _seed_audit(
            repository_database_url,
            "tenant-a",
            action=action,
            principal=principal,
            occurred_at=base + timedelta(hours=offset),
        )

    by_action = asyncio.run(store.for_tenant("tenant-a", actions=("membership_assigned",)))
    assert [event.action for event in by_action] == ["membership_assigned"]

    by_principal = asyncio.run(store.for_tenant("tenant-a", principal="operator-1"))
    assert [event.action for event in by_principal] == ["trace.read_refused", "staff_reply_sent"]

    window = asyncio.run(
        store.for_tenant("tenant-a", since=base, until=base + timedelta(minutes=30))
    )
    assert [event.action for event in window] == ["staff_reply_sent"]

    # The bound applies after the filters, so a narrow action survives a small limit.
    bounded = asyncio.run(store.for_tenant("tenant-a", limit=1, actions=("membership_assigned",)))
    assert [event.action for event in bounded] == ["membership_assigned"]

    newest_first = asyncio.run(store.for_tenant("tenant-a"))
    assert [event.action for event in newest_first] == [
        "trace.read_refused",
        "membership_assigned",
        "staff_reply_sent",
    ]


def test_a_trail_filter_never_matches_another_tenant(
    database: Database, repository_database_url: str
) -> None:
    _seed_tenants(repository_database_url, "tenant-a", "tenant-b")
    store = PostgresAuditStore(database.engine)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    _seed_audit(
        repository_database_url,
        "tenant-a",
        action="staff_reply_sent",
        principal="operator-1",
        occurred_at=base,
    )
    _seed_audit(
        repository_database_url,
        "tenant-b",
        action="staff_reply_sent",
        principal="operator-1",
        occurred_at=base,
    )

    rows = asyncio.run(store.for_tenant("tenant-b", actions=("staff_reply_sent",)))
    assert len(rows) == 1
    assert rows[0].tenant_id == "tenant-b"
    assert asyncio.run(store.for_tenant("tenant-b", principal="operator-9")) == ()


def test_membership_listing_is_per_tenant_and_deterministic(
    database: Database, repository_database_url: str
) -> None:
    _seed_tenants(repository_database_url, "tenant-a", "tenant-b")
    store = PostgresMembershipStore(database.engine)
    asyncio.run(store.assign(tenant_id="tenant-a", subject="operator-1", role="viewer"))
    asyncio.run(store.assign(tenant_id="tenant-a", subject="operator-2", role="tenant_admin"))
    asyncio.run(store.assign(tenant_id="tenant-b", subject="operator-3", role="support_agent"))

    listed = asyncio.run(store.for_tenant("tenant-a"))

    assert [(row.principal_subject, row.role) for row in listed] == [
        ("operator-1", "viewer"),
        ("operator-2", "tenant_admin"),
    ]
    assert asyncio.run(store.for_tenant("tenant-b"))[0].principal_subject == "operator-3"

    asyncio.run(store.revoke(tenant_id="tenant-a", subject="operator-1"))
    assert [row.principal_subject for row in asyncio.run(store.for_tenant("tenant-a"))] == [
        "operator-2"
    ]
