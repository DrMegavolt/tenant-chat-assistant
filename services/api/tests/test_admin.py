"""The operator console contract, and the boundary around it.

Half of these are about what the console can do and half about what an
unauthenticated or under-privileged caller cannot, because both halves are the
feature: an admin API that works is worth nothing if it also answers strangers.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.tests.conftest import (
    BOOKING_TENANT,
    LEAD_TENANT,
    TEST_GATEWAY_TOKEN,
    ScriptedModel,
    VisitorSession,
    booking_call,
    lead_call,
)
from tenantchat.api.identity import (
    CSRF_HEADER,
    GATEWAY_TOKEN_HEADER,
    ROLE_HEADER,
    SUBJECT_HEADER,
)
from tenantchat.api.store import (
    InMemoryLeadStore,
    InMemoryWorkflowStore,
)
from tenantchat.core.commands import LeadCommand, LeadUrgency
from tenantchat.core.contact import Contact
from tenantchat.core.ports import IdempotencyKey
from tenantchat.core.routing import IntentName
from tenantchat.core.workflows import ToolResult
from tenantchat.orchestration.model import ModelResponse, ToolCall

ADMIN_ROUTES: tuple[tuple[str, str, dict[str, str]], ...] = (
    ("GET", "/api/admin/chats", {"tenant_id": BOOKING_TENANT}),
    ("GET", "/api/admin/csrf-token", {}),
)


def csrf_for(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    token: str = response.json()["csrf_token"]
    return token


@pytest.mark.security
@pytest.mark.parametrize(("method", "path", "params"), ADMIN_ROUTES)
def test_an_unauthenticated_caller_reaches_no_admin_route(
    client: TestClient, method: str, path: str, params: dict[str, str]
) -> None:
    response = client.request(method, path, params=params)

    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


@pytest.mark.security
def test_identity_headers_without_the_gateway_token_authenticate_nothing(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> None:
    """The identity headers are forgeable by anything that can reach the port.

    Only the shared token establishes that they were written by the gateway, so
    a caller that supplies a complete, plausible identity and no token must be
    indistinguishable from one that supplied nothing.
    """
    headers = operator_headers()
    del headers[GATEWAY_TOKEN_HEADER]

    response = client.get("/api/admin/chats", params={"tenant_id": BOOKING_TENANT}, headers=headers)

    assert response.status_code == 401


@pytest.mark.security
def test_a_wrong_gateway_token_is_refused(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> None:
    response = client.get(
        "/api/admin/chats",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(**{GATEWAY_TOKEN_HEADER: f"{TEST_GATEWAY_TOKEN}x"}),
    )

    assert response.status_code == 401


@pytest.mark.security
def test_an_undefined_role_is_refused(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> None:
    """An authenticated identity with an unknown role is forbidden, not logged out."""
    response = client.get(
        "/api/admin/chats",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(**{ROLE_HEADER: "superuser"}),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


@pytest.mark.security
def test_an_authenticated_operator_without_a_role_is_forbidden(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> None:
    """A roleless OIDC user must not trigger the console's expired-session loop."""
    response = client.get(
        "/api/admin/tenants",
        headers=operator_headers(**{ROLE_HEADER: ""}),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


@pytest.mark.security
def test_a_viewer_may_not_send_a_staff_reply(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    open_session: Callable[..., str],
) -> None:
    session_id = open_session()
    headers = operator_headers(role="viewer")

    response = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "On my way."},
        headers=headers | {CSRF_HEADER: csrf_for(client, headers)},
    )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


@pytest.mark.security
def test_a_staff_reply_without_a_csrf_token_is_refused(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    open_session: Callable[..., str],
) -> None:
    session_id = open_session()

    response = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "On my way."},
        headers=operator_headers(),
    )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_validation_failed"


@pytest.mark.security
def test_a_csrf_token_minted_for_another_operator_is_refused(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    open_session: Callable[..., str],
) -> None:
    """The token is bound to a subject, so a leaked one is not a spare key."""
    session_id = open_session()
    other = operator_headers(**{SUBJECT_HEADER: "operator-99"})

    response = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "On my way."},
        headers=operator_headers() | {CSRF_HEADER: csrf_for(client, other)},
    )

    assert response.status_code == 403


@pytest.mark.security
def test_admin_responses_carry_no_cross_origin_grant(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> None:
    """An allowlisted widget origin must not become a way to read transcripts.

    The origin list exists for the embedded widget. Admin requests arrive
    through the gateway already carrying identity, so a browser that could read
    the response would be reading someone else's conversation.
    """
    response = client.get(
        "/api/admin/chats",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers() | {"Origin": "https://widget.example"},
    )

    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_the_widget_surface_still_answers_cross_origin(client: TestClient) -> None:
    """The guard above must not have turned CORS off for everything."""
    response = client.get("/api/tenants", headers={"Origin": "https://widget.example"})

    assert response.headers["access-control-allow-origin"] == "https://widget.example"


def test_conversations_are_listed_most_recently_active_first(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    visitor_session: Callable[..., VisitorSession],
) -> None:
    older, newer = visitor_session(), visitor_session()
    for visitor in (older, newer):
        client.post("/api/chat", json={"message": "Hello"}, headers=visitor.headers)

    response = client.get(
        "/api/admin/chats",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(role="viewer"),
    )

    assert response.status_code == 200
    listed = [session["session_id"] for session in response.json()["sessions"]]
    assert listed == [newer.session_id, older.session_id]


def test_a_conversation_nobody_spoke_in_is_not_listed(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    open_session: Callable[..., str],
) -> None:
    """An empty row is not work: it is an abandoned tab or a correlation record."""
    open_session()

    response = client.get(
        "/api/admin/chats",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(),
    )

    assert response.json()["sessions"] == []


def test_the_list_never_carries_transcripts(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    visitor_session: Callable[..., VisitorSession],
) -> None:
    visitor = visitor_session()
    client.post("/api/chat", json={"message": "My name is Dana"}, headers=visitor.headers)

    body = client.get(
        "/api/admin/chats",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(),
    ).text

    assert "My name is Dana" not in body


def test_one_conversation_reads_in_full(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    visitor_session: Callable[..., VisitorSession],
) -> None:
    visitor = visitor_session()
    client.post("/api/chat", json={"message": "Hours?"}, headers=visitor.headers)

    response = client.get(
        f"/api/admin/chats/{visitor.session_id}",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(),
    )

    assert response.status_code == 200
    assert [entry["role"] for entry in response.json()["messages"]] == ["visitor", "assistant"]


def test_a_staff_reply_is_stored_as_a_person_speaking(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    visitor_session: Callable[..., VisitorSession],
) -> None:
    """Distinct from `assistant`: a customer reading a promise should know who made it."""
    visitor = visitor_session()
    headers = operator_headers()

    response = client.post(
        f"/api/admin/chats/{visitor.session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "I can be there at four."},
        headers=headers | {CSRF_HEADER: csrf_for(client, headers)},
    )

    assert response.status_code == 201
    assert response.json()["message"]["role"] == "staff"

    visitor_view = client.get("/api/chat/session", headers=visitor.headers).json()["messages"]
    assert visitor_view[-1]["content"] == "I can be there at four."


def test_a_reply_cannot_be_delivered_to_another_tenants_conversation(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    open_session: Callable[..., str],
) -> None:
    session_id = open_session(BOOKING_TENANT)
    headers = operator_headers()

    response = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": "apex", "content": "On my way."},
        headers=headers | {CSRF_HEADER: csrf_for(client, headers)},
    )

    assert response.status_code == 404


def test_an_unknown_conversation_is_not_found(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> None:
    response = client.get(
        f"/api/admin/chats/{uuid.uuid4()}",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(),
    )

    assert response.status_code == 404


def test_the_queue_row_carries_counts_and_a_last_message_preview(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    visitor_session: Callable[..., VisitorSession],
) -> None:
    """L-A09/L-A10 and the frontend contract: a queue row advertises how much
    work it holds and how recently it moved, without carrying the transcript."""
    visitor = visitor_session()
    client.post("/api/chat", json={"message": "My AC is dripping."}, headers=visitor.headers)

    body = client.get(
        "/api/admin/chats",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(),
    ).json()["sessions"][0]

    assert body["message_count"] == 2
    assert body["lead_count"] == 0
    assert body["last_message"]["role"] == "assistant"
    assert body["last_message"]["content"] == "We are open until 7pm."
    assert body["last_message"]["created_at"]


def test_the_list_carries_only_a_bounded_last_message_preview(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    open_session: Callable[..., str],
) -> None:
    """The preview is a recency hint bounded to 200 characters, never a
    transcript: content beyond the bound is reachable only through the audited
    detail read."""
    session_id = open_session()
    headers = operator_headers()
    long_reply = "The earliest slot is Tuesday." + " Details follow in the transcript. " * 20
    client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": long_reply},
        headers=headers | {CSRF_HEADER: csrf_for(client, headers)},
    )

    listed = client.get(
        "/api/admin/chats",
        params={"tenant_id": BOOKING_TENANT},
        headers=headers,
    ).json()["sessions"][0]

    assert len(listed["last_message"]["content"]) == 200
    assert listed["message_count"] == 1

    detail = client.get(
        f"/api/admin/chats/{session_id}",
        params={"tenant_id": BOOKING_TENANT},
        headers=headers,
    ).json()
    assert detail["messages"][-1]["content"] == long_reply


def test_the_session_detail_names_what_the_conversation_produced(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    model: ScriptedModel,
) -> None:
    """The frontend session-detail contract (L-A01): one response carries the
    transcript plus the leads, bookings, and tool events the side cards render.
    The wire names are snake_case; the console's adapter maps them to its
    camelCase `leads`/`bookings`/`toolEvents` fields."""
    model.script = [
        ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
        ModelResponse(content="You are booked.", model_name="scripted"),
    ]
    opened = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
    assert opened.status_code == 201
    visitor = VisitorSession(
        BOOKING_TENANT,
        opened.json()["session"]["session_id"],
        opened.json()["credential"],
    )
    granted = client.post(
        "/api/chat/consent",
        json={"purposes": ["booking", "follow_up"]},
        headers=visitor.headers,
    )
    assert granted.status_code == 200
    client.post("/api/chat", json={"message": "Book HVAC"}, headers=visitor.headers)
    client.post("/api/chat/confirmation", json={"decision": "approved"}, headers=visitor.headers)

    body = client.get(
        f"/api/admin/chats/{visitor.session_id}",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(),
    ).json()

    assert body["session"]["message_count"] == 2
    assert body["session"]["lead_count"] == 0
    assert body["leads"] == []
    [booking] = body["bookings"]
    assert booking["customer_name"] == "Dana Ruiz"
    assert booking["contact"] == "(555) 222-1919"
    assert booking["service"] == "HVAC"
    assert booking["slot"]
    assert booking["address"] == "12 Alder Court, Portland, OR 97205"
    assert body["tool_events"] == []


def test_the_session_detail_names_the_leads_a_conversation_captured(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    model: ScriptedModel,
) -> None:
    model.script = [
        ModelResponse(content="", tool_calls=(lead_call(),), model_name="scripted"),
        ModelResponse(content="The team will call you back.", model_name="scripted"),
    ]
    opened = client.post("/api/chat/session", json={"tenant_id": LEAD_TENANT})
    assert opened.status_code == 201
    visitor = VisitorSession(
        LEAD_TENANT,
        opened.json()["session"]["session_id"],
        opened.json()["credential"],
    )
    granted = client.post(
        "/api/chat/consent",
        json={"purposes": ["booking", "follow_up"]},
        headers=visitor.headers,
    )
    assert granted.status_code == 200
    client.post("/api/chat", json={"message": "Please call me"}, headers=visitor.headers)
    client.post("/api/chat/confirmation", json={"decision": "approved"}, headers=visitor.headers)

    body = client.get(
        f"/api/admin/chats/{visitor.session_id}",
        params={"tenant_id": LEAD_TENANT},
        headers=operator_headers(),
    ).json()

    assert body["session"]["lead_count"] == 1
    assert body["bookings"] == []
    [lead] = body["leads"]
    assert lead["customer_name"] == "Dana Ruiz"
    assert lead["contact"] == "dana@example.com"
    assert lead["service"] == "HVAC"
    assert lead["urgency"]
    assert lead["summary"] == "Furnace is making a grinding noise."


def test_the_session_detail_carries_the_service_text_the_visitor_parsed(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    model: ScriptedModel,
) -> None:
    """N-07: the lead keeps the service string the visitor's request parsed to,
    even when it is not an exact catalog member and no urgency was given —
    the operator's callback card is where that free text is consumed. The
    confirmation card showed "HVAC repair - AC not cooling"; the detail
    payload must carry the same string, not an empty or resolved-away value.
    """
    model.script = [
        ModelResponse(
            content="",
            tool_calls=(
                ToolCall(
                    call_id="call-lead-free-text",
                    name="create_lead",
                    arguments={
                        "customer_name": "Jane Tester",
                        "customer_phone_or_email": "jane@example.com",
                        "service": "HVAC repair - AC not cooling",
                        "summary": "The AC is not cooling.",
                    },
                ),
            ),
            model_name="scripted",
        ),
        ModelResponse(content="The team will call you back.", model_name="scripted"),
    ]
    opened = client.post("/api/chat/session", json={"tenant_id": LEAD_TENANT})
    assert opened.status_code == 201
    visitor = VisitorSession(
        LEAD_TENANT,
        opened.json()["session"]["session_id"],
        opened.json()["credential"],
    )
    granted = client.post(
        "/api/chat/consent",
        json={"purposes": ["booking", "follow_up"]},
        headers=visitor.headers,
    )
    assert granted.status_code == 200
    client.post("/api/chat", json={"message": "Please call me"}, headers=visitor.headers)
    client.post("/api/chat/confirmation", json={"decision": "approved"}, headers=visitor.headers)

    body = client.get(
        f"/api/admin/chats/{visitor.session_id}",
        params={"tenant_id": LEAD_TENANT},
        headers=operator_headers(),
    ).json()

    [lead] = body["leads"]
    assert lead["service"] == "HVAC repair - AC not cooling"
    assert lead["urgency"] == "unknown"


def test_a_staff_reply_is_audited_with_the_reply(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    open_session: Callable[..., str],
    audit_store: Any,
) -> None:
    """R-39: the row that vouches for a staff reply commits with it, so an
    audit read never shows a reply the audit cannot account for (or the
    reverse) — and it names the message it vouches for, so the accountability
    record and the persisted reply join on the id."""
    session_id = open_session()
    headers = operator_headers()

    response = client.post(
        f"/api/admin/chats/{session_id}/messages",
        json={"tenant_id": BOOKING_TENANT, "content": "On my way."},
        headers=headers | {CSRF_HEADER: csrf_for(client, headers)},
    )

    assert response.status_code == 201
    message_id = response.json()["message"]["message_id"]
    events = asyncio.run(audit_store.for_tenant(BOOKING_TENANT))
    replies = [event for event in events if event.action == "staff_reply_sent"]
    assert len(replies) == 1
    assert str(replies[0].resource_id) == session_id
    assert replies[0].details["message_id"] == message_id
    assert replies[0].principal_id == "operator-7"
    assert replies[0].request_id


def test_leads_and_bookings_are_bounded_with_their_total(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
) -> None:
    """R-56: the tenant lists are bounded pages, not unbounded dumps; `total`
    tells the console how far the page is from the end."""
    lead_store = cast(InMemoryLeadStore, cast(FastAPI, client.app).state.lead_store)
    for index in range(3):
        asyncio.run(
            lead_store.record(
                LeadCommand(
                    tenant_id=LEAD_TENANT,
                    customer_name=f"Lead {index}",
                    contact=Contact.parse("dana@example.com"),
                    service="HVAC",
                    service_slug="hvac",
                    summary="Furnace noise",
                    address_or_zip="97205",
                    urgency=LeadUrgency.parse("today"),
                ),
                session_id=f"lead-session-{index}",
            )
        )
    headers = operator_headers()

    leads = client.get(
        "/api/admin/leads", params={"tenant_id": LEAD_TENANT, "limit": 2}, headers=headers
    )
    assert leads.status_code == 200, leads.text
    lead_body = leads.json()
    assert len(lead_body["leads"]) == 2
    assert lead_body["limit"] == 2
    assert lead_body["total"] == 3

    bookings = client.get(
        "/api/admin/bookings", params={"tenant_id": BOOKING_TENANT}, headers=headers
    )
    assert bookings.status_code == 200, bookings.text
    booking_body = bookings.json()
    assert booking_body["bookings"] == []
    assert booking_body["limit"] == 100
    assert booking_body["total"] == 0


def test_the_session_detail_surfaces_the_durable_tool_results(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    visitor_session: Callable[..., VisitorSession],
) -> None:
    """The tool-events card reads the AGENT-001 workflow record's model-facing
    payloads (`OBS-004`): a JSON result parses back into its object shape so
    the console renders it verbatim, and a non-JSON payload passes through as
    the string it was. (The booking and lead flows do not yet feed this record
    — that write lives in the orchestration package — so this seeds the store
    the route reads, exactly as a deployment's record would look.)"""
    visitor = visitor_session()
    workflows = cast(InMemoryWorkflowStore, cast(FastAPI, client.app).state.workflow_store)
    started = asyncio.run(
        workflows.start(
            tenant_id=BOOKING_TENANT,
            session_id=visitor.session_id,
            intent=IntentName.BOOKING,
            agent_version="dispatch@3",
            next_allowed_actions=("book_appointment",),
            turn_index=1,
            idempotency_key=IdempotencyKey.parse("wf-start-00000001"),
        )
    )
    asyncio.run(
        workflows.update(
            tenant_id=BOOKING_TENANT,
            session_id=visitor.session_id,
            workflow_id=started.workflow_id,
            collected_fields={},
            tool_results=(
                ToolResult(
                    call_id="call-1",
                    name="check_service_area",
                    result=json.dumps({"served": True}),
                ),
                ToolResult(call_id="call-2", name="legacy_tool", result="raw text"),
            ),
            next_allowed_actions=("book_appointment",),
            turn_index=1,
            idempotency_key=IdempotencyKey.parse("wf-tools-00000001"),
        )
    )

    body = client.get(
        f"/api/admin/chats/{visitor.session_id}",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(),
    ).json()

    assert body["tool_events"] == [
        {"name": "check_service_area", "result": {"served": True}},
        {"name": "legacy_tool", "result": "raw text"},
    ]
