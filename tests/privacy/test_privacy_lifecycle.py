"""The PRIV-001 lifecycle against a real PostgreSQL: consent, export, erasure,
retention, and the audit trail that makes each one answerable.

One disposable database is migrated and then shared across these tests; each
test opens fresh conversation sessions so stored records never leak between
them. The API surface is driven through `create_app` exactly as a deployment
composes it. Deletion runs through REL-003's leased worker; scheduled retention
continues through the privacy pass.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple, cast

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
from tenantchat.api.job_worker import WorkerSettings, privacy_deletion_handler, run_once
from tenantchat.api.jobs import JobKind
from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresAuditStore,
    PostgresJobStore,
    PostgresPrivacyStore,
    PostgresTurnRecordStore,
)
from tenantchat.api.privacy_worker import run_pass
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.settings import Settings
from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER
from tenantchat.orchestration.model import AssembledPrompt, ModelResponse, ToolSpec

BOOKING_TENANT = "clearview"
OTHER_TENANT = "apex"
DANA_PHONE = "555-222-1919"
BORIS_PHONE = "555-333-4444"
TEST_POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=5)
GATEWAY_TOKEN = "gateway-token-for-privacy-tests"
CSRF_SECRET = "csrf-secret-for-privacy-tests"
SIGNING_KEY = "visitor-signing-key-for-privacy-tests-" + "x" * 16

pytestmark = pytest.mark.integration


class PlainModel:
    """A scripted model that answers questions without booking anything."""

    def __init__(self) -> None:
        self.calls: list[AssembledPrompt] = []

    async def complete(
        self, prompt: AssembledPrompt, *, tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        self.calls.append(prompt)
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
        visitor_credential_signing_key=SIGNING_KEY,
        ingestion_storage_root=tempfile.mkdtemp(prefix="tenantchat-privacy-lifecycle-"),
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


class Visitor(NamedTuple):
    """An opened conversation and the credential that names it (SEC-002)."""

    session_id: str
    headers: dict[str, str]


def open_session(client: TestClient, tenant_id: str) -> Visitor:
    body = client.post("/api/chat/session", json={"tenant_id": tenant_id}).json()
    return Visitor(
        session_id=body["session"]["session_id"],
        headers={VISITOR_CREDENTIAL_HEADER: body["credential"]},
    )


def open_session_with_consent(client: TestClient, tenant_id: str, purposes: list[str]) -> Visitor:
    visitor = open_session(client, tenant_id)
    granted = client.post("/api/chat/consent", json={"purposes": purposes}, headers=visitor.headers)
    assert granted.status_code == 200, granted.text
    return visitor


def offered_slot(client: TestClient, tenant_id: str, service: str = "HVAC") -> str:
    """The first slot the tenant is actually offering.

    A hardcoded label cannot work since DATA-003: the provider mints future
    windows and the reservation refuses anything it is not currently offering.
    """
    response = client.get(f"/api/tenants/{tenant_id}/availability", params={"service": service})
    assert response.status_code == 200, response.text
    slots: list[str] = response.json()["slots"]
    return slots[0]


def plant_subject(
    client: TestClient, tenant_id: str, phone: str, *, with_booking: bool = True
) -> tuple[str, dict[str, object]]:
    """A session with a transcript and a lead naming one subject, plus a booking
    when the tenant books.

    The booking and lead are stored against the session the server derives from
    the correlation id, while the transcript and consent sit on the conversation
    session, so a correct export has to stitch both together.
    """
    visitor = open_session_with_consent(client, tenant_id, ["booking", "follow_up"])
    session_id = visitor.session_id
    turn = client.post(
        "/api/chat",
        json={"message": f"Please call me back at {phone}"},
        headers=visitor.headers,
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
                "slot": offered_slot(client, tenant_id),
                "address": "12 Alder Court, Portland, OR 97205",
            },
            headers={"Idempotency-Key": f"privacy-{session_id}"},
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
    visitor = open_session(client, BOOKING_TENANT)

    refused = client.post(
        "/api/book",
        json={
            "tenant_id": BOOKING_TENANT,
            "session_id": visitor.session_id,
            "customer_name": "Dana Ruiz",
            "contact": DANA_PHONE,
            "service": "HVAC",
            "slot": offered_slot(client, BOOKING_TENANT),
            "address": "12 Alder Court, Portland, OR 97205",
        },
        headers={"Idempotency-Key": "privacy-no-consent"},
    )

    assert refused.status_code == 403
    assert refused.json()["code"] == "consent_required"


def test_a_follow_up_only_grant_cannot_book(client: TestClient) -> None:
    """The booking requires both purposes; one is not enough."""
    visitor = open_session_with_consent(client, BOOKING_TENANT, ["follow_up"])

    refused = client.post(
        "/api/book",
        json={
            "tenant_id": BOOKING_TENANT,
            "session_id": visitor.session_id,
            "customer_name": "Dana Ruiz",
            "contact": DANA_PHONE,
            "service": "HVAC",
            "slot": offered_slot(client, BOOKING_TENANT),
            "address": "12 Alder Court, Portland, OR 97205",
        },
        headers={"Idempotency-Key": "privacy-follow-up-only"},
    )

    assert refused.status_code == 403
    assert refused.json()["code"] == "consent_required"


def test_a_recorded_grant_unlocks_the_action(client: TestClient) -> None:
    visitor = open_session_with_consent(client, BOOKING_TENANT, ["booking", "follow_up"])

    accepted = client.post(
        "/api/book",
        json={
            "tenant_id": BOOKING_TENANT,
            "session_id": visitor.session_id,
            "customer_name": "Dana Ruiz",
            "contact": DANA_PHONE,
            "service": "HVAC",
            "slot": offered_slot(client, BOOKING_TENANT),
            "address": "12 Alder Court, Portland, OR 97205",
        },
        headers={"Idempotency-Key": "privacy-granted"},
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
            privacy = PostgresPrivacyStore(database.engine, database.engine)
            return await run_once(
                PostgresJobStore(database.engine),
                {
                    JobKind.PRIVACY_DELETION: privacy_deletion_handler(
                        privacy, PostgresAuditStore(database.engine)
                    )
                },
                WorkerSettings(
                    worker_id="privacy-worker-test",
                    batch_size=1,
                    lease_duration=timedelta(seconds=30),
                ),
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

        job_status = _row(
            connection,
            """
            SELECT status, idempotency_key
            FROM background_jobs
            WHERE tenant_id = %s AND kind = 'privacy_deletion'
              AND payload->>'request_id' = %s
            """,
            (BOOKING_TENANT, request_id),
        )
        assert job_status == ("succeeded", request_id)

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
    for visitor in (expired, kept):
        turn = client.post(
            "/api/chat",
            json={"message": "What hours are you open?"},
            headers=visitor.headers,
        )
        assert turn.status_code == 200, turn.text
    with psycopg.connect(_libpq(privacy_database_url)) as connection:
        connection.execute(
            "UPDATE messages SET created_at = %s WHERE chat_session_id = %s",
            (now - timedelta(days=100), uuid.UUID(expired.session_id)),
        )
        connection.execute(
            "UPDATE chat_sessions SET started_at = %s, last_activity_at = %s WHERE id = %s",
            (now - timedelta(days=100), now - timedelta(days=100), uuid.UUID(expired.session_id)),
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
            (uuid.UUID(expired.session_id),),
        )
        kept_messages = _scalar(
            connection,
            "SELECT count(*) FROM messages WHERE chat_session_id = %s",
            (uuid.UUID(kept.session_id),),
        )
        expired_session = _scalar(
            connection,
            "SELECT count(*) FROM chat_sessions WHERE id = %s",
            (uuid.UUID(expired.session_id),),
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


def plant_turn_records(
    database_url: str,
    tenant_id: str,
    session_id: str,
    *,
    contact: str,
    recorded_at: datetime | None = None,
) -> uuid.UUID:
    """One inference-plane record naming the subject, via the `OBS-004` seam.

    Written through ``PostgresTurnRecordStore`` exactly as the trace
    finalizer will write it, so the retention and erasure tests pin the
    repository's contract, not a test's private INSERT.
    """
    database = Database.connect(database_url, TEST_POOL)
    try:
        turn = asyncio.run(
            PostgresTurnRecordStore(database.engine).record(
                tenant_id,
                uuid.UUID(session_id),
                trace_id="trace-for-privacy-tests",
                content={
                    "prompt": f"Please call {contact} back",
                    "evidence": ["document-1"],
                    "output": "Noted, thank you.",
                },
                recorded_at=recorded_at,
            )
        )
    finally:
        asyncio.run(_dispose(database))
    return turn.turn_id


def plant_projection(database_url: str, tenant_id: str, turn_record_id: uuid.UUID) -> uuid.UUID:
    """A derived dataset row pinned to the turn, the `FEAT-008` shape."""
    projection_id = uuid.uuid4()
    with psycopg.connect(_libpq(database_url)) as connection:
        connection.execute(
            """
            INSERT INTO turn_record_projections (id, tenant_id, turn_record_id, kind)
            VALUES (%s, %s, %s, 'eval_dataset')
            """,
            (projection_id, tenant_id, turn_record_id),
        )
    return projection_id


def test_an_export_includes_turn_records_and_their_projections(
    client: TestClient, privacy_database_url: str
) -> None:
    """The inference plane is the subject's data, so the export must hold it.

    A subject's prompt and the dataset derived from it are a data-subject
    export's whole point: omitting them would make the export look complete
    while the words were still on disk.
    """
    dana_session, _ = plant_subject(client, BOOKING_TENANT, DANA_PHONE)
    boris_session, _ = plant_subject(client, BOOKING_TENANT, BORIS_PHONE)
    dana_turn = plant_turn_records(
        privacy_database_url, BOOKING_TENANT, dana_session, contact=DANA_PHONE
    )
    boris_turn = plant_turn_records(
        privacy_database_url, BOOKING_TENANT, boris_session, contact=BORIS_PHONE
    )
    plant_projection(privacy_database_url, BOOKING_TENANT, dana_turn)

    response = client.post(
        "/api/admin/privacy/export",
        headers=_csrf_headers(client, "tenant_admin"),
        json={"tenant_id": BOOKING_TENANT, "contact": DANA_PHONE},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    # The conversation itself wrote one record per completed turn (`RAG-005`
    # persists the inference-plane envelope), so the export holds the planted
    # record and the chat turn's own record for the same session.
    turn_ids = [item["turn_id"] for item in body["turn_records"]]
    assert len(turn_ids) == 2
    assert str(dana_turn) in turn_ids
    assert str(boris_turn) not in turn_ids
    exported = next(item for item in body["turn_records"] if item["turn_id"] == str(dana_turn))
    assert exported["trace_id"] == "trace-for-privacy-tests"
    assert DANA_PHONE in exported["content"]["prompt"]
    assert [item["kind"] for item in body["projections"]] == ["eval_dataset"]
    assert body["projections"][0]["turn_record_id"] == str(dana_turn)


def test_a_session_is_found_through_turn_record_content_only(
    client: TestClient, privacy_database_url: str
) -> None:
    """Subject discovery reaches the inference plane, not just the transcript.

    A contact can live in a prompt or a retrieved chunk without ever appearing
    in a message row; an erasure that searched only messages would leave the
    subject's words behind in the plane that exists to hold them.
    """
    visitor = open_session_with_consent(client, OTHER_TENANT, ["follow_up"])
    session_id = visitor.session_id
    turn = plant_turn_records(privacy_database_url, OTHER_TENANT, session_id, contact=DANA_PHONE)

    response = client.post(
        "/api/admin/privacy/export",
        headers=_csrf_headers(client, "tenant_admin"),
        json={"tenant_id": OTHER_TENANT, "contact": DANA_PHONE},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert {item["turn_id"] for item in body["turn_records"]} == {str(turn)}
    assert session_id in {item["session_id"] for item in body["sessions"]}


def test_an_erasure_request_removes_turn_records_and_derived_projections(
    client: TestClient, privacy_database_url: str
) -> None:
    """Erasure reaches the whole inference plane and the removal is verifiable.

    The projection rows are removed by cascading off their turn records: one
    deletion statement covers the record and everything derived from it, and
    the audit row carries the count so a completed request is provable.
    """
    dana_session, _ = plant_subject(client, BOOKING_TENANT, DANA_PHONE)
    dana_turn = plant_turn_records(
        privacy_database_url, BOOKING_TENANT, dana_session, contact=DANA_PHONE
    )
    plant_projection(privacy_database_url, BOOKING_TENANT, dana_turn)

    filed = client.post(
        "/api/admin/privacy/deletion-requests",
        headers=_csrf_headers(client, "platform_admin"),
        json={"tenant_id": BOOKING_TENANT, "contact": DANA_PHONE},
    )
    assert filed.status_code == 201, filed.text

    database = Database.connect(privacy_database_url, TEST_POOL)
    try:

        async def worker_pass() -> int:
            privacy = PostgresPrivacyStore(database.engine, database.engine)
            return await run_once(
                PostgresJobStore(database.engine),
                {
                    JobKind.PRIVACY_DELETION: privacy_deletion_handler(
                        privacy, PostgresAuditStore(database.engine)
                    )
                },
                WorkerSettings(
                    worker_id="privacy-worker-test",
                    batch_size=1,
                    lease_duration=timedelta(seconds=30),
                ),
            )

        completed = asyncio.run(worker_pass())
    finally:
        asyncio.run(_dispose(database))
    assert completed == 1

    with psycopg.connect(_libpq(privacy_database_url)) as connection:
        turn_count = _scalar(
            connection,
            "SELECT count(*) FROM turn_records WHERE tenant_id = %s AND id = %s",
            (BOOKING_TENANT, dana_turn),
        )
        projection_count = _scalar(
            connection,
            "SELECT count(*) FROM turn_record_projections WHERE tenant_id = %s",
            (BOOKING_TENANT,),
        )
        assert (turn_count, projection_count) == (0, 0)

        erased = _row(
            connection,
            """
            SELECT details FROM audit_events
            WHERE tenant_id = %s AND action = 'privacy.erased'
            ORDER BY occurred_at DESC LIMIT 1
            """,
            (BOOKING_TENANT,),
        )
        assert erased[0]["turn_records_deleted"] == 2  # the planted record and the chat turn's own


def test_expired_turn_records_are_purged_while_the_transcript_survives(
    client: TestClient, privacy_database_url: str
) -> None:
    """Trace retention is independent of, and shorter than, transcript retention.

    A turn record past its 30-day rule is purged while the 90-day transcript
    it derived from is untouched, and the purge is observable as a count — not
    as a list of what was purged.
    """
    now = datetime(2026, 8, 5, tzinfo=UTC)
    visitor = open_session_with_consent(client, BOOKING_TENANT, ["follow_up"])
    turn = client.post(
        "/api/chat",
        json={"message": "What hours are you open?"},
        headers=visitor.headers,
    )
    assert turn.status_code == 200, turn.text
    expired_turn = plant_turn_records(
        privacy_database_url,
        BOOKING_TENANT,
        visitor.session_id,
        contact=DANA_PHONE,
        recorded_at=now - timedelta(days=40),
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
        expired_turns = _scalar(
            connection,
            "SELECT count(*) FROM turn_records WHERE tenant_id = %s AND id = %s",
            (BOOKING_TENANT, expired_turn),
        )
        surviving_messages = _scalar(
            connection,
            "SELECT count(*) FROM messages WHERE chat_session_id = %s",
            (uuid.UUID(visitor.session_id),),
        )
        assert expired_turns == 0
        assert surviving_messages == 2

        purge_row = _row(
            connection,
            "SELECT details FROM audit_events WHERE action = %s AND tenant_id = %s",
            ("privacy.retention_purged", BOOKING_TENANT),
        )
        details = purge_row[0]
        assert details["turn_records_deleted"] == 1
        assert details["messages_deleted"] == 0
        assert "turn_id" not in str(details) and "prompt" not in str(details)
