"""The operator console contract, and the boundary around it.

Half of these are about what the console can do and half about what an
unauthenticated or under-privileged caller cannot, because both halves are the
feature: an admin API that works is worth nothing if it also answers strangers.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

from services.api.tests.conftest import BOOKING_TENANT, TEST_GATEWAY_TOKEN, VisitorSession
from tenantchat.api.identity import (
    CSRF_HEADER,
    GATEWAY_TOKEN_HEADER,
    ROLE_HEADER,
    SUBJECT_HEADER,
)

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
    """A role this service does not define grants nothing, rather than defaulting."""
    response = client.get(
        "/api/admin/chats",
        params={"tenant_id": BOOKING_TENANT},
        headers=operator_headers(**{ROLE_HEADER: "superuser"}),
    )

    assert response.status_code == 401


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
