"""The visitor conversation contract.

These tests read as the guarantee the widget is written against: a conversation
is opened by the server, a turn is answered and written down, a booking is not
committed until the customer says yes, and a conversation cannot be reached from
another tenant.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi.testclient import TestClient

from services.api.tests.conftest import (
    BOOKING_TENANT,
    OFFERED_SLOT,
    ScriptedModel,
    booking_call,
)
from tenantchat.orchestration.model import ModelResponse


def test_a_session_is_opened_by_the_server(client: TestClient) -> None:
    response = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})

    assert response.status_code == 201
    session = response.json()["session"]
    assert uuid.UUID(session["session_id"])
    assert session["tenant_id"] == BOOKING_TENANT
    assert response.json()["messages"] == []


def test_an_unknown_tenant_opens_no_session(client: TestClient) -> None:
    response = client.post("/api/chat/session", json={"tenant_id": "no-such-tenant"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_a_turn_is_answered_and_recorded(
    client: TestClient, open_session: Callable[..., str]
) -> None:
    session_id = open_session()

    response = client.post(
        "/api/chat",
        json={
            "tenant_id": BOOKING_TENANT,
            "session_id": session_id,
            "message": "What time do you close?",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "We are open until 7pm."
    assert body["pending"] is None
    assert body["provenance"] == {
        "model_name": "scripted",
        "graph_version": "dispatch@1",
        "prompt_version": "dispatch-system@1",
    }

    transcript = client.get(
        f"/api/chat/session/{session_id}", params={"tenant_id": BOOKING_TENANT}
    ).json()["messages"]
    assert [(entry["role"], entry["content"]) for entry in transcript] == [
        ("visitor", "What time do you close?"),
        ("assistant", "We are open until 7pm."),
    ]


def test_the_visitor_message_survives_a_model_that_never_answers(
    client: TestClient, model: ScriptedModel, open_session: Callable[..., str]
) -> None:
    """A failed turn must lose the reply, never the question.

    The customer's words are the part nobody can reproduce. If the store were
    written only after a successful turn, a provider outage would leave a
    conversation that reads as though the customer never typed.
    """
    model.script = [ModelResponse(content="", model_name="scripted")]
    session_id = open_session()

    client.post(
        "/api/chat",
        json={"tenant_id": BOOKING_TENANT, "session_id": session_id, "message": "Hello?"},
    )

    transcript = client.get(
        f"/api/chat/session/{session_id}", params={"tenant_id": BOOKING_TENANT}
    ).json()["messages"]
    assert transcript[0]["role"] == "visitor"
    assert transcript[0]["content"] == "Hello?"


def test_a_proposed_booking_pauses_before_it_commits(
    client: TestClient, model: ScriptedModel, open_session: Callable[..., str]
) -> None:
    model.script = [
        ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
        ModelResponse(content="You are booked.", model_name="scripted"),
    ]
    session_id = open_session()

    response = client.post(
        "/api/chat",
        json={"tenant_id": BOOKING_TENANT, "session_id": session_id, "message": "Book HVAC"},
    )

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
    client: TestClient, model: ScriptedModel, open_session: Callable[..., str]
) -> None:
    """A visitor who closed the tab mid-confirmation gets the question back."""
    model.script = [
        ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
        ModelResponse(content="You are booked.", model_name="scripted"),
    ]
    session_id = open_session()
    client.post(
        "/api/chat",
        json={"tenant_id": BOOKING_TENANT, "session_id": session_id, "message": "Book HVAC"},
    )

    response = client.get(f"/api/chat/session/{session_id}", params={"tenant_id": BOOKING_TENANT})

    assert response.status_code == 200
    assert response.json()["pending"]["slot"] == OFFERED_SLOT


def test_an_approved_booking_commits_once_and_answers(
    client: TestClient, model: ScriptedModel, open_session: Callable[..., str]
) -> None:
    model.script = [
        ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
        ModelResponse(content="You are booked for Monday at 2pm.", model_name="scripted"),
    ]
    session_id = open_session()
    client.post(
        "/api/chat",
        json={"tenant_id": BOOKING_TENANT, "session_id": session_id, "message": "Book HVAC"},
    )

    response = client.post(
        "/api/chat/confirmation",
        json={
            "tenant_id": BOOKING_TENANT,
            "session_id": session_id,
            "decision": "approved",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "You are booked for Monday at 2pm."
    assert body["pending"] is None
    assert [action["action"] for action in body["committed"]] == ["book_appointment"]
    assert body["committed"][0]["replayed"] is False


def test_a_declined_booking_commits_nothing(
    client: TestClient, model: ScriptedModel, open_session: Callable[..., str]
) -> None:
    model.script = [
        ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
        ModelResponse(content="No problem, nothing is booked.", model_name="scripted"),
    ]
    session_id = open_session()
    client.post(
        "/api/chat",
        json={"tenant_id": BOOKING_TENANT, "session_id": session_id, "message": "Book HVAC"},
    )

    response = client.post(
        "/api/chat/confirmation",
        json={"tenant_id": BOOKING_TENANT, "session_id": session_id, "decision": "declined"},
    )

    assert response.status_code == 200
    assert response.json()["committed"] == []


def test_confirming_when_nothing_is_pending_is_refused(
    client: TestClient, open_session: Callable[..., str]
) -> None:
    """Resuming a finished turn would append a second answer to one question."""
    session_id = open_session()
    client.post(
        "/api/chat",
        json={"tenant_id": BOOKING_TENANT, "session_id": session_id, "message": "Hours?"},
    )

    response = client.post(
        "/api/chat/confirmation",
        json={"tenant_id": BOOKING_TENANT, "session_id": session_id, "decision": "approved"},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "conflict"


def test_a_conversation_is_unreachable_from_another_tenant(
    client: TestClient, open_session: Callable[..., str]
) -> None:
    session_id = open_session(BOOKING_TENANT)

    posted = client.post(
        "/api/chat",
        json={"tenant_id": "apex", "session_id": session_id, "message": "Hello"},
    )
    read = client.get(f"/api/chat/session/{session_id}", params={"tenant_id": "apex"})

    assert posted.status_code == 404
    assert read.status_code == 404


def test_an_unknown_session_is_not_created_by_talking_to_it(client: TestClient) -> None:
    response = client.post(
        "/api/chat",
        json={"tenant_id": BOOKING_TENANT, "session_id": str(uuid.uuid4()), "message": "Hello"},
    )

    assert response.status_code == 404


def test_an_empty_message_is_rejected(client: TestClient, open_session: Callable[..., str]) -> None:
    response = client.post(
        "/api/chat",
        json={"tenant_id": BOOKING_TENANT, "session_id": open_session(), "message": ""},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "malformed_request"


def test_a_deployment_without_a_model_says_so(
    modelless_client: TestClient,
) -> None:
    """Until `AI-001` lands, chat is unavailable rather than broken."""
    opened = modelless_client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
    session_id = opened.json()["session"]["session_id"]

    response = modelless_client.post(
        "/api/chat",
        json={"tenant_id": BOOKING_TENANT, "session_id": session_id, "message": "Hello"},
    )

    assert response.status_code == 503
    assert response.json()["code"] == "chat_unavailable"


def test_a_transcript_is_readable_without_a_model(modelless_client: TestClient) -> None:
    """Reading what was already said must not depend on being able to answer."""
    opened = modelless_client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
    session_id = opened.json()["session"]["session_id"]

    response = modelless_client.get(
        f"/api/chat/session/{session_id}", params={"tenant_id": BOOKING_TENANT}
    )

    assert response.status_code == 200
    assert response.json()["pending"] is None
