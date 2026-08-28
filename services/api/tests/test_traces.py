"""The PRIV-002 inference-plane surface: who may read a turn record, and what
the system records about every attempt.

Half of these are about the dedicated role and half about the audit trail,
because both halves are the feature: a trace API that works is worth nothing if
it also answers operators who were never granted the trace-read role, and a
role check no one can verify later is a role check in name only.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.api.tests.conftest import (
    BOOKING_TENANT,
    LEAD_TENANT,
    TEST_GATEWAY_TOKEN,
)
from tenantchat.api import identity as identity_module
from tenantchat.api.app import create_app
from tenantchat.api.identity import (
    CSRF_CLOCK_SKEW,
    CSRF_HEADER,
    CSRF_TOKEN_TTL,
    EMAIL_HEADER,
    GATEWAY_TOKEN_HEADER,
    ROLE_HEADER,
    SUBJECT_HEADER,
)
from tenantchat.api.registry import SYSTEM_TENANT_ID
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    InMemoryAuditStore,
    InMemoryBookingStore,
    InMemoryConsentStore,
    InMemoryConversationStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryLeadStore,
    InMemoryMembershipStore,
    InMemoryPrivacyStore,
    InMemoryTraceAccessStore,
    InMemoryTurnRecordStore,
)

TRACE_TENANT = BOOKING_TENANT
OTHER_TENANT = LEAD_TENANT
READ_REASON = "quality_review"


TraceApp = tuple[
    TestClient,
    InMemoryTurnRecordStore,
    InMemoryTraceAccessStore,
    InMemoryAuditStore,
]


@pytest.fixture
def trace_app() -> Iterator[TraceApp]:
    """A client over a fresh app with the trace stores wired, plus handles to them.

    The stores are returned so a test can plant a turn record and then read it
    back through the API — the same shape a deployment has, where `OBS-004`
    writes through the same repository the read route serves.
    """
    settings = Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=65536,
        docs_enabled=False,
        admin_gateway_token=TEST_GATEWAY_TOKEN,
        admin_csrf_secret="csrf-secret-for-trace-tests",
        visitor_credential_signing_key="visitor-signing-key-for-trace-tests-" + "x" * 16,
    )
    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    turns = InMemoryTurnRecordStore()
    grants = InMemoryTraceAccessStore()
    audit = InMemoryAuditStore()
    membership = InMemoryMembershipStore()
    for tenant_id in (TRACE_TENANT, OTHER_TENANT):
        asyncio.run(
            membership.assign(tenant_id=tenant_id, subject="operator-7", role="support_agent")
        )
    app = create_app(
        settings,
        booking_store=InMemoryBookingStore(),
        lead_store=InMemoryLeadStore(),
        conversation_store=conversations,
        handoff_store=InMemoryHandoffStore(),
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=membership,
        consent_store=consent,
        privacy_store=InMemoryPrivacyStore(
            conversations,
            InMemoryBookingStore(),
            InMemoryLeadStore(),
            InMemoryHandoffStore(),
            consent,
            turn_records=turns,
        ),
        audit_store=audit,
        turn_record_store=turns,
        trace_access_store=grants,
    )
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client, turns, grants, audit


def _operator(role: str = "support_agent", **overrides: str) -> dict[str, str]:
    return {
        GATEWAY_TOKEN_HEADER: TEST_GATEWAY_TOKEN,
        SUBJECT_HEADER: "operator-7",
        EMAIL_HEADER: "operator@example.com",
        ROLE_HEADER: role,
    } | overrides


def _csrf(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


class _FrozenTime:
    """The `time` module surface identity.py uses, pinned to one instant.

    The CSRF token's expiry is wall-clock based; a test cannot wait out the
    TTL, so the module's clock is swapped for a frozen one and re-frozen at
    the instants the token's lifetime should have already passed.
    """

    def __init__(self, now: float) -> None:
        self._now = now

    def time(self) -> float:
        return self._now


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, now: float) -> None:
    monkeypatch.setattr(identity_module, "time", _FrozenTime(now))


def _bytes_headers(headers: dict[str, str]) -> dict[bytes, bytes]:
    """A header mapping with every value latin-1 encoded, as the wire has it.

    httpx validates str header values as ASCII, but the real gateway forwards
    raw bytes; encoding here is how a test presents a header byte ≥ 0x80 to
    the server at all.
    """
    return {name.encode("latin-1"): value.encode("latin-1") for name, value in headers.items()}


def _plant_turn(
    turns: InMemoryTurnRecordStore, *, tenant_id: str = TRACE_TENANT
) -> tuple[str, str]:
    """One turn record with content naming the caller, returned as (turn_id, session_id)."""
    session_id = uuid.uuid4()
    asyncio.run(
        turns.record(
            tenant_id,
            session_id,
            trace_id="trace-1",
            content={
                "prompt": "The customer asked about availability.",
                "evidence": ["document-1"],
                "output": "We are open until 7pm.",
            },
        )
    )
    return str(asyncio.run(turns.for_session(tenant_id, session_id, limit=1))[0].turn_id), str(
        session_id
    )


@pytest.mark.security
def test_an_unauthenticated_caller_reaches_no_trace_surface(
    trace_app: tuple[
        TestClient, InMemoryTurnRecordStore, InMemoryTraceAccessStore, InMemoryAuditStore
    ],
) -> None:
    client, turns, _grants, _audit = trace_app
    turn_id, _session = _plant_turn(turns)

    read = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
    )
    grant = client.post("/api/admin/trace-access", json={"tenant_id": TRACE_TENANT, "subject": "s"})

    assert read.status_code == 401
    assert read.json()["code"] == "unauthenticated"
    assert grant.status_code == 401


@pytest.mark.security
def test_a_support_agent_without_the_role_cannot_read_a_turn(
    trace_app: tuple[
        TestClient, InMemoryTurnRecordStore, InMemoryTraceAccessStore, InMemoryAuditStore
    ],
) -> None:
    """A transcript viewer is not a trace reader: the role is separate by design."""
    client, turns, _grants, audit = trace_app
    turn_id, _session = _plant_turn(turns)

    response = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"

    refused = [event for event in audit._events if event.action == "trace.read_refused"]
    assert len(refused) == 1
    assert refused[0].principal_id == "operator-7"
    assert refused[0].details["required_role"] == "trace_viewer"
    assert refused[0].details["reason"] == READ_REASON


@pytest.mark.security
def test_a_tenant_admin_without_the_role_is_refused_and_audited(
    trace_app: tuple[
        TestClient, InMemoryTurnRecordStore, InMemoryTraceAccessStore, InMemoryAuditStore
    ],
) -> None:
    """The ordered transcript hierarchy confers nothing on the trace plane."""
    client, turns, _grants, audit = trace_app
    turn_id, _session = _plant_turn(turns)

    response = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(role="tenant_admin"),
    )

    assert response.status_code == 403
    assert any(event.action == "trace.read_refused" for event in audit._events)


@pytest.mark.security
def test_platform_admin_reads_without_a_grant_row(trace_app: TraceApp) -> None:
    client, turns, _grants, audit = trace_app
    turn_id, _session = _plant_turn(turns)

    response = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(role="platform_admin"),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["turn_id"] == turn_id
    assert body["content"]["output"] == "We are open until 7pm."

    reads = [event for event in audit._events if event.action == "trace.read"]
    assert len(reads) == 1
    assert reads[0].principal_id == "operator-7"
    assert reads[0].resource_type == "turn_record"
    assert str(reads[0].resource_id) == turn_id
    assert reads[0].details["reason"] == READ_REASON


def test_a_granted_operator_reads_and_every_read_is_audited(trace_app: TraceApp) -> None:
    client, turns, grants, audit = trace_app
    turn_id, _session = _plant_turn(turns)
    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))

    first = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )
    second = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT, "reason": "subject_request"},
        headers=_operator(),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    reads = [event for event in audit._events if event.action == "trace.read"]
    assert len(reads) == 2
    assert {event.details["reason"] for event in reads} == {READ_REASON, "subject_request"}
    assert all(event.principal_id == "operator-7" for event in reads)
    assert all(str(event.resource_id) == turn_id for event in reads)


def test_a_read_without_a_reason_is_refused_by_the_schema(trace_app: TraceApp) -> None:
    """No reason means no audit trail; the route refuses before the store is touched."""
    client, turns, grants, audit = trace_app
    turn_id, _session = _plant_turn(turns)
    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))

    response = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT},
        headers=_operator(),
    )

    assert response.status_code == 422
    assert not any(event.action == "trace.read" for event in audit._events)


def test_a_turn_from_another_tenant_is_undistinguishable_from_a_missing_one(
    trace_app: TraceApp,
) -> None:
    """Cross-tenant reads return the same 404 as a read of nothing."""
    client, turns, grants, audit = trace_app
    turn_id, _session = _plant_turn(turns, tenant_id=OTHER_TENANT)
    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))

    wrong_tenant = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )
    missing = client.get(
        f"/api/admin/traces/{uuid.uuid4()}",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )

    assert wrong_tenant.status_code == 404
    assert missing.status_code == 404
    assert wrong_tenant.json()["code"] == "not_found"
    assert missing.json()["code"] == "not_found"


def test_granting_trace_access_needs_platform_admin_and_is_audited(trace_app: TraceApp) -> None:
    client, _turns, grants, audit = trace_app

    refused = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": TRACE_TENANT, "subject": "operator-7"},
        headers=_operator(role="tenant_admin"),
    )

    assert refused.status_code == 403
    assert refused.json()["code"] == "forbidden"
    assert not asyncio.run(grants.has_access(TRACE_TENANT, "operator-7"))

    headers = _operator(role="platform_admin")
    headers[CSRF_HEADER] = _csrf(client, headers)
    granted = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": TRACE_TENANT, "subject": "operator-7"},
        headers=headers,
    )

    assert granted.status_code == 201, granted.text
    assert granted.json()["subject"] == "operator-7"
    assert granted.json()["granted_by"] == "operator-7"
    assert asyncio.run(grants.has_access(TRACE_TENANT, "operator-7"))
    assert any(
        event.action == "trace_access.granted" and event.details == {"subject": "operator-7"}
        for event in audit._events
    )


def test_revoking_trace_access_closes_the_read_gate(trace_app: TraceApp) -> None:
    client, turns, grants, _audit = trace_app
    turn_id, _session = _plant_turn(turns)
    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))
    headers = _operator(role="platform_admin")
    headers[CSRF_HEADER] = _csrf(client, headers)

    revoked = client.delete(
        "/api/admin/trace-access",
        params={"tenant_id": TRACE_TENANT, "subject": "operator-7"},
        headers=headers,
    )

    assert revoked.status_code == 204
    assert not asyncio.run(grants.has_access(TRACE_TENANT, "operator-7"))

    read = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )
    assert read.status_code == 403


def test_granting_and_reading_are_tenant_scoped(trace_app: TraceApp) -> None:
    """A grant for one tenant opens nothing in another."""
    client, turns, grants, _audit = trace_app
    turn_id, _session = _plant_turn(turns, tenant_id=OTHER_TENANT)
    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))

    read = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": OTHER_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )

    assert read.status_code == 403
    assert read.json()["code"] == "forbidden"


def test_search_is_gated_by_the_dedicated_role_and_audited_with_its_filters(
    trace_app: TraceApp,
) -> None:
    """The attribution surface: same role gate as reads, one audit row per search."""
    client, turns, grants, audit = trace_app
    session_id = uuid.uuid4()
    asyncio.run(
        turns.record(
            TRACE_TENANT,
            session_id,
            content={"output": "We are open until 7pm."},
            outcome="answered",
        )
    )

    refused = client.get(
        "/api/admin/traces",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )
    assert refused.status_code == 403

    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))
    search = client.get(
        "/api/admin/traces",
        params={
            "tenant_id": TRACE_TENANT,
            "reason": READ_REASON,
            "outcome": "answered",
            "limit": 10,
        },
        headers=_operator(),
    )
    assert search.status_code == 200, search.text
    body = search.json()
    assert [record["outcome"] for record in body["records"]] == ["answered"]
    assert all("prompt" not in record and "output" not in record for record in body["records"])

    searches = [event for event in audit._events if event.action == "trace.search"]
    assert len(searches) == 1
    assert searches[0].principal_id == "operator-7"
    assert searches[0].details == {
        "reason": READ_REASON,
        "manifest_hash": None,
        "cause": None,
        "diagnosis_status": None,
        "outcome": "answered",
        "since": None,
        "until": None,
        "limit": 10,
        "generation_id": None,
        "matches": 1,
    }


def test_search_filters_by_manifest_hash_and_cause(trace_app: TraceApp) -> None:
    """The attribution query surface: component-version and cause filtering."""
    client, turns, grants, _audit = trace_app
    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))
    session_id = uuid.uuid4()
    asyncio.run(
        turns.record(
            TRACE_TENANT,
            session_id,
            content={},
            outcome="answered",
            component_manifest_hash="a" * 64,
            diagnosis_causes=("grounding_or_citation_error",),
        )
    )
    asyncio.run(
        turns.record(
            TRACE_TENANT,
            session_id,
            content={},
            outcome="abstained",
            component_manifest_hash="b" * 64,
            diagnosis_causes=("retrieval_miss",),
        )
    )
    headers = _operator()

    by_manifest = client.get(
        "/api/admin/traces",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON, "manifest_hash": "a" * 64},
        headers=headers,
    )
    assert by_manifest.status_code == 200
    assert [record["outcome"] for record in by_manifest.json()["records"]] == ["answered"]

    by_cause = client.get(
        "/api/admin/traces",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON, "cause": "retrieval_miss"},
        headers=headers,
    )
    assert by_cause.status_code == 200
    assert [record["outcome"] for record in by_cause.json()["records"]] == ["abstained"]

    malformed = client.get(
        "/api/admin/traces",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON, "manifest_hash": "short"},
        headers=headers,
    )
    assert malformed.status_code == 422


def test_the_correlation_lookup_returns_the_record_and_is_audited(
    trace_app: TraceApp,
) -> None:
    """by-trace-id: the OBS-001 correlation id names exactly one turn record."""
    client, turns, grants, audit = trace_app
    session_id = uuid.uuid4()
    recorded = asyncio.run(
        turns.record(
            TRACE_TENANT,
            session_id,
            trace_id="trace-request-1",
            content={"output": "We are open until 7pm."},
        )
    )
    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))

    found = client.get(
        "/api/admin/traces/by-trace-id/trace-request-1",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )

    assert found.status_code == 200, found.text
    assert found.json()["turn_id"] == str(recorded.turn_id)
    assert found.json()["content"]["output"] == "We are open until 7pm."
    reads = [event for event in audit._events if event.action == "trace.read"]
    assert len(reads) == 1
    assert reads[0].details == {"reason": READ_REASON, "trace_id": "trace-request-1"}

    missing = client.get(
        "/api/admin/traces/by-trace-id/trace-unknown",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


def test_the_correlation_lookup_is_tenant_scoped(trace_app: TraceApp) -> None:
    """A trace id from another tenant is indistinguishable from a missing one."""
    client, turns, grants, _audit = trace_app
    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))
    asyncio.run(
        turns.record(
            OTHER_TENANT,
            uuid.uuid4(),
            trace_id="trace-foreign",
            content={},
        )
    )

    response = client.get(
        "/api/admin/traces/by-trace-id/trace-foreign",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=_operator(),
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_a_grant_mutation_requires_the_csrf_token(trace_app: TraceApp) -> None:
    client, _turns, grants, _audit = trace_app

    response = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": TRACE_TENANT, "subject": "operator-7"},
        headers=_operator(role="platform_admin"),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_validation_failed"
    assert not asyncio.run(grants.has_access(TRACE_TENANT, "operator-7"))


@pytest.mark.security
def test_a_non_ascii_gateway_token_is_refused_not_a_server_error(trace_app: TraceApp) -> None:
    """R-08: header bytes decode as latin-1, so a raw byte ≥ 0x80 arrives as a
    non-ASCII string that ``hmac.compare_digest`` refuses to compare — which
    used to surface as a TypeError and a 500 instead of the refusal it is. The
    headers go over as raw bytes because the real gateway forwards raw bytes."""
    client, turns, _grants, _audit = trace_app
    turn_id, _session = _plant_turn(turns)

    wire_headers = _bytes_headers(_operator() | {GATEWAY_TOKEN_HEADER: "gateway-t\xf6ken-\xff"})
    response = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
        headers=wire_headers,
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.security
def test_a_non_ascii_csrf_token_is_refused_not_a_server_error(trace_app: TraceApp) -> None:
    """The same latin-1 hazard on the double-submit path: a presented token
    with non-ASCII characters must fail validation, not the request handler."""
    client, _turns, _grants, _audit = trace_app

    response = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": TRACE_TENANT, "subject": "operator-7"},
        headers=_bytes_headers(_operator(role="platform_admin") | {CSRF_HEADER: "t\xf6ken-\xff"}),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_validation_failed"


@pytest.mark.security
def test_a_refusal_for_an_unknown_tenant_is_still_audited(trace_app: TraceApp) -> None:
    """R-09: ``audit_events`` is foreign-keyed to ``tenants``, so a refusal
    naming a tenant this deployment never served used to hit an IntegrityError
    and vanish along with the 500. The row must always persist — under the
    bootstrapped system tenant — and never under the probed id, so the refusal
    stays unusable for tenant enumeration."""
    client, turns, _grants, audit = trace_app
    turn_id, _session = _plant_turn(turns)

    response = client.get(
        f"/api/admin/traces/{turn_id}",
        params={"tenant_id": "ghost-tenant", "reason": READ_REASON},
        headers=_operator(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    refused = [event for event in audit._events if event.action == "trace.read_refused"]
    assert len(refused) == 1
    assert refused[0].tenant_id == SYSTEM_TENANT_ID
    assert refused[0].principal_id == "operator-7"


def test_the_csrf_token_rotates_on_every_mint(
    trace_app: TraceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-57: the token derives from the subject and the minting instant, so
    each fetch rotates it and a token copied out of an old response stops
    working at the next one."""
    client, _turns, _grants, _audit = trace_app
    headers = _operator(role="platform_admin")
    _freeze_clock(monkeypatch, 1_000_000)
    first = _csrf(client, headers)
    _freeze_clock(monkeypatch, 1_000_001)
    second = _csrf(client, headers)

    assert first != second


def test_an_expired_csrf_token_is_refused(
    trace_app: TraceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-57: a token is presentable for TTL hours, not forever — replaying a
    minted token after the window must fail validation like an absent one."""
    client, _turns, _grants, _audit = trace_app
    headers = _operator(role="platform_admin")
    _freeze_clock(monkeypatch, 1_000_000)
    token = _csrf(client, headers)
    lifetime = int(CSRF_TOKEN_TTL.total_seconds())
    _freeze_clock(monkeypatch, 1_000_000 + lifetime + 1)

    response = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": TRACE_TENANT, "subject": "operator-7"},
        headers=headers | {CSRF_HEADER: token},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_validation_failed"


def test_a_fresh_csrf_token_still_authorizes_the_mutation(
    trace_app: TraceApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The expiry bound is not a regression on the happy path: a token inside
    its window (and slightly skewed clocks) still works."""
    client, _turns, grants, _audit = trace_app
    headers = _operator(role="platform_admin")
    _freeze_clock(monkeypatch, 1_000_000)
    token = _csrf(client, headers)
    _freeze_clock(monkeypatch, 1_000_000 + int(CSRF_CLOCK_SKEW) - 1)

    response = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": TRACE_TENANT, "subject": "operator-7"},
        headers=headers | {CSRF_HEADER: token},
    )

    assert response.status_code == 201, response.text
    assert asyncio.run(grants.has_access(TRACE_TENANT, "operator-7"))


@pytest.mark.security
def test_a_superscript_digit_csrf_token_is_refused_not_a_server_error(
    trace_app: TraceApp,
) -> None:
    """R-08's hazard class, one layer deeper: ``str.isdigit`` accepts the
    superscript digits (``"²"``) whose ``int()`` raises ValueError, so a
    header of ``².x`` used to surface as a 500 instead of the 403 it is. A
    legitimate minted token still passes alongside it."""
    client, _turns, _grants, _audit = trace_app
    headers = _operator(role="platform_admin")
    granted = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": TRACE_TENANT, "subject": "operator-7"},
        headers=headers | {CSRF_HEADER: _csrf(client, headers)},
    )
    assert granted.status_code == 201, granted.text

    refused = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": TRACE_TENANT, "subject": "operator-9"},
        # Raw bytes, as the wire carries them: the server decodes latin-1 and
        # sees the superscript digit str-side.
        headers=_bytes_headers(headers) | {CSRF_HEADER.encode(): "².x".encode("latin-1")},
    )

    assert refused.status_code == 403
    assert refused.json()["code"] == "csrf_validation_failed"


def test_listing_trace_grants_needs_no_csrf_token(trace_app: TraceApp) -> None:
    """R-57: the grant list is a GET like every other trace-plane read — the
    double-submit token defends state-changing requests, and demanding CSRF on
    one GET while its siblings go without was an inconsistent policy, not a
    stronger one."""
    client, _turns, grants, _audit = trace_app
    asyncio.run(grants.grant(TRACE_TENANT, "operator-9", granted_by="platform-admin-1"))

    response = client.get(
        "/api/admin/trace-access",
        params={"tenant_id": TRACE_TENANT},
        headers=_operator(role="platform_admin"),
    )

    assert response.status_code == 200, response.text
    subjects = [grant["subject"] for grant in response.json()["grants"]]
    assert subjects == ["operator-9"]
