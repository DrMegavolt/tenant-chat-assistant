"""The visitor conversation contract.

These tests read as the guarantee the widget is written against: a conversation
is opened by the server with a signed credential, a turn is answered and
written down, a booking is not committed until the customer says yes, and the
credential — the only identity these routes accept — cannot move a conversation
between tenants (SEC-002).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

from services.api.tests.conftest import (
    BOOKING_TENANT,
    OFFERED_SLOT,
    ScriptedModel,
    VisitorSession,
    booking_call,
)
from tenantchat.orchestration.model import ModelResponse

VISITOR_TURN = {"message": "What time do you close?"}


def test_a_session_is_opened_by_the_server_with_a_credential(
    client: TestClient,
) -> None:
    response = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})

    assert response.status_code == 201
    session = response.json()["session"]
    assert session["tenant_id"] == BOOKING_TENANT
    assert response.json()["messages"] == []
    assert response.json()["credential"].startswith("tc.v1.")


def test_an_unknown_tenant_opens_no_session(client: TestClient) -> None:
    response = client.post("/api/chat/session", json={"tenant_id": "no-such-tenant"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_a_turn_is_answered_and_recorded(
    client: TestClient, visitor_session: Callable[..., VisitorSession]
) -> None:
    visitor = visitor_session()

    response = client.post("/api/chat", json=VISITOR_TURN, headers=visitor.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "We are open until 7pm."
    assert body["pending"] is None
    assert body["provenance"] == {
        "model_name": "scripted",
        "graph_version": "dispatch@2",
        "prompt_version": "dispatch-system@4",
    }

    transcript = client.get("/api/chat/session", headers=visitor.headers).json()["messages"]
    assert [(entry["role"], entry["content"]) for entry in transcript] == [
        ("visitor", "What time do you close?"),
        ("assistant", "We are open until 7pm."),
    ]


def test_the_visitor_message_survives_a_model_that_never_answers(
    client: TestClient,
    model: ScriptedModel,
    visitor_session: Callable[..., VisitorSession],
) -> None:
    """A failed turn must lose the reply, never the question.

    The customer's words are the part nobody can reproduce. If the store were
    written only after a successful turn, a provider outage would leave a
    conversation that reads as though the customer never typed.
    """
    model.script = [ModelResponse(content="", model_name="scripted")]
    visitor = visitor_session()

    client.post("/api/chat", json={"message": "Hello?"}, headers=visitor.headers)

    transcript = client.get("/api/chat/session", headers=visitor.headers).json()["messages"]
    assert transcript[0]["role"] == "visitor"
    assert transcript[0]["content"] == "Hello?"


def test_a_proposed_booking_pauses_before_it_commits(
    client: TestClient,
    model: ScriptedModel,
    visitor_session: Callable[..., VisitorSession],
) -> None:
    model.script = [
        ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
        ModelResponse(content="You are booked.", model_name="scripted"),
    ]
    visitor = visitor_session()

    response = client.post("/api/chat", json={"message": "Book HVAC"}, headers=visitor.headers)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == ""
    assert body["committed"] == []
    assert body["pending"] == {
        "awaiting": "booking_confirmation",
        "service": "HVAC",
        "slot": OFFERED_SLOT,
        "customer_name": "Dana Ruiz",
        "address": "12 Alder Court, Portland, OR 97205",
    }


def test_a_paused_conversation_reports_what_it_is_waiting_on(
    client: TestClient,
    model: ScriptedModel,
    visitor_session: Callable[..., VisitorSession],
) -> None:
    """A visitor who closed the tab mid-confirmation gets the question back."""
    model.script = [
        ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
        ModelResponse(content="You are booked.", model_name="scripted"),
    ]
    visitor = visitor_session()
    client.post("/api/chat", json={"message": "Book HVAC"}, headers=visitor.headers)

    response = client.get("/api/chat/session", headers=visitor.headers)

    assert response.status_code == 200
    assert response.json()["pending"]["slot"] == OFFERED_SLOT


def test_an_approved_booking_commits_once_and_answers(
    client: TestClient,
    model: ScriptedModel,
    visitor_session: Callable[..., VisitorSession],
) -> None:
    model.script = [
        ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
        ModelResponse(content="You are booked for Monday at 2pm.", model_name="scripted"),
    ]
    visitor = visitor_session()
    client.post("/api/chat", json={"message": "Book HVAC"}, headers=visitor.headers)

    response = client.post(
        "/api/chat/confirmation", json={"decision": "approved"}, headers=visitor.headers
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "You are booked for Monday at 2pm."
    assert body["pending"] is None
    assert [action["action"] for action in body["committed"]] == ["book_appointment"]
    assert body["committed"][0]["replayed"] is False


def test_a_declined_booking_commits_nothing(
    client: TestClient,
    model: ScriptedModel,
    visitor_session: Callable[..., VisitorSession],
) -> None:
    model.script = [
        ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
        ModelResponse(content="No problem, nothing is booked.", model_name="scripted"),
    ]
    visitor = visitor_session()
    client.post("/api/chat", json={"message": "Book HVAC"}, headers=visitor.headers)

    response = client.post(
        "/api/chat/confirmation", json={"decision": "declined"}, headers=visitor.headers
    )

    assert response.status_code == 200
    assert response.json()["committed"] == []


def test_confirming_when_nothing_is_pending_is_refused(
    client: TestClient, visitor_session: Callable[..., VisitorSession]
) -> None:
    """Resuming a finished turn would append a second answer to one question."""
    visitor = visitor_session()
    client.post("/api/chat", json={"message": "Hours?"}, headers=visitor.headers)

    response = client.post(
        "/api/chat/confirmation", json={"decision": "approved"}, headers=visitor.headers
    )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_a_turn_cannot_name_a_tenant_or_session_in_the_body(
    client: TestClient, visitor_session: Callable[..., VisitorSession]
) -> None:
    """`extra="forbid"`: the body cannot even carry the tenant the old API took.

    This is the reassignment attack's last foothold. Identity comes from the
    credential header, so a body that tries to name another tenant is malformed
    rather than ignored.
    """
    visitor = visitor_session()

    response = client.post(
        "/api/chat",
        json={"tenant_id": "apex", "session_id": visitor.session_id, "message": "Hello"},
        headers=visitor.headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "malformed_request"


def test_a_turn_cannot_name_a_model(
    client: TestClient, visitor_session: Callable[..., VisitorSession]
) -> None:
    """Model selection is deployment configuration; a body field cannot pick one.

    The provider is chosen by environment and tenant policy (`AI-001`), never by
    the caller: a visitor who could name a model could select an expensive or
    differently-behaved one.
    """
    visitor = visitor_session()

    response = client.post(
        "/api/chat",
        json={"message": "Hello", "model": "expensive-frontier-model"},
        headers=visitor.headers,
    )

    assert response.status_code == 422
    assert response.json()["code"] == "malformed_request"


def test_a_credential_cannot_reach_another_tenants_conversation(
    client: TestClient, visitor_session: Callable[..., VisitorSession]
) -> None:
    """Each credential names exactly one tenant; nothing in the request can
    change that, so one tenant's visitor cannot read or write another's."""
    apex = visitor_session("apex")
    clearview = visitor_session(BOOKING_TENANT)
    apex_turn = client.post("/api/chat", json={"message": "Apex side"}, headers=apex.headers)
    clearview_turn = client.post(
        "/api/chat", json={"message": "Clearview side"}, headers=clearview.headers
    )

    assert apex_turn.status_code == 200
    assert clearview_turn.status_code == 200

    apex_transcript = client.get("/api/chat/session", headers=apex.headers).json()["messages"]
    clearview_transcript = client.get("/api/chat/session", headers=clearview.headers).json()[
        "messages"
    ]
    assert [m["content"] for m in apex_transcript] == ["Apex side", "We are open until 7pm."]
    assert [m["content"] for m in clearview_transcript] == [
        "Clearview side",
        "We are open until 7pm.",
    ]


def test_a_credential_for_a_session_that_never_opened_creates_nothing(
    client: TestClient, mint_credential: Callable[..., str]
) -> None:
    """A signed token cannot conjure a conversation: the store row decides.

    The unguessable-session property survives inside the token, so replaying a
    token for a session that does not exist is a 404, not a creation.
    """
    headers = {
        "X-Visitor-Credential": mint_credential(
            BOOKING_TENANT, "00000000-0000-0000-0000-000000000000"
        )
    }

    response = client.post("/api/chat", json={"message": "Hello"}, headers=headers)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_an_empty_message_is_rejected(
    client: TestClient, visitor_session: Callable[..., VisitorSession]
) -> None:
    response = client.post("/api/chat", json={"message": ""}, headers=visitor_session().headers)

    assert response.status_code == 422
    assert response.json()["code"] == "malformed_request"


def test_a_deployment_without_a_model_says_so(
    modelless_client: TestClient,
) -> None:
    """Until `AI-001` lands, chat is unavailable rather than broken."""
    opened = modelless_client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
    headers = {"X-Visitor-Credential": opened.json()["credential"]}

    response = modelless_client.post("/api/chat", json={"message": "Hello"}, headers=headers)

    assert response.status_code == 503
    assert response.json()["code"] == "chat_unavailable"


def test_a_transcript_is_readable_without_a_model(modelless_client: TestClient) -> None:
    """Reading what was already said must not depend on being able to answer."""
    opened = modelless_client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
    headers = {"X-Visitor-Credential": opened.json()["credential"]}

    response = modelless_client.get("/api/chat/session", headers=headers)

    assert response.status_code == 200
    assert response.json()["pending"] is None
