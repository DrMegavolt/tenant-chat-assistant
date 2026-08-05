"""SEC-001: per-tenant role enforcement on the operator surface.

These are the exploit-class regressions the global DoD routes to security
tests: a tenant scope that can be probed, widened, or silently retained, and a
privilege ceiling that a membership row can breach. The happy paths live in
``test_admin.py``; everything here is a caller that must be refused, or a grant
that must prove it worked.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from services.api.tests.conftest import (
    BOOKING_TENANT,
    TEST_OPERATOR_SUBJECT,
)
from tenantchat.api.app import create_app
from tenantchat.api.identity import (
    CSRF_HEADER,
    EMAIL_HEADER,
    ROLE_HEADER,
    SUBJECT_HEADER,
)
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    AuditActorType,
    InMemoryAuditStore,
    InMemoryBookingStore,
    InMemoryConversationStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryLeadStore,
    InMemoryMembershipStore,
)

UNAFFILIATED = "operator-99"


def _no_membership_headers(
    operator_headers: Callable[..., dict[str, str]],
) -> dict[str, str]:
    return operator_headers(**{SUBJECT_HEADER: UNAFFILIATED})


def _csrf_for(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    token: str = response.json()["csrf_token"]
    return token


TENANT_ROUTES: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("GET", "/api/admin/chats", {"tenant_id": BOOKING_TENANT}),
    ("GET", "/api/admin/leads", {"tenant_id": BOOKING_TENANT}),
    ("GET", "/api/admin/bookings", {"tenant_id": BOOKING_TENANT}),
)


@pytest.mark.security
@pytest.mark.parametrize(("method", "path", "params"), TENANT_ROUTES)
def test_an_operator_without_a_membership_reaches_no_tenant_data(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    method: str,
    path: str,
    params: dict[str, str],
) -> None:
    response = client.request(
        method, path, params=params, headers=_no_membership_headers(operator_headers)
    )

    assert response.status_code == 403
    assert response.json()["code"] == "tenant_access_denied"


@pytest.mark.security
def test_an_unaffiliated_operator_cannot_send_a_staff_reply(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    open_session: Callable[..., str],
) -> None:
    session_id = open_session()
    headers = _no_membership_headers(operator_headers)

    response = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "On my way."},
        headers=headers | {CSRF_HEADER: _csrf_for(client, headers)},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "tenant_access_denied"


@pytest.mark.security
def test_a_missing_tenant_is_indistinguishable_from_a_forbidden_one(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> None:
    """The refusal must not confirm whether a tenant exists.

    A 404 would hand an unaffiliated caller a tenant registry one entry at a
    time; the detail must carry no tenant identifier either, so a probe and a
    refusal are byte-identical documents.
    """
    headers = _no_membership_headers(operator_headers)

    existing = client.get(
        "/api/admin/chats", params={"tenant_id": BOOKING_TENANT}, headers=headers
    ).json()
    phantom = client.get(
        "/api/admin/chats", params={"tenant_id": "tenant-that-never-existed"}, headers=headers
    ).json()

    # The request ID is per-request by design; every other field must match.
    assert {key: value for key, value in existing.items() if key != "requestId"} == {
        key: value for key, value in phantom.items() if key != "requestId"
    }
    assert BOOKING_TENANT not in existing["detail"]
    assert "tenant-that-never-existed" not in phantom["detail"]


@pytest.mark.security
def test_a_membership_cannot_widen_the_directory_role(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
    open_session: Callable[..., str],
) -> None:
    """The identity provider remains the privilege ceiling (SEC-001).

    A tenant row granting ``tenant_admin`` must not let a directory-level
    ``viewer`` speak to customers: the effective role is the tighter of the two.
    """
    session_id = open_session()
    asyncio.run(
        membership_store.assign(
            tenant_id=BOOKING_TENANT, subject=TEST_OPERATOR_SUBJECT, role="tenant_admin"
        )
    )
    headers = operator_headers(role="viewer")

    response = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "On my way."},
        headers=headers | {CSRF_HEADER: _csrf_for(client, headers)},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


@pytest.mark.security
def test_a_membership_can_narrow_the_directory_role(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
    open_session: Callable[..., str],
) -> None:
    session_id = open_session()
    asyncio.run(
        membership_store.assign(
            tenant_id=BOOKING_TENANT, subject=TEST_OPERATOR_SUBJECT, role="viewer"
        )
    )
    headers = operator_headers(role="support_agent")

    listing = client.get("/api/admin/chats", params={"tenant_id": BOOKING_TENANT}, headers=headers)
    reply = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "On my way."},
        headers=headers | {CSRF_HEADER: _csrf_for(client, headers)},
    )

    assert listing.status_code == 200
    assert reply.status_code == 403
    assert reply.json()["code"] == "forbidden"


@pytest.mark.security
def test_a_platform_administrator_spans_every_tenant_without_a_membership(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    open_session: Callable[..., str],
) -> None:
    session_id = open_session()
    headers = operator_headers(role="platform_admin", **{SUBJECT_HEADER: UNAFFILIATED})

    listing = client.get("/api/admin/chats", params={"tenant_id": BOOKING_TENANT}, headers=headers)
    reply = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "On my way."},
        headers=headers | {CSRF_HEADER: _csrf_for(client, headers)},
    )

    assert listing.status_code == 200
    assert reply.status_code == 201


@pytest.mark.security
def test_revoked_access_is_gone_immediately(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    asyncio.run(membership_store.revoke(tenant_id=BOOKING_TENANT, subject=TEST_OPERATOR_SUBJECT))

    response = client.get(
        "/api/admin/chats", params={"tenant_id": BOOKING_TENANT}, headers=operator_headers()
    )

    assert response.status_code == 403
    assert response.json()["code"] == "tenant_access_denied"


def test_assigned_access_takes_effect_without_a_restart(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    audit_store: InMemoryAuditStore,
) -> None:
    """Membership is read per request, so a grant needs no process reload.

    The platform administrator grants operator-99 viewer access; the granted
    operator must then read the tenant immediately, and the grant must be on
    the accountability log with the assigning principal.
    """
    admin = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})
    granted = _no_membership_headers(operator_headers)

    grant = client.post(
        "/api/admin/memberships",
        json={"tenant_id": BOOKING_TENANT, "subject": UNAFFILIATED, "role": "viewer"},
        headers=admin | {CSRF_HEADER: _csrf_for(client, admin)},
    )
    listing = client.get("/api/admin/chats", params={"tenant_id": BOOKING_TENANT}, headers=granted)

    assert grant.status_code == 201
    assert listing.status_code == 200

    rows = asyncio.run(audit_store.for_tenant(BOOKING_TENANT))
    assignment = [row for row in rows if row.action == "membership_assigned"]
    assert len(assignment) == 1
    assert assignment[0].principal_id == "platform-1"
    assert assignment[0].actor_type == AuditActorType.STAFF
    assert assignment[0].details == {"subject": UNAFFILIATED, "role": "viewer"}


@pytest.mark.security
def test_only_a_platform_administrator_may_grant_membership(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    audit_store: InMemoryAuditStore,
) -> None:
    """The power to grant roles is the highest role, so a tenant admin may not.

    The attempted grant must leave the store untouched and the audit log
    silent: a refused mutation is a refused mutation, not a logged one.
    """
    headers = operator_headers(role="tenant_admin")

    response = client.post(
        "/api/admin/memberships",
        json={"tenant_id": BOOKING_TENANT, "subject": UNAFFILIATED, "role": "viewer"},
        headers=headers | {CSRF_HEADER: _csrf_for(client, headers)},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"
    assert asyncio.run(audit_store.for_tenant(BOOKING_TENANT)) == ()


@pytest.mark.security
def test_a_membership_mutation_without_a_csrf_token_is_refused(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> None:
    headers = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})

    response = client.post(
        "/api/admin/memberships",
        json={"tenant_id": BOOKING_TENANT, "subject": UNAFFILIATED, "role": "viewer"},
        headers=headers,
    )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_validation_failed"


def test_membership_routes_reject_an_unknown_tenant(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> None:
    """An unknown tenant is a 404 for the assigner, not a database violation."""
    headers = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})

    response = client.post(
        "/api/admin/memberships",
        json={"tenant_id": "tenant-that-never-existed", "subject": UNAFFILIATED, "role": "viewer"},
        headers=headers | {CSRF_HEADER: _csrf_for(client, headers)},
    )

    assert response.status_code == 404


def test_a_staff_reply_is_audited_with_principal_tenant_and_request(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    audit_store: InMemoryAuditStore,
    open_session: Callable[..., str],
) -> None:
    """SEC-001 accountability: who spoke, to which tenant, under which request.

    The request ID links the row to the gateway's log line, and the principal
    is the subject rather than the email, which is the pseudonymous ID the
    identity provider owns.
    """
    session_id = open_session()
    headers = operator_headers()

    response = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "I can be there at four."},
        headers=headers | {CSRF_HEADER: _csrf_for(client, headers)},
    )
    request_id = response.headers["x-request-id"]

    rows = asyncio.run(audit_store.for_tenant(BOOKING_TENANT))
    reply = [row for row in rows if row.action == "staff_reply_sent"]
    assert len(reply) == 1
    assert reply[0].principal_id == TEST_OPERATOR_SUBJECT
    assert reply[0].actor_type == AuditActorType.STAFF
    assert reply[0].resource_type == "chat_session"
    assert str(reply[0].resource_id) == session_id
    assert reply[0].request_id == request_id
    assert reply[0].occurred_at is not None


@pytest.mark.security
def test_a_refused_operator_never_touches_the_tenant(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    audit_store: InMemoryAuditStore,
    open_session: Callable[..., str],
) -> None:
    """The scope check runs before any store write, so a refusal writes nothing."""
    session_id = open_session()
    headers = _no_membership_headers(operator_headers)

    response = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "On my way."},
        headers=headers | {CSRF_HEADER: _csrf_for(client, headers)},
    )

    assert response.status_code == 403
    assert asyncio.run(audit_store.for_tenant(BOOKING_TENANT)) == ()


def test_a_revocation_is_audited_even_when_the_membership_never_existed(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    audit_store: InMemoryAuditStore,
) -> None:
    """The intent is the record: a platform administrator asked for it."""
    headers = operator_headers(role="platform_admin", **{SUBJECT_HEADER: "platform-1"})

    response = client.delete(
        "/api/admin/memberships",
        params={"tenant_id": BOOKING_TENANT, "subject": UNAFFILIATED},
        headers=headers | {CSRF_HEADER: _csrf_for(client, headers)},
    )

    assert response.status_code == 204
    rows = asyncio.run(audit_store.for_tenant(BOOKING_TENANT))
    assert [row.action for row in rows] == ["membership_revoked"]


def test_development_auth_serves_the_operator_without_a_gateway(
    monkeypatch: pytest.MonkeyPatch,
    membership_store: InMemoryMembershipStore,
) -> None:
    """CHAT_API_DEV_AUTH trusts the identity headers directly (SEC-001).

    The production composition refuses to start in this mode against a remote
    database, so the trust is confined to a developer's laptop by construction.
    """
    monkeypatch.setenv("CHAT_API_DEV_AUTH", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://user@127.0.0.1/db")
    monkeypatch.delenv("ADMIN_GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_CSRF_SECRET", raising=False)

    deployed = replace(
        Settings.from_environment(),
        allowed_origins=("http://127.0.0.1:8000",),
    )
    assert deployed.dev_auth
    assert deployed.admin_gateway_token is None
    # The loopback-only mode mints its own CSRF secret rather than demanding one.
    assert deployed.admin_csrf_secret

    app = create_app(
        deployed,
        booking_store=InMemoryBookingStore(),
        lead_store=InMemoryLeadStore(),
        conversation_store=InMemoryConversationStore(),
        handoff_store=InMemoryHandoffStore(),
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=membership_store,
        audit_store=InMemoryAuditStore(),
    )

    with TestClient(app) as dev_client:
        headers = {
            SUBJECT_HEADER: TEST_OPERATOR_SUBJECT,
            EMAIL_HEADER: "operator@example.com",
            ROLE_HEADER: "support_agent",
        }
        token = dev_client.get("/api/admin/csrf-token", headers=headers)
        assert token.status_code == 200
        assert token.json()["csrf_token"]


@pytest.mark.security
def test_production_refuses_to_start_with_development_auth_enabled(
    settings: Settings,
) -> None:
    """A remote database with dev auth is the production shape and must not start."""
    deployed = replace(
        settings,
        dev_auth=True,
        admin_gateway_token=None,
        admin_csrf_secret=None,
        database_url="postgresql+psycopg://user@tenant-chat-prod.example.com/db",
    )

    with pytest.raises(ValueError, match="CHAT_API_DEV_AUTH is enabled"):
        create_app(deployed)
