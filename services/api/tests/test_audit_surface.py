"""FEAT-016: the audit read surface and the permissions view.

The trail is the accountability record every privileged action already writes.
These tests are about the boundary around reading it back: who may, what a
request for another tenant proves, that every read is itself a recorded read,
that the filters narrow the tenant's rows before the bound, and that the
projection is content-free by construction.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from services.api.tests.conftest import (
    BOOKING_TENANT,
    LEAD_TENANT,
    TEST_OPERATOR_SUBJECT,
)
from tenantchat.api.identity import CSRF_HEADER, SUBJECT_HEADER
from tenantchat.api.store import (
    AuditActorType,
    AuditEvent,
    InMemoryAuditStore,
    InMemoryMembershipStore,
)

OTHER_TENANT = "apex"
PHANTOM_TENANT = "tenant-that-never-existed"

# The bounded keys an audit projection may carry, in the serialized document.
AUDIT_PROJECTION_KEYS = frozenset(
    {
        "action",
        "actor_type",
        "principal",
        "tenant_id",
        "request_id",
        "trace_id",
        "resource_type",
        "resource_id",
        "occurred_at",
        "permission",
    }
)


def _csrf_for(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    token: str = response.json()["csrf_token"]
    return token


def _admin_headers(
    operator_headers: Callable[..., dict[str, str]], **overrides: str
) -> dict[str, str]:
    return operator_headers(role="tenant_admin", **overrides)


def _make_tenant_admin(
    membership_store: InMemoryMembershipStore,
    tenant_id: str,
    subject: str = TEST_OPERATOR_SUBJECT,
) -> None:
    """Bypass the API so a test seeds membership without an audit row on purpose."""
    asyncio.run(
        membership_store.assign(
            tenant_id=tenant_id, subject=subject, role="tenant_admin"
        )
    )


def _event(
    tenant_id: str,
    *,
    action: str,
    principal: str = "operator-7",
    request_id: str | None = "req-1",
    occurred_at: datetime | None = None,
    resource_type: str = "chat_session",
    resource_id: uuid.UUID | None = None,
    details: dict[str, object] | None = None,
) -> AuditEvent:
    return AuditEvent(
        tenant_id=tenant_id,
        actor_type=AuditActorType.STAFF,
        principal_id=principal,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        details=details or {},
        occurred_at=occurred_at or datetime.now(UTC),
    )


@pytest.mark.security
def test_an_unauthenticated_caller_reaches_no_audit_surface(client: TestClient) -> None:
    for path in ("/api/admin/audit", "/api/admin/permissions"):
        response = client.get(path, params={"tenant_id": BOOKING_TENANT})

        assert response.status_code == 401
        assert response.json()["code"] == "unauthenticated"


@pytest.mark.security
def test_an_operator_below_tenant_admin_cannot_read_the_audit_trail(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    audit_store: InMemoryAuditStore,
) -> None:
    """A viewer or support agent administers nothing, so the surface refuses.

    The refusal is the console's 404 rather than a 403: a caller without
    admin authority must not learn that the named tenant exists.
    """
    asyncio.run(audit_store.record(_event(BOOKING_TENANT, action="staff_reply_sent")))

    for role in ("viewer", "support_agent"):
        response = client.get(
            "/api/admin/audit",
            params={"tenant_id": BOOKING_TENANT},
            headers=operator_headers(role=role),
        )

        assert response.status_code == 404
        assert response.json()["code"] == "not_found"


def test_a_tenant_admin_reads_only_their_own_tenants_rows(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    asyncio.run(audit_store.record(_event(BOOKING_TENANT, action="staff_reply_sent")))
    asyncio.run(audit_store.record(_event(OTHER_TENANT, action="membership_assigned")))

    response = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT},
        headers=_admin_headers(operator_headers),
    )

    assert response.status_code == 200
    actions = [event["action"] for event in response.json()["events"]]
    assert actions == ["staff_reply_sent"]
    assert all(event["tenant_id"] == BOOKING_TENANT for event in response.json()["events"])
    assert OTHER_TENANT not in response.text


@pytest.mark.security
def test_a_request_for_another_tenant_is_byte_identical_to_a_phantom_tenant(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    """The console cannot be used to probe the tenant registry.

    An operator who administers one tenant and asks for another gets exactly
    the document a tenant that never existed produces, so existence cannot be
    distinguished and no other tenant's rows leak.
    """
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    asyncio.run(audit_store.record(_event(OTHER_TENANT, action="staff_reply_sent")))
    headers = _admin_headers(operator_headers)

    other = client.get("/api/admin/audit", params={"tenant_id": OTHER_TENANT}, headers=headers)
    phantom = client.get("/api/admin/audit", params={"tenant_id": PHANTOM_TENANT}, headers=headers)

    assert other.status_code == 404
    assert phantom.status_code == 404
    assert {k: v for k, v in other.json().items() if k != "requestId"} == {
        k: v for k, v in phantom.json().items() if k != "requestId"
    }
    assert OTHER_TENANT not in other.json()["detail"]
    assert PHANTOM_TENANT not in phantom.json()["detail"]


@pytest.mark.security
def test_a_request_for_another_tenants_permissions_is_the_same_404(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
) -> None:
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    headers = _admin_headers(operator_headers)

    other = client.get(
        "/api/admin/permissions", params={"tenant_id": OTHER_TENANT}, headers=headers
    )
    phantom = client.get(
        "/api/admin/permissions", params={"tenant_id": PHANTOM_TENANT}, headers=headers
    )

    assert other.status_code == 404
    assert phantom.status_code == 404
    assert {k: v for k, v in other.json().items() if k != "requestId"} == {
        k: v for k, v in phantom.json().items() if k != "requestId"
    }


def test_every_audit_read_is_itself_audited_and_terminates(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    """An audit of an audit adds exactly one row and stops.

    The read records its own `audit.read` row after fetching, so the current
    read's row is never in its own response; the previous read's row always is.
    Recording never re-enters the read path, so the trail cannot recurse.
    """
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    asyncio.run(audit_store.record(_event(BOOKING_TENANT, action="staff_reply_sent")))
    headers = _admin_headers(operator_headers)

    first = client.get("/api/admin/audit", params={"tenant_id": BOOKING_TENANT}, headers=headers)
    assert first.status_code == 200
    first_actions = [event["action"] for event in first.json()["events"]]
    assert first_actions == ["staff_reply_sent"]

    second = client.get("/api/admin/audit", params={"tenant_id": BOOKING_TENANT}, headers=headers)
    assert second.status_code == 200
    second_actions = [event["action"] for event in second.json()["events"]]
    # The first read's own row is now in the store and therefore in the trail;
    # the second read's own row is recorded only after the fetch.
    assert second_actions == ["audit.read", "staff_reply_sent"]

    rows = asyncio.run(audit_store.for_tenant(BOOKING_TENANT))
    audit_reads = [row for row in rows if row.action == "audit.read"]
    assert len(audit_reads) == 2
    assert audit_reads[0].principal_id == TEST_OPERATOR_SUBJECT
    assert audit_reads[0].resource_type == "audit_trail"


def test_an_audit_read_records_the_filters_that_ran(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    """The console's own read is answerable as actor and filter, content-free."""
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    headers = _admin_headers(operator_headers)

    client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "action": "staff_reply_sent", "limit": 50},
        headers=headers,
    )

    rows = asyncio.run(audit_store.for_tenant(BOOKING_TENANT))
    read = next(row for row in rows if row.action == "audit.read")
    assert read.principal_id == TEST_OPERATOR_SUBJECT
    assert read.details["action"] == "staff_reply_sent"
    assert read.details["limit"] == 50
    assert read.request_id is not None


def test_audit_filters_narrow_by_action_principal_and_time(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    asyncio.run(
        audit_store.record(
            _event(BOOKING_TENANT, action="staff_reply_sent", principal="operator-1", occurred_at=base)
        )
    )
    asyncio.run(
        audit_store.record(
            _event(
                BOOKING_TENANT,
                action="membership_assigned",
                principal="operator-2",
                occurred_at=base + timedelta(hours=1),
            )
        )
    )
    asyncio.run(
        audit_store.record(
            _event(
                BOOKING_TENANT,
                action="trace.read_refused",
                principal="operator-1",
                occurred_at=base + timedelta(hours=2),
            )
        )
    )
    headers = _admin_headers(operator_headers)

    by_action = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "action": "membership_assigned"},
        headers=headers,
    )
    assert [event["action"] for event in by_action.json()["events"]] == ["membership_assigned"]

    by_principal = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "principal": "operator-1"},
        headers=headers,
    )
    assert [event["action"] for event in by_principal.json()["events"]] == [
        "trace.read_refused",
        "staff_reply_sent",
    ]

    window = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "since": base.isoformat(), "until": (base + timedelta(minutes=30)).isoformat()},
        headers=headers,
    )
    assert [event["action"] for event in window.json()["events"]] == ["staff_reply_sent"]

    before = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "until": base.isoformat()},
        headers=headers,
    )
    assert [event["action"] for event in before.json()["events"]] == ["staff_reply_sent"]


def test_audit_filters_never_escape_the_tenant(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    """A filter that matches another tenant's rows matches nothing here."""
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    asyncio.run(audit_store.record(_event(OTHER_TENANT, action="staff_reply_sent")))
    headers = _admin_headers(operator_headers)

    response = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "action": "staff_reply_sent"},
        headers=headers,
    )

    assert [event["action"] for event in response.json()["events"]] == []


def test_the_limit_is_applied_after_the_filters(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    """Filtering first and bounding second keeps the bound honest."""
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    base = datetime(2026, 8, 1, tzinfo=UTC)
    for index in range(5):
        asyncio.run(
            audit_store.record(
                _event(
                    BOOKING_TENANT,
                    action="staff_reply_sent",
                    occurred_at=base + timedelta(minutes=index),
                )
            )
        )
    headers = _admin_headers(operator_headers)

    bounded = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "limit": 2},
        headers=headers,
    )
    assert len(bounded.json()["events"]) == 2

    filtered = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "action": "trace.read", "limit": 2},
        headers=headers,
    )
    assert filtered.json()["events"] == []


@pytest.mark.security
def test_a_platform_administrator_reads_any_tenants_trail_without_a_membership(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    audit_store: InMemoryAuditStore,
) -> None:
    asyncio.run(audit_store.record(_event(BOOKING_TENANT, action="staff_reply_sent")))
    headers = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})

    response = client.get("/api/admin/audit", params={"tenant_id": BOOKING_TENANT}, headers=headers)

    assert response.status_code == 200
    assert "staff_reply_sent" in [event["action"] for event in response.json()["events"]]


def test_an_audit_row_renders_the_permission_that_authorized_it(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    asyncio.run(
        audit_store.record(_event(BOOKING_TENANT, action="membership_assigned", principal="platform-1"))
    )
    asyncio.run(
        audit_store.record(_event(BOOKING_TENANT, action="trace.read", principal="operator-9"))
    )
    asyncio.run(
        audit_store.record(
            _event(BOOKING_TENANT, action="trace.read_refused", principal="operator-9")
        )
    )
    headers = _admin_headers(operator_headers)

    response = client.get("/api/admin/audit", params={"tenant_id": BOOKING_TENANT}, headers=headers)

    by_action = {event["action"]: event for event in response.json()["events"]}
    assert "platform_admin" in by_action["membership_assigned"]["permission"]
    assert "trace_viewer" in by_action["trace.read"]["permission"]
    assert "refused" in by_action["trace.read_refused"]["permission"]
    # The console's own read appears in the next read, carrying its permission.
    from tenantchat.api.access import authorizing_permission

    assert "tenant_admin" in authorizing_permission("audit.read")
    assert "tenant_admin" in authorizing_permission("permissions.read")


@pytest.mark.security
def test_no_content_field_is_reachable_from_any_audit_projection(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    """The projection is bounded fields only, even when the store holds content.

    The ``details`` dict is the server-authored context that could, by bug or
    by a future lane, hold content; it never crosses this surface. Stufng a
    prompt, an answer, and a contact detail into a row and reading the trail
    back must surface none of them (`ADR-0010`).
    """
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    session_id = uuid.uuid4()
    asyncio.run(
        audit_store.record(
            _event(
                BOOKING_TENANT,
                action="staff_reply_sent",
                request_id="req-content",
                resource_id=session_id,
                details={
                    "prompt": "the assembled prompt",
                    "evidence": "the retrieved chunk",
                    "answer": "the model answer",
                    "contact": "dana@example.com",
                },
            )
        )
    )
    headers = _admin_headers(operator_headers)

    response = client.get("/api/admin/audit", params={"tenant_id": BOOKING_TENANT}, headers=headers)

    assert response.status_code == 200
    body = response.text
    for leaked in ("the assembled prompt", "the retrieved chunk", "the model answer", "dana@example.com"):
        assert leaked not in body

    row = next(event for event in response.json()["events"] if event["action"] == "staff_reply_sent")
    assert set(row) == AUDIT_PROJECTION_KEYS
    assert row["request_id"] == "req-content"
    assert row["resource_type"] == "chat_session"
    assert row["resource_id"] == str(session_id)


def test_a_tenant_admin_reads_the_permissions_view_with_grantors(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    """Who granted a role and when comes from the trail, never invented."""
    platform = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})
    assignment = client.post(
        "/api/admin/memberships",
        json={"tenant_id": BOOKING_TENANT, "subject": "operator-42", "role": "tenant_admin"},
        headers=platform | {CSRF_HEADER: _csrf_for(client, platform)},
    )
    assert assignment.status_code == 201
    _make_tenant_admin(membership_store, BOOKING_TENANT)

    response = client.get(
        "/api/admin/permissions",
        params={"tenant_id": BOOKING_TENANT},
        headers=_admin_headers(operator_headers),
    )

    assert response.status_code == 200
    roles = {role["subject"]: role for role in response.json()["roles"]}
    granted = roles["operator-42"]
    assert granted["role"] == "tenant_admin"
    assert granted["granted_by"] == "platform-1"
    assert granted["granted_at"]

    rows = asyncio.run(audit_store.for_tenant(BOOKING_TENANT))
    assignment_row = next(row for row in rows if row.action == "membership_assigned")
    assert datetime.fromisoformat(granted["granted_at"].replace("Z", "+00:00")) == (
        assignment_row.occurred_at
    )


def test_a_role_seeded_without_an_assignment_shows_no_invented_grantor(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
) -> None:
    _make_tenant_admin(membership_store, BOOKING_TENANT)

    response = client.get(
        "/api/admin/permissions",
        params={"tenant_id": BOOKING_TENANT},
        headers=_admin_headers(operator_headers),
    )

    roles = {role["subject"]: role for role in response.json()["roles"]}
    assert roles[TEST_OPERATOR_SUBJECT]["role"] == "tenant_admin"
    assert roles[TEST_OPERATOR_SUBJECT]["granted_by"] is None


def test_trace_read_grants_are_listed_as_a_separate_control(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
) -> None:
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    platform = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})
    granted = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": BOOKING_TENANT, "subject": "operator-9"},
        headers=platform | {CSRF_HEADER: _csrf_for(client, platform)},
    )
    assert granted.status_code == 201

    response = client.get(
        "/api/admin/permissions",
        params={"tenant_id": BOOKING_TENANT},
        headers=_admin_headers(operator_headers),
    )

    body = response.json()
    grants = {grant["subject"]: grant for grant in body["grants"]}
    assert grants["operator-9"]["granted_by"] == "platform-1"
    # The grant and the role are different controls on different lists.
    assert "operator-9" not in {role["subject"] for role in body["roles"]}
    assert body["grants"] and body["roles"]


def test_revoking_a_role_or_a_grant_is_visible_without_a_redeploy(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
) -> None:
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    platform = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})
    csrf = _csrf_for(client, platform)
    client.post(
        "/api/admin/memberships",
        json={"tenant_id": BOOKING_TENANT, "subject": "operator-42", "role": "viewer"},
        headers=platform | {CSRF_HEADER: csrf},
    )
    client.post(
        "/api/admin/trace-access",
        json={"tenant_id": BOOKING_TENANT, "subject": "operator-9"},
        headers=platform | {CSRF_HEADER: csrf},
    )
    headers = _admin_headers(operator_headers)

    before = client.get(
        "/api/admin/permissions", params={"tenant_id": BOOKING_TENANT}, headers=headers
    ).json()
    assert "operator-42" in {role["subject"] for role in before["roles"]}
    assert "operator-9" in {grant["subject"] for grant in before["grants"]}

    assert client.delete(
        "/api/admin/memberships",
        params={"tenant_id": BOOKING_TENANT, "subject": "operator-42"},
        headers=platform | {CSRF_HEADER: csrf},
    ).status_code == 204
    assert client.delete(
        "/api/admin/trace-access",
        params={"tenant_id": BOOKING_TENANT, "subject": "operator-9"},
        headers=platform | {CSRF_HEADER: csrf},
    ).status_code == 204

    after = client.get(
        "/api/admin/permissions", params={"tenant_id": BOOKING_TENANT}, headers=headers
    ).json()
    assert "operator-42" not in {role["subject"] for role in after["roles"]}
    assert "operator-9" not in {grant["subject"] for grant in after["grants"]}


@pytest.mark.security
def test_a_revoked_principals_later_attempt_appears_in_the_trail_as_a_refusal(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
) -> None:
    """The refusal is a record, not an absence.

    After the trace-read grant is revoked, the operator's next read attempt
    writes a `trace.read_refused` row that the tenant admin can see on the
    trail — the attempt is not silently dropped.
    """
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    platform = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})
    csrf = _csrf_for(client, platform)
    granted = client.post(
        "/api/admin/trace-access",
        json={"tenant_id": BOOKING_TENANT, "subject": TEST_OPERATOR_SUBJECT},
        headers=platform | {CSRF_HEADER: csrf},
    )
    assert granted.status_code == 201
    revoked = client.delete(
        "/api/admin/trace-access",
        params={"tenant_id": BOOKING_TENANT, "subject": TEST_OPERATOR_SUBJECT},
        headers=platform | {CSRF_HEADER: csrf},
    )
    assert revoked.status_code == 204

    attempt = client.get(
        "/api/admin/traces",
        params={"tenant_id": BOOKING_TENANT, "reason": "quality_review"},
        headers=operator_headers(),
    )
    assert attempt.status_code == 403
    assert attempt.json()["code"] == "forbidden"

    trail = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "action": "trace.read_refused"},
        headers=_admin_headers(operator_headers),
    )
    assert trail.status_code == 200
    refusals = [event for event in trail.json()["events"] if event["action"] == "trace.read_refused"]
    assert len(refusals) == 1
    assert refusals[0]["principal"] == TEST_OPERATOR_SUBJECT
    assert "refused" in refusals[0]["permission"]


def test_a_membership_revocation_is_a_trail_record(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
) -> None:
    """Revoking a role is an audited action, visible on the trail."""
    _make_tenant_admin(membership_store, BOOKING_TENANT)
    platform = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})
    csrf = _csrf_for(client, platform)
    client.post(
        "/api/admin/memberships",
        json={"tenant_id": BOOKING_TENANT, "subject": "operator-42", "role": "viewer"},
        headers=platform | {CSRF_HEADER: csrf},
    )
    client.delete(
        "/api/admin/memberships",
        params={"tenant_id": BOOKING_TENANT, "subject": "operator-42"},
        headers=platform | {CSRF_HEADER: csrf},
    )

    trail = client.get(
        "/api/admin/audit",
        params={"tenant_id": BOOKING_TENANT, "action": "membership_revoked"},
        headers=_admin_headers(operator_headers),
    )

    revoked = [event for event in trail.json()["events"] if event["action"] == "membership_revoked"]
    assert len(revoked) == 1
    assert revoked[0]["principal"] == "platform-1"
    assert "platform_admin" in revoked[0]["permission"]


def test_the_permissions_read_is_itself_audited(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: object,
    audit_store: InMemoryAuditStore,
) -> None:
    _make_tenant_admin(membership_store, BOOKING_TENANT)

    response = client.get(
        "/api/admin/permissions",
        params={"tenant_id": BOOKING_TENANT},
        headers=_admin_headers(operator_headers),
    )

    assert response.status_code == 200
    rows = asyncio.run(audit_store.for_tenant(BOOKING_TENANT))
    read = next(row for row in rows if row.action == "permissions.read")
    assert read.principal_id == TEST_OPERATOR_SUBJECT
    assert read.resource_type == "tenant_permissions"
