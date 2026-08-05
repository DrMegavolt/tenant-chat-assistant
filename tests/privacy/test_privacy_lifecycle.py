"""The PRIV-001 lifecycle against a real PostgreSQL: consent, export, erasure,
retention, and the audit trail that makes each one answerable.

One disposable database is migrated and then shared across these tests; each
test opens fresh conversation sessions so stored records never leak between
them. The API surface is driven through `create_app` exactly as a deployment
composes it, and the worker is driven through ``run_pass`` because that is the
code a CronJob runs.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import psycopg
import pytest
from fastapi.testclient import TestClient

from tenantchat.api.app import create_app
from tenantchat.api.identity import (
    EMAIL_HEADER,
    GATEWAY_TOKEN_HEADER,
    ROLE_HEADER,
    SUBJECT_HEADER,
)
from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresAuditStore,
    PostgresPrivacyStore,
)
from tenantchat.api.privacy_worker import run_pass
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.settings import Settings
from tenantchat.orchestration.model import ModelMessage, ModelResponse, ToolSpec

BOOKING_TENANT = "clearview"
OTHER_TENANT = "apex"
DANA_PHONE = "555-222-1919"
BORIS_PHONE = "555-333-4444"
TEST_POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=5)
GATEWAY_TOKEN = "gateway-token-for-privacy-tests"
CSRF_SECRET = "csrf-secret-for-privacy-tests"

pytestmark = pytest.mark.integration


class PlainModel:
    """A scripted model that answers questions without booking anything."""

    def __init__(self) -> None:
        self.calls: list[tuple[ModelMessage, ...]] = []

    async def complete(
        self, messages: Sequence[ModelMessage], *, tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        self.calls.append(tuple(messages))
        return ModelResponse(content="Noted, thank you.", model_name="scripted")


def _libpq(sqlalchemy_url: str) -> str:
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _scalar(connection: psycopg.Connection[Any], query: str, params: Sequence[object] = ()) -> Any:
    row = connection.execute(query, tuple(params)).fetchone()
    assert row is not None
    return row[0]


def _row(
    connection: psycopg.Connection[Any], query: str, params: Sequence[object] = ()
) -> tuple[Any, ...]:
    row = connection.execute(query, tuple(params)).fetchone()
    assert row is not None
    return tuple(row)


def deployment(database_url: str, model: PlainModel) -> TestClient:
    settings = Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=65536,
        docs_enabled=False,
        database_url=database_url,
        database_pool_size=2,
        database_max_overflow=0,
        admin_gateway_token=GATEWAY_TOKEN,
        admin_csrf_secret=CSRF_SECRET,
    )
    return TestClient(create_app(settings, chat_model=model))


@pytest.fixture
def client(privacy_database_url: str) -> Iterator[TestClient]:
    with deployment(privacy_database_url, PlainModel()) as test_client:
        yield test_client


def _csrf(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def _operator(role: str) -> dict[str, str]:
    return {
        GATEWAY_TOKEN_HEADER: GATEWAY_TOKEN,
        SUBJECT_HEADER: "operator-9",
        EMAIL_HEADER: "operator@example.com",
        ROLE_HEADER: role,
    }


def _csrf_headers(client: TestClient, role: str) -> dict[str, str]:
    headers = _operator(role)
    headers["X-CSRF-Token"] = _csrf(client, headers)
    return headers


def open_session_with_consent(client: TestClient, tenant_id: str, purposes: list[str]) -> str:
    session_id: str = client.post("/api/chat/session", json={"tenant_id": tenant_id}).json()[
        "session"
    ]["session_id"]
    granted = client.post(
        "/api/chat/consent",
        json={"tenant_id": tenant_id, "session_id": session_id, "purposes": purposes},
    )
    assert granted.status_code == 200, granted.text
    return session_id


def plant_subject(
    client: TestClient, tenant_id: str, phone: str, *, with_booking: bool = True
) -> tuple[str, dict[str, object]]:
    """A session with a transcript and a lead naming one subject, plus a booking
    when the tenant books.

    The booking and lead are stored against the session the server derives from
    the correlation id, while the transcript and consent sit on the conversation
    session, so a correct export has to stitch both together.
    """
    session_id = open_session_with_consent(client, tenant_id, ["booking", "follow_up"])
    turn = client.post(
        "/api/chat",
        json={
            "tenant_id": tenant_id,
            "session_id": session_id,
            "message": f"Please call me back at {phone}",
        },
    )
    assert turn.status_code == 200, turn.text
    booking = None
    if with_booking:
        booking_response = client.post(
            "/api/book",
            json={
                "tenant_id": tenant_id,
                "session_id": session_id,
                "customer_name": "Dana Ruiz",
                "contact": phone,
                "service": "HVAC",
                "slot": "Mon Jul 1, 2:00 PM",
                "address": "12 Alder Court, Portland, OR 97205",
            },
        )
        assert booking_response.status_code == 201, booking_response.text
        booking = booking_response.json()
    lead = client.post(
        "/api/leads",
        json={
            "tenant_id": tenant_id,
            "session_id": session_id,
            "customer_name": "Dana Ruiz",
            "contact": phone,
            "service": "HVAC",
            "summary": "Furnace needs service.",
        },
    )
    assert lead.status_code == 201, lead.text
    return session_id, {"booking": booking, "lead": lead.json()}


def test_consent_required_error_carries_the_missing_purposes(client: TestClient) -> None:
    session_id = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT}).json()[
        "session"
    ]["session_id"]

    refused = client.post(
        "/api/book",
        json={
            "tenant_id": BOOKING_TENANT,
            "session_id": session_id,
            "customer_name": "Dana Ruiz",
            "contact": DANA_PHONE,
            "service": "HVAC",
            "slot": "Mon Jul 1, 2:00 PM",
            "address": "12 Alder Court, Portland, OR 97205",
        },
    )

    assert refused.status_code == 403
    assert refused.json()["code"] == "consent_required"


def test_a_follow_up_only_grant_cannot_book(client: TestClient) -> None:
    """The booking requires both purposes; one is not enough."""
    session_id = open_session_with_consent(client, BOOKING_TENANT, ["follow_up"])

    refused = client.post(
        "/api/book",
        json={
            "tenant_id": BOOKING_TENANT,
            "session_id": session_id,
            "customer_name": "Dana Ruiz",
            "contact": DANA_PHONE,
            "service": "HVAC",
            "slot": "Mon Jul 1, 2:00 PM",
            "address": "12 Alder Court, Portland, OR 97205",
        },
    )

    assert refused.status_code == 403
    assert refused.json()["code"] == "consent_required"


def test_a_recorded_grant_unlocks_the_action(client: TestClient) -> None:
    session_id = open_session_with_consent(client, BOOKING_TENANT, ["booking", "follow_up"])

    accepted = client.post(
        "/api/book",
        json={
            "tenant_id": BOOKING_TENANT,
            "session_id": session_id,
            "customer_name": "Dana Ruiz",
            "contact": DANA_PHONE,
            "service": "HVAC",
            "slot": "Mon Jul 1, 2:00 PM",
            "address": "12 Alder Court, Portland, OR 97205",
        },
    )

    assert accepted.status_code == 201


def test_an_export_contains_one_subject_and_no_other(
    client: TestClient, privacy_database_url: str
) -> None:
    dana_session, dana = plant_subject(client, BOOKING_TENANT, DANA_PHONE)
    boris_session, boris = plant_subject(client, BOOKING_TENANT, BORIS_PHONE)
    # The same phone in another tenant is a different subject's data.
    apex_session, _ = plant_subject(client, OTHER_TENANT, DANA_PHONE, with_booking=False)

    response = client.post(
        "/api/admin/privacy/export",
        headers=_csrf_headers(client, "tenant_admin"),
        json={"tenant_id": BOOKING_TENANT, "contact": DANA_PHONE},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["contact_value"] == "+15552221919"
    exported_sessions = {item["session_id"] for item in body["sessions"]}
    assert dana_session in exported_sessions
    assert boris_session not in exported_sessions
    assert apex_session not in exported_sessions

    exported_messages = [item["content"] for item in body["messages"]]
    assert any(DANA_PHONE in message for message in exported_messages)
    assert not any(BORIS_PHONE in message for message in exported_messages)

    assert [item["booking_id"] for item in body["bookings"]] == [
        cast(dict[str, object], dana["booking"])["booking_id"]
    ]
    assert [item["lead_id"] for item in body["leads"]] == [
        cast(dict[str, object], dana["lead"])["lead_id"]
    ]
    assert all(item["contact"] == "+15552221919" for item in body["bookings"])
    assert all(item["contact"] == "+15552221919" for item in body["leads"])
    assert {item["purpose"] for item in body["consent"]} == {"booking", "follow_up"}


def test_an_export_requires_an_operator_identity(
    client: TestClient, privacy_database_url: str
) -> None:
    """No identity means no rights request, not a defaulted one.

    A gateway token drop or misrouting must fail closed: the route is an
    operator-only surface, so a request without the authenticated headers is
    refused before it can read anything.
    """
    response = client.post(
        "/api/admin/privacy/export",
        json={"tenant_id": BOOKING_TENANT, "contact": DANA_PHONE},
    )
    assert response.status_code == 401


def test_a_viewer_may_not_export_a_subject(client: TestClient, privacy_database_url: str) -> None:
    """Export needs tenancy-admin privilege; a viewer is refused as forbidden."""
    response = client.post(
        "/api/admin/privacy/export",
        headers=_csrf_headers(client, "viewer"),
        json={"tenant_id": BOOKING_TENANT, "contact": DANA_PHONE},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_deletion_request_needs_platform_admin(
    client: TestClient, privacy_database_url: str
) -> None:
    """Filing for erasure is more privileged than reading: tenancy admin is refused.

    The distinction matters because erasure is destructive and must stay an
    explicit, narrow privilege rather than travel with every admin read.
    """
    response = client.post(
        "/api/admin/privacy/deletion-requests",
        headers=_csrf_headers(client, "tenant_admin"),
        json={"tenant_id": BOOKING_TENANT, "contact": DANA_PHONE},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_deletion_request_is_fulfilled_and_audited(
    client: TestClient, privacy_database_url: str
) -> None:
    dana_session, _ = plant_subject(client, BOOKING_TENANT, DANA_PHONE)
    boris_session, _ = plant_subject(client, BOOKING_TENANT, BORIS_PHONE)

    filed = client.post(
        "/api/admin/privacy/deletion-requests",
        headers=_csrf_headers(client, "platform_admin"),
        json={"tenant_id": BOOKING_TENANT, "contact": DANA_PHONE},
    )
    assert filed.status_code == 201, filed.text
    request_id = filed.json()["request_id"]

    database = Database.connect(privacy_database_url, TEST_POOL)
    try:

        async def worker_pass() -> int:
            return await run_pass(
                TenantRegistry.seeded(),
                PostgresPrivacyStore(database.engine, database.engine),
                PostgresAuditStore(database.engine),
            )

        completed = asyncio.run(worker_pass())
    finally:
        asyncio.run(_dispose(database))
    assert completed == 1

    with psycopg.connect(_libpq(privacy_database_url)) as connection:
        # Everything for the erased subject is gone across every primary table.
        dana_sessions = _scalar(
            connection,
            """
            SELECT count(*) FROM chat_sessions s
            WHERE s.tenant_id = %s AND (
                EXISTS (SELECT 1 FROM bookings b WHERE b.tenant_id = s.tenant_id
                        AND b.chat_session_id = s.id AND b.contact_value = %s)
                OR EXISTS (SELECT 1 FROM leads l WHERE l.tenant_id = s.tenant_id
                           AND l.chat_session_id = s.id AND l.contact_value = %s)
                OR EXISTS (SELECT 1 FROM messages m WHERE m.tenant_id = s.tenant_id
                           AND m.chat_session_id = s.id AND m.content ILIKE %s)
            )
            """,
            (BOOKING_TENANT, DANA_PHONE, DANA_PHONE, f"%{DANA_PHONE}%"),
        )
        dana_sessions_named = _scalar(
            connection,
            "SELECT count(*) FROM messages WHERE tenant_id = %s AND content ILIKE %s",
            (BOOKING_TENANT, f"%{DANA_PHONE}%"),
        )
        dana_bookings = _scalar(
            connection,
            "SELECT count(*) FROM bookings WHERE tenant_id = %s AND contact_value = %s",
            (BOOKING_TENANT, DANA_PHONE),
        )
        dana_leads = _scalar(
            connection,
            "SELECT count(*) FROM leads WHERE tenant_id = %s AND contact_value = %s",
            (BOOKING_TENANT, DANA_PHONE),
        )
        assert (dana_sessions, dana_sessions_named, dana_bookings, dana_leads) == (0, 0, 0, 0)

        # The other subject, in the same tenant, is untouched.
        other_messages = _scalar(
            connection,
            "SELECT count(*) FROM messages WHERE tenant_id = %s AND " "chat_session_id = %s",
            (BOOKING_TENANT, uuid.UUID(boris_session)),
        )
        assert other_messages == 2

        request_row = _row(
            connection,
            "SELECT status, contact_value FROM privacy_requests WHERE id = %s",
            (uuid.UUID(request_id),),
        )
        assert request_row[0] == "completed"
        assert request_row[1] == "erased"

        actions = [
            row[0]
            for row in connection.execute(
                "SELECT action FROM audit_events WHERE tenant_id = %s ORDER BY action",
                (BOOKING_TENANT,),
            )
        ]
        assert "privacy.deletion_requested" in actions
        assert "privacy.erased" in actions


def test_expired_transcripts_are_purged_with_audited_counts(
    client: TestClient, privacy_database_url: str
) -> None:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    expired = open_session_with_consent(client, BOOKING_TENANT, ["follow_up"])
    kept = open_session_with_consent(client, BOOKING_TENANT, ["follow_up"])
    for session_id in (expired, kept):
        turn = client.post(
            "/api/chat",
            json={
                "tenant_id": BOOKING_TENANT,
                "session_id": session_id,
                "message": "What hours are you open?",
            },
        )
        assert turn.status_code == 200, turn.text
    with psycopg.connect(_libpq(privacy_database_url)) as connection:
        connection.execute(
            "UPDATE messages SET created_at = %s WHERE chat_session_id = %s",
            (now - timedelta(days=100), uuid.UUID(expired)),
        )
        connection.execute(
            "UPDATE chat_sessions SET started_at = %s, last_activity_at = %s WHERE id = %s",
            (now - timedelta(days=100), now - timedelta(days=100), uuid.UUID(expired)),
        )

    database = Database.connect(privacy_database_url, TEST_POOL)
    try:

        async def worker_pass() -> int:
            return await run_pass(
                TenantRegistry.seeded(),
                PostgresPrivacyStore(database.engine, database.engine),
                PostgresAuditStore(database.engine),
                now=now,
            )

        asyncio.run(worker_pass())
    finally:
        asyncio.run(_dispose(database))

    with psycopg.connect(_libpq(privacy_database_url)) as connection:
        expired_messages = _scalar(
            connection,
            "SELECT count(*) FROM messages WHERE chat_session_id = %s",
            (uuid.UUID(expired),),
        )
        kept_messages = _scalar(
            connection,
            "SELECT count(*) FROM messages WHERE chat_session_id = %s",
            (uuid.UUID(kept),),
        )
        expired_session = _scalar(
            connection,
            "SELECT count(*) FROM chat_sessions WHERE id = %s",
            (uuid.UUID(expired),),
        )
        assert expired_messages == 0
        assert kept_messages == 2
        assert expired_session == 0

        purge_row = _row(
            connection,
            "SELECT details FROM audit_events WHERE action = %s AND tenant_id = %s",
            ("privacy.retention_purged", BOOKING_TENANT),
        )
        details = purge_row[0]
        assert details["sessions_deleted"] == 1
        assert details["messages_deleted"] == 2
        assert details["consent_records_deleted"] >= 1


async def _dispose(database: Database) -> None:
    await database.dispose()
