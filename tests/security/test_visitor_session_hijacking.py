"""SEC-002 regression suite: session hijacking and tenant reassignment.

The vulnerabilities this guards against, named as an operator would report
them:

- **Session hijacking** — an attacker who learns a conversation's unguessable
  session UUID (from a URL, a log line, or a guess) could previously read the
  whole transcript and keep writing to it. A captured UUID must now authorize
  nothing; the credential that names it is a signed token, so nothing about a
  session can be copied out of a URL.
- **Tenant reassignment** — an attacker could previously edit the ``tenant_id``
  on a request to move a session into another tenant's view, or even run
  another tenant's tools against a stolen session. The tenant now comes from
  the verified credential, and a request body cannot name a tenant at all.

Each test runs against the API exactly as a caller would reach it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.tests.conftest import (
    BOOKING_TENANT,
    ScriptedModel,
    VisitorSession,
    booking_call,
)
from tenantchat.api.store import BookingRecord
from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER
from tenantchat.orchestration.model import ModelResponse


class TestSessionHijacking:
    """A captured or guessed session ID must authorize nothing."""

    def test_a_session_uuid_is_not_a_credential(
        self, client: TestClient, open_session: Callable[..., str]
    ) -> None:
        """The old API took the UUID in the URL and body; both shapes are gone,
        so a leaked UUID has no place to be presented."""
        session_id = open_session(BOOKING_TENANT)

        read = client.get("/api/chat/session")
        posted = client.post("/api/chat", json={"session_id": session_id, "message": "Hello"})

        assert read.status_code == 401
        assert posted.status_code == 401

    def test_a_missing_credential_and_a_forged_one_are_indistinguishable(
        self, client: TestClient
    ) -> None:
        """The 401 must not say which half failed, or a forger learns what to fix."""
        absent = client.get("/api/chat/session")
        garbage = client.get(
            "/api/chat/session", headers={VISITOR_CREDENTIAL_HEADER: "not-a-token"}
        )

        assert absent.status_code == 401
        assert garbage.status_code == 401
        assert absent.json()["code"] == garbage.json()["code"] == "invalid_visitor_credential"

    def test_a_tampered_credential_authorizes_nothing(
        self, client: TestClient, visitor_session: Callable[..., VisitorSession]
    ) -> None:
        """Flipping one character breaks the signature, not the claims."""
        visitor = visitor_session()
        tampered = visitor.credential[:-1] + ("A" if visitor.credential[-1] == "B" else "B")

        response = client.get("/api/chat/session", headers={VISITOR_CREDENTIAL_HEADER: tampered})

        assert response.status_code == 401
        assert response.json()["code"] == "invalid_visitor_credential"

    def test_an_expired_credential_is_a_stable_recoverable_failure(
        self, client: TestClient, visitor_session: Callable[..., VisitorSession]
    ) -> None:
        """Expiry is its own code, distinct from forgery, and the recovery —
        open a fresh session — works immediately afterwards."""
        app = cast(FastAPI, client.app)
        visitor = visitor_session()
        issued_at = app.state.clock()
        ttl = app.state.settings.visitor_credential_ttl_seconds
        app.state.clock = lambda: issued_at + timedelta(seconds=ttl + 1)

        expired = client.get("/api/chat/session", headers=visitor.headers)

        assert expired.status_code == 401
        assert expired.json()["code"] == "visitor_credential_expired"

        app.state.clock = lambda: issued_at
        reopened = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
        assert reopened.status_code == 201


class TestTenantReassignment:
    """A request body can no longer move anything between tenants."""

    def test_no_route_accepts_a_tenant_field_anymore(
        self, client: TestClient, visitor_session: Callable[..., VisitorSession]
    ) -> None:
        """`extra="forbid"` on every request model: naming another tenant is a
        malformed request, which is how "stop accepting tenant changes for
        existing sessions" is enforced on the wire."""
        visitor = visitor_session()

        turn = client.post(
            "/api/chat",
            json={"tenant_id": "apex", "message": "Hello"},
            headers=visitor.headers,
        )
        confirmation = client.post(
            "/api/chat/confirmation",
            json={"tenant_id": "apex", "decision": "approved"},
            headers=visitor.headers,
        )

        assert turn.status_code == 422
        assert confirmation.status_code == 422
        assert turn.json()["code"] == confirmation.json()["code"] == "malformed_request"

    def test_a_confirmed_booking_is_recorded_under_the_credentials_tenant(
        self,
        client: TestClient,
        model: ScriptedModel,
        visitor_session: Callable[..., VisitorSession],
    ) -> None:
        """ "Invoke another tenant's tools": the graph commits with the tenant
        from the credential. Approving under clearview's credential must leave
        the booking in clearview's store and nowhere else."""
        model.script = [
            ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
            ModelResponse(content="You are booked.", model_name="scripted"),
        ]
        visitor = visitor_session(BOOKING_TENANT)
        client.post("/api/chat", json={"message": "Book HVAC"}, headers=visitor.headers)

        approved = client.post(
            "/api/chat/confirmation", json={"decision": "approved"}, headers=visitor.headers
        )

        assert approved.status_code == 200
        assert [effect["action"] for effect in approved.json()["committed"]] == ["book_appointment"]
        clearview_bookings = _bookings_for_tenant(client, "clearview")
        apex_bookings = _bookings_for_tenant(client, "apex")
        assert len(clearview_bookings) == 1
        assert clearview_bookings[0].session_id == visitor.session_id
        assert apex_bookings == ()

    def test_a_credential_minted_for_another_tenants_session_cannot_read_it(
        self,
        client: TestClient,
        visitor_session: Callable[..., VisitorSession],
        mint_credential: Callable[..., str],
    ) -> None:
        """The signing key cannot be reached by a caller, but if a credential
        for tenant A were somehow minted with tenant B's session ID, the store
        read still happens under the *credential's* tenant — the session row
        belongs to B, so the read is a 404, not a leak."""
        apex = visitor_session("apex")
        misbound = {VISITOR_CREDENTIAL_HEADER: mint_credential(BOOKING_TENANT, apex.session_id)}

        response = client.get("/api/chat/session", headers=misbound)

        assert response.status_code == 404


def _bookings_for_tenant(client: TestClient, tenant_id: str) -> tuple[BookingRecord, ...]:
    import asyncio

    app = cast(FastAPI, client.app)
    return asyncio.run(app.state.booking_store.for_tenant(tenant_id))
