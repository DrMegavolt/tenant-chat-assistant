"""FEAT-004: the staff handoff queue, the ownership transaction, and the visitor gate.

Half the file is the visitor gate — the agent must stay quiet while a staff
member owns the conversation, resume on release without committing a second
time, and tell the visitor queue, takeover, and resolution without any staff
identity — and half is the staff surface: a race to accept has exactly one
winner, release and resolution honor single ownership, every mutation is
audited to a principal and a request ID, and nothing crosses tenants.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.tests.conftest import ScriptedModel
from tenantchat.api.app import create_app
from tenantchat.api.identity import (
    CSRF_HEADER,
    EMAIL_HEADER,
    GATEWAY_TOKEN_HEADER,
    ROLE_HEADER,
    SUBJECT_HEADER,
)
from tenantchat.api.registry import TenantRegistry
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
)
from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER
from tenantchat.core.commands import HandoffCommand
from tenantchat.core.errors import HandoffTransitionError
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import ModelResponse

QUEUE_NOTICE = "You're in the queue for a member of the team"
TAKEOVER_NOTICE = "A member of the team is now with you"
CLOSED_NOTICE = "This conversation is now closed"

OPERATOR = "operator-7"
SECOND_OPERATOR = "operator-8"
SUPERVISOR = "operator-9"


def csrf_for(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    token: str = response.json()["csrf_token"]
    return token


def operator_headers(subject: str = OPERATOR, role: str = "support_agent") -> dict[str, str]:
    return {
        GATEWAY_TOKEN_HEADER: "gateway-token-for-tests",
        SUBJECT_HEADER: subject,
        EMAIL_HEADER: f"{subject}@example.com",
        ROLE_HEADER: role,
    }


@pytest.fixture
def staff_console() -> (
    Iterator[tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel]]
):
    """An app with a shared handoff store, audit log, and model to inspect.

    The shared store is the point: the HTTP surface and the assertions read
    the same rows the transitions wrote, so a test proves durable state, not
    just the response it got back. The model is returned unwrapped so a test
    can assert how many turns the graph actually ran.
    """
    audit = InMemoryAuditStore()
    conversations = InMemoryConversationStore(audit=audit)
    consent = InMemoryConsentStore()
    handoffs = InMemoryHandoffStore(audit=audit)
    memberships = InMemoryMembershipStore()
    for tenant in ("clearview", "apex"):
        for subject in (OPERATOR, SECOND_OPERATOR):
            asyncio.run(memberships.assign(tenant_id=tenant, subject=subject, role="support_agent"))
        asyncio.run(memberships.assign(tenant_id=tenant, subject=SUPERVISOR, role="tenant_admin"))
    settings = Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=2048,
        docs_enabled=True,
        admin_gateway_token="gateway-token-for-tests",
        admin_csrf_secret="csrf-secret-for-tests",
        dev_auth=False,
    )
    model = ScriptedModel([ModelResponse(content="We are open until 7pm.", model_name="scripted")])

    with TestClient(
        create_app(
            settings,
            booking_store=InMemoryBookingStore(),
            lead_store=InMemoryLeadStore(),
            conversation_store=conversations,
            handoff_store=handoffs,
            idempotency_store=InMemoryIdempotencyStore(),
            membership_store=memberships,
            consent_store=consent,
            privacy_store=InMemoryPrivacyStore(
                conversations,
                InMemoryBookingStore(),
                InMemoryLeadStore(),
                handoffs,
                consent,
            ),
            audit_store=audit,
            chat_model=model,
            checkpointer=InMemorySaver(),
        ),
        raise_server_exceptions=False,
    ) as client:
        yield client, handoffs, audit, model


def record_handoff(
    handoffs: InMemoryHandoffStore,
    tenant_id: str,
    session_id: str,
    *,
    reason: str = "customer_request",
    summary: str = "Customer asked to speak to a person about a warranty claim.",
) -> str:
    policy = TenantRegistry.seeded().get(tenant_id).policy
    command = HandoffCommand.parse(policy, reason=reason, summary=summary)

    async def run() -> str:
        record = await handoffs.record(command, session_id=session_id)
        return record.handoff_id

    return asyncio.run(run())


def accept(client: TestClient, headers: dict[str, str], handoff_id: str, tenant_id: str) -> Any:
    return client.post(
        f"/api/admin/handoffs/{handoff_id}/accept",
        params={"tenant_id": tenant_id},
        headers=headers | {CSRF_HEADER: csrf_for(client, headers)},
    )


def release(client: TestClient, headers: dict[str, str], handoff_id: str, tenant_id: str) -> Any:
    return client.post(
        f"/api/admin/handoffs/{handoff_id}/release",
        params={"tenant_id": tenant_id},
        headers=headers | {CSRF_HEADER: csrf_for(client, headers)},
    )


def resolve(client: TestClient, headers: dict[str, str], handoff_id: str, tenant_id: str) -> Any:
    return client.post(
        f"/api/admin/handoffs/{handoff_id}/resolve",
        params={"tenant_id": tenant_id},
        headers=headers | {CSRF_HEADER: csrf_for(client, headers)},
    )


def visitor_message(client: TestClient, credential: str, text: str) -> Any:
    return client.post(
        "/api/chat",
        json={"message": text},
        headers={VISITOR_CREDENTIAL_HEADER: credential},
    )


def open_visitor_session(client: TestClient, tenant_id: str = "clearview") -> tuple[str, str]:
    response = client.post("/api/chat/session", json={"tenant_id": tenant_id})
    assert response.status_code == 201, response.text
    body = response.json()
    return body["session"]["session_id"], body["credential"]


def audit_actions(audit: InMemoryAuditStore, tenant_id: str) -> list[str]:
    async def load() -> list[str]:
        events = await audit.for_tenant(tenant_id)
        return [event.action for event in events if event.action.startswith("handoff.")]

    return asyncio.run(load())


class TestSingleOwnershipUnderRace:
    def test_a_race_to_accept_has_exactly_one_winner(self) -> None:
        """Two consoles accepting at once leave exactly one staff owner.

        The store serializes the transition, and the loser reads the winner's
        committed assignment instead of its own snapshot — the database, not a
        UI affordance, is what decides.
        """
        handoffs = InMemoryHandoffStore()
        handoff_id = record_handoff(handoffs, "clearview", "session-race")

        async def race() -> Any:
            return await asyncio.gather(
                handoffs.accept("clearview", handoff_id, principal_id=OPERATOR),
                handoffs.accept("clearview", handoff_id, principal_id=SECOND_OPERATOR),
                return_exceptions=True,
            )

        outcomes = asyncio.run(race())
        winners = [
            result.assigned_principal_id for result in outcomes if not isinstance(result, Exception)
        ]
        refused = sum(1 for result in outcomes if isinstance(result, HandoffTransitionError))

        assert refused == 1
        assert winners == [OPERATOR] or winners == [SECOND_OPERATOR]

        async def current() -> str | None:
            record = await handoffs.for_session("clearview", "session-race")
            return None if record is None else record.assigned_principal_id

        assert asyncio.run(current()) in (OPERATOR, SECOND_OPERATOR)

    def test_a_losing_accept_sees_a_transition_error_not_a_blank(self) -> None:
        handoffs = InMemoryHandoffStore()
        handoff_id = record_handoff(handoffs, "clearview", "session-race")
        asyncio.run(handoffs.accept("clearview", handoff_id, principal_id=OPERATOR))

        async def second() -> None:
            with pytest.raises(HandoffTransitionError):
                await handoffs.accept("clearview", handoff_id, principal_id=SECOND_OPERATOR)

        asyncio.run(second())

    def test_a_released_handoff_can_be_accepted_again_by_one_winner(self) -> None:
        """Release returns the conversation to the queue, not to no one's control."""
        handoffs = InMemoryHandoffStore()
        handoff_id = record_handoff(handoffs, "clearview", "session-race")
        asyncio.run(handoffs.accept("clearview", handoff_id, principal_id=OPERATOR))
        asyncio.run(handoffs.release("clearview", handoff_id, principal_id=OPERATOR))

        async def race() -> Any:
            return await asyncio.gather(
                handoffs.accept("clearview", handoff_id, principal_id=OPERATOR),
                handoffs.accept("clearview", handoff_id, principal_id=SECOND_OPERATOR),
                return_exceptions=True,
            )

        outcomes = asyncio.run(race())
        assert sum(1 for outcome in outcomes if not isinstance(outcome, Exception)) == 1
        assert sum(1 for outcome in outcomes if isinstance(outcome, HandoffTransitionError)) == 1


class TestTheVisitorTurnGate:
    def test_a_visitor_message_while_queued_gets_the_queue_notice_and_no_model(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, model = staff_console
        session_id, credential = open_visitor_session(client)
        record_handoff(handoffs, "clearview", session_id)

        response = visitor_message(client, credential, "hello?")

        assert response.status_code == 200
        assert QUEUE_NOTICE in response.json()["reply"]
        assert response.json()["turn_id"] is None
        assert model.calls == []
        # The visitor's message was stored — history stays server-authoritative —
        # and the same notice is in the transcript beside it.
        transcript = client.get(
            "/api/chat/session", headers={VISITOR_CREDENTIAL_HEADER: credential}
        ).json()["messages"]
        assert any(
            message["role"] == "visitor" and message["content"] == "hello?"
            for message in transcript
        )
        assert any(
            message["role"] == "system" and QUEUE_NOTICE in message["content"]
            for message in transcript
        )

    def test_a_visitor_message_during_takeover_gets_the_takeover_notice(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, model = staff_console
        session_id, credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)

        accepted = accept(client, operator_headers(), handoff_id, "clearview")
        assert accepted.status_code == 201

        response = visitor_message(client, credential, "are you there?")

        assert response.status_code == 200
        assert TAKEOVER_NOTICE in response.json()["reply"]
        # No model answer: the agent is paused during active takeover.
        assert model.calls == []

    def test_a_released_handoff_resumes_the_agent(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        """Release is the explicit invitation: the next message runs the graph."""
        client, handoffs, _audit, model = staff_console
        session_id, credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        accept(client, operator_headers(), handoff_id, "clearview")

        released = release(client, operator_headers(), handoff_id, "clearview")
        assert released.status_code == 201
        assert released.json()["handoff"]["status"] == "queued"

        response = visitor_message(client, credential, "now can you help?")

        assert response.status_code == 200
        assert response.json()["reply"] == "We are open until 7pm."
        assert len(model.calls) >= 1
        assert response.json()["turn_id"] is not None

    def test_a_visitor_message_after_resolution_gets_the_closure_notice(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        accept(client, operator_headers(), handoff_id, "clearview")
        resolved = resolve(client, operator_headers(), handoff_id, "clearview")
        assert resolved.status_code == 201

        response = visitor_message(client, credential, "thanks, bye")

        assert response.status_code == 200
        assert CLOSED_NOTICE in response.json()["reply"]

    def test_a_confirmation_while_queued_returns_the_notice_and_keeps_no_visible_turn(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        """Approving a booking during a handoff answers with the notice only.

        The confirmation is a graph run like any other, so the pause gate stops
        it too — and the notice must land in the transcript, exactly as a
        paused visitor message does, so a staff member reading the conversation
        later can see what the visitor was told.
        """
        client, handoffs, _audit, model = staff_console
        session_id, credential = open_visitor_session(client)
        record_handoff(handoffs, "clearview", session_id)

        response = client.post(
            "/api/chat/confirmation",
            json={"decision": "approved"},
            headers={VISITOR_CREDENTIAL_HEADER: credential},
        )

        assert response.status_code == 200
        assert QUEUE_NOTICE in response.json()["reply"]
        assert model.calls == []
        transcript = client.get(
            "/api/chat/session", headers={VISITOR_CREDENTIAL_HEADER: credential}
        ).json()["messages"]
        assert any(
            message["role"] == "system" and QUEUE_NOTICE in message["content"]
            for message in transcript
        )

    def test_the_notices_never_name_a_staff_member(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        accept(client, operator_headers(subject=SECOND_OPERATOR), handoff_id, "clearview")

        transcript = client.get(
            "/api/chat/session", headers={VISITOR_CREDENTIAL_HEADER: credential}
        ).json()["messages"]
        joined = " ".join(message["content"] for message in transcript)
        assert SECOND_OPERATOR not in joined
        # The internal queue position is a store concept; nothing on the
        # visitor surface may number the visitor's place in it.
        assert "position" not in joined.casefold()


class TestTheStaffQueueSurface:
    def test_the_queue_lists_only_open_handoffs(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, _credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        headers = operator_headers()

        listed = client.get(
            "/api/admin/handoffs", params={"tenant_id": "clearview"}, headers=headers
        )
        assert listed.status_code == 200
        rows = listed.json()["handoffs"]
        assert [row["handoff_id"] for row in rows] == [handoff_id]
        assert rows[0]["status"] == "requested"
        assert rows[0]["summary"].startswith("Customer asked")

        resolve(client, headers, handoff_id, "clearview")
        after = client.get(
            "/api/admin/handoffs", params={"tenant_id": "clearview"}, headers=headers
        ).json()["handoffs"]
        assert after == []

    def test_accept_release_resolve_are_audited_to_the_principal_and_request(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, audit, _model = staff_console
        session_id, _credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        headers = operator_headers(subject=SECOND_OPERATOR)

        accept(client, headers, handoff_id, "clearview")
        release(client, headers, handoff_id, "clearview")
        resolve(client, headers, handoff_id, "clearview")

        events = asyncio.run(audit.for_tenant("clearview"))
        actions = [event.action for event in events if event.action.startswith("handoff.")]
        assert sorted(actions) == ["handoff.accepted", "handoff.released", "handoff.resolved"]
        for event in events:
            if event.action.startswith("handoff."):
                assert event.principal_id == SECOND_OPERATOR
                assert event.tenant_id == "clearview"
                assert event.request_id
                assert event.resource_type == "handoff"

    def test_only_the_current_owner_or_a_supervisor_can_release_or_resolve(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, _credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        accept(client, operator_headers(), handoff_id, "clearview")

        other = operator_headers(subject=SECOND_OPERATOR)
        refused = release(client, other, handoff_id, "clearview")
        assert refused.status_code == 409
        # An ownership refusal is its own code, not a queue race: the status
        # admits the transition, so the client must not advise "reload the
        # queue" — the caller simply does not own the conversation.
        assert refused.json()["code"] == "handoff_ownership_refused"
        assert refused.json()["ownershipRefused"] is True

        supervisor = operator_headers(subject=SUPERVISOR, role="tenant_admin")
        resolved = resolve(client, supervisor, handoff_id, "clearview")
        assert resolved.status_code == 201

    def test_a_non_owner_cannot_resolve_without_the_administrative_flag(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        """Resolution of an assigned handoff is an ownership action too.

        The status admits resolve (an assigned handoff is resolvable), so the
        refusal must be the ownership code, not a contradictory "permitted from
        assigned" — the queue did not move, this principal holds no ownership.
        """
        client, handoffs, _audit, _model = staff_console
        session_id, _credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        accept(client, operator_headers(), handoff_id, "clearview")

        other = operator_headers(subject=SECOND_OPERATOR)
        refused = resolve(client, other, handoff_id, "clearview")
        assert refused.status_code == 409
        body = refused.json()
        assert body["code"] == "handoff_ownership_refused"
        assert body["ownershipRefused"] is True
        assert body["currentState"] == "assigned"

    def test_releasing_someone_elses_assignment_is_the_disconnect_recovery(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, _credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        accept(client, operator_headers(), handoff_id, "clearview")

        supervisor = operator_headers(subject=SUPERVISOR, role="tenant_admin")
        released = release(client, supervisor, handoff_id, "clearview")
        assert released.status_code == 201
        assert released.json()["handoff"]["status"] == "queued"
        assert released.json()["handoff"]["assigned_principal_id"] is None

        # The conversation is takeable again.
        accepted = accept(
            client, operator_headers(subject=SECOND_OPERATOR), handoff_id, "clearview"
        )
        assert accepted.status_code == 201
        assert accepted.json()["handoff"]["assigned_principal_id"] == SECOND_OPERATOR

    def test_a_refused_accept_carries_the_current_state(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, _credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        accept(client, operator_headers(), handoff_id, "clearview")

        second = accept(client, operator_headers(subject=SECOND_OPERATOR), handoff_id, "clearview")
        assert second.status_code == 409
        body = second.json()
        assert body["code"] == "invalid_handoff_transition"
        assert body["currentState"] == "assigned"

    def test_a_viewer_cannot_work_the_queue(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, _credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)

        viewer = operator_headers(subject=SECOND_OPERATOR, role="viewer")
        assert (
            client.get(
                "/api/admin/handoffs", params={"tenant_id": "clearview"}, headers=viewer
            ).status_code
            == 403
        )
        assert accept(client, viewer, handoff_id, "clearview").status_code == 403


class TestStaffReplyOwnership:
    def test_a_conversation_another_staff_member_holds_rejects_other_replies(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, _credential = open_visitor_session(client)
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        accept(client, operator_headers(), handoff_id, "clearview")

        other = operator_headers(subject=SECOND_OPERATOR)
        refused = client.post(
            f"/api/admin/chats/{session_id}/messages",
            json={"tenant_id": "clearview", "content": "I'll take it."},
            headers=other | {CSRF_HEADER: csrf_for(client, other)},
        )
        assert refused.status_code == 403

        owner = operator_headers()
        delivered = client.post(
            f"/api/admin/chats/{session_id}/messages",
            json={"tenant_id": "clearview", "content": "On my way."},
            headers=owner | {CSRF_HEADER: csrf_for(client, owner)},
        )
        assert delivered.status_code == 201

    def test_an_unowned_handoff_is_replyable_by_any_staff_member(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, _credential = open_visitor_session(client)
        record_handoff(handoffs, "clearview", session_id)

        other = operator_headers(subject=SECOND_OPERATOR)
        delivered = client.post(
            f"/api/admin/chats/{session_id}/messages",
            json={"tenant_id": "clearview", "content": "Checking on this."},
            headers=other | {CSRF_HEADER: csrf_for(client, other)},
        )
        assert delivered.status_code == 201


class TestCrossTenantIsolation:
    def test_a_queue_from_one_tenant_is_invisible_to_another(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        session_id, _credential = open_visitor_session(client, tenant_id="clearview")
        handoff_id = record_handoff(handoffs, "clearview", session_id)
        headers = operator_headers()

        # The operator is a member of both seeded tenants, so the empty apex
        # queue is the surface's own tenant scoping rather than a membership
        # error: no apex queue ever sees a clearview row.
        apex_rows = client.get(
            "/api/admin/handoffs", params={"tenant_id": "apex"}, headers=headers
        ).json()["handoffs"]
        assert apex_rows == []
        clearview_rows = client.get(
            "/api/admin/handoffs", params={"tenant_id": "clearview"}, headers=headers
        ).json()["handoffs"]
        assert [row["handoff_id"] for row in clearview_rows] == [handoff_id]

    def test_a_visitor_never_learns_another_tenants_queue_exists(
        self,
        staff_console: tuple[TestClient, InMemoryHandoffStore, InMemoryAuditStore, ScriptedModel],
    ) -> None:
        client, handoffs, _audit, _model = staff_console
        clearview_session, _credential = open_visitor_session(client, tenant_id="clearview")
        record_handoff(handoffs, "clearview", clearview_session)

        # A visitor in apex has no handoff of their own, and the visitor
        # surfaces expose nothing about other tenants' queues.
        _apex_session, apex_credential = open_visitor_session(client, tenant_id="apex")
        reply = visitor_message(client, apex_credential, "hello")
        assert reply.status_code == 200
        assert reply.json()["reply"] == "We are open until 7pm."
