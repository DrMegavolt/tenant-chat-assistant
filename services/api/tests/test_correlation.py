"""OBS-001: every request is one addressable unit across the turn.

The middleware mints the request ID and trace ID (never accepting a client's),
echoes them on every response, and binds them in the correlation context so
anything the turn logs lines up under them. The tenant reaches the log plane
only as a keyed pseudonym, and only after a verified credential named it.
These tests drive the real HTTP surface and inspect the JSON lines a
deployment would ship.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.testclient import TestClient as StarletteTestClient

from tenantchat.api.app import create_app
from tenantchat.api.correlation import (
    TRACE_ID_HEADER,
    CorrelationContext,
    bind,
    correlation_headers,
    current,
    reset,
    tenant_pseudonym,
)
from tenantchat.api.logging_setup import build_json_handler
from tenantchat.api.problems import REQUEST_ID_HEADER
from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER
from tenantchat.orchestration.checkpoints import InMemorySaver

BOOKING_TENANT = "clearview"
TEST_PSEUDONYM_KEY = "test-pseudonym-key"

TEST_ACCESS_FIELDS = ("timestamp", "level", "service", "environment", "event")


def _stores() -> dict[str, Any]:
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

    bookings = InMemoryBookingStore()
    leads = InMemoryLeadStore()
    conversations = InMemoryConversationStore()
    handoffs = InMemoryHandoffStore()
    consent = InMemoryConsentStore()
    return {
        "booking_store": bookings,
        "lead_store": leads,
        "conversation_store": conversations,
        "handoff_store": handoffs,
        "idempotency_store": InMemoryIdempotencyStore(),
        "membership_store": InMemoryMembershipStore(),
        "audit_store": InMemoryAuditStore(),
        "consent_store": consent,
        "privacy_store": InMemoryPrivacyStore(conversations, bookings, leads, handoffs, consent),
    }


@pytest.fixture
def build_app(settings: Any, model: Any) -> Callable[..., FastAPI]:
    """Compose an app with fresh stores per call, like the abuse suite."""

    def build(**overrides: Any) -> FastAPI:
        resolved = dataclasses.replace(settings, **overrides)
        return create_app(resolved, chat_model=model, checkpointer=InMemorySaver(), **_stores())

    return build


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    """Attach a JSON handler on root and hand back its stream."""
    stream = io.StringIO()
    handler = build_json_handler(service="chat-api", environment="test", stream=stream)
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield stream
    finally:
        root.removeHandler(handler)


def _json_lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def _open_turn(client: StarletteTestClient, message: str) -> tuple[str, str, str]:
    """Open a conversation and run one turn; returns (request_id, trace_id, tenant)."""
    opened = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
    assert opened.status_code == 201, opened.text
    credential = opened.json()["credential"]
    headers = {VISITOR_CREDENTIAL_HEADER: credential}
    granted = client.post(
        "/api/chat/consent", json={"purposes": ["booking", "follow_up"]}, headers=headers
    )
    assert granted.status_code == 200, granted.text
    turn = client.post("/api/chat", json={"message": message}, headers=headers)
    assert turn.status_code == 200, turn.text
    return turn.headers[REQUEST_ID_HEADER], turn.headers[TRACE_ID_HEADER], credential


class TestServerIssuedIds:
    def test_every_response_carries_distinct_request_and_trace_ids(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        with TestClient(build_app(), raise_server_exceptions=False) as client:
            first = client.get("/api/tenants")
            second = client.get("/api/tenants")

        for response in (first, second):
            assert response.headers[REQUEST_ID_HEADER]
            assert response.headers[TRACE_ID_HEADER]
            assert response.headers[REQUEST_ID_HEADER] != response.headers[TRACE_ID_HEADER]
        assert first.headers[REQUEST_ID_HEADER] != second.headers[REQUEST_ID_HEADER]

    def test_client_supplied_ids_are_never_trusted(self, build_app: Callable[..., FastAPI]) -> None:
        """A forged or repeated ID is useless for correlation and audit."""
        with TestClient(build_app(), raise_server_exceptions=False) as client:
            response = client.get(
                "/api/tenants",
                headers={REQUEST_ID_HEADER: "forged-id", TRACE_ID_HEADER: "forged-trace"},
            )

        assert response.headers[REQUEST_ID_HEADER] != "forged-id"
        assert response.headers[TRACE_ID_HEADER] != "forged-trace"

    def test_a_refused_request_still_echoes_the_ids(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        """An early guard refusal is as addressable as a success."""
        with TestClient(build_app(), raise_server_exceptions=False) as client:
            oversized = client.post(
                "/api/chat/session",
                json={"tenant_id": BOOKING_TENANT, "padding": "x" * 4096},
            )

        assert oversized.status_code == 413
        assert oversized.headers[REQUEST_ID_HEADER]
        assert oversized.headers[TRACE_ID_HEADER]


class TestTenantPseudonym:
    def test_pseudonym_is_stable_bounded_and_never_the_tenant_id(self) -> None:
        first = tenant_pseudonym(BOOKING_TENANT, key=TEST_PSEUDONYM_KEY)
        second = tenant_pseudonym(BOOKING_TENANT, key=TEST_PSEUDONYM_KEY)

        assert first == second
        assert len(first) <= 20
        assert first.startswith("t-")
        assert first != BOOKING_TENANT

    def test_tenants_differ_and_keys_differ(self) -> None:
        assert tenant_pseudonym("clearview", key=TEST_PSEUDONYM_KEY) != tenant_pseudonym(
            "apex", key=TEST_PSEUDONYM_KEY
        )
        assert tenant_pseudonym(BOOKING_TENANT, key="key-a") != tenant_pseudonym(
            BOOKING_TENANT, key="key-b"
        )

    def test_context_binding_round_trips_through_headers(self) -> None:
        pseudonym = tenant_pseudonym(BOOKING_TENANT, key=TEST_PSEUDONYM_KEY)
        bind(
            CorrelationContext(
                request_id="r-1",
                trace_id="t-1",
                tenant_id=BOOKING_TENANT,
                tenant_pseudonym=pseudonym,
            )
        )
        try:
            assert correlation_headers() == {REQUEST_ID_HEADER: "r-1", TRACE_ID_HEADER: "t-1"}
            context = current()
            assert context is not None
            assert context.tenant_pseudonym == pseudonym
        finally:
            reset()
        assert current() is None
        assert correlation_headers() == {}


class TestTracedTurn:
    def test_one_chat_turn_lines_up_under_one_trace(
        self,
        build_app: Callable[..., FastAPI],
        captured_logs: io.StringIO,
    ) -> None:
        with TestClient(
            build_app(log_access=True, log_pseudonym_key=TEST_PSEUDONYM_KEY),
            raise_server_exceptions=False,
        ) as client:
            request_id, trace_id, _credential = _open_turn(
                client, "Is there a furnace discount this month?"
            )
            assert client.get("/api/tenants").status_code == 200

        lines = _json_lines(captured_logs)

        access = [line for line in lines if line.get("event") == "request completed"]
        turn_access = [
            line
            for line in access
            if line.get("method") == "POST" and line.get("path") == "/api/chat"
        ]
        assert len(turn_access) == 1
        assert turn_access[0]["status"] == 200
        assert turn_access[0]["tenant"] == tenant_pseudonym(BOOKING_TENANT, key=TEST_PSEUDONYM_KEY)
        turn_lines = [line for line in lines if line.get("event") == "chat turn completed"]
        assert len(turn_lines) == 1
        turn = turn_lines[0]

        # The whole turn — access line and turn event — shares one trace.
        assert turn["request_id"] == request_id
        assert turn["trace_id"] == trace_id
        assert turn_access[0]["trace_id"] == trace_id
        assert turn["tenant"] == tenant_pseudonym(BOOKING_TENANT, key=TEST_PSEUDONYM_KEY)
        assert turn["graph_version"] == "dispatch@1"
        assert turn["committed_actions"] == []

        # Every line carries the contract fields; records emitted outside a
        # request (e.g. the HTTP client's own lines) have no IDs to carry.
        for line in lines:
            for field in TEST_ACCESS_FIELDS:
                assert field in line, line
            assert line["service"] == "chat-api"
            assert line["environment"] == "test"
        for line in access + turn_lines:
            assert "request_id" in line
            assert "trace_id" in line

    def test_the_visitor_message_never_reaches_the_log_plane(
        self,
        build_app: Callable[..., FastAPI],
        captured_logs: io.StringIO,
    ) -> None:
        message = "My water heater is leaking at 555-222-1919"
        with TestClient(
            build_app(log_access=True, log_pseudonym_key=TEST_PSEUDONYM_KEY),
            raise_server_exceptions=False,
        ) as client:
            _open_turn(client, message)

        written = captured_logs.getvalue()
        assert message not in written
        assert "555-222-1919" not in written
        assert "leaking" not in written

    def test_unauthenticated_requests_log_no_tenant(
        self,
        build_app: Callable[..., FastAPI],
        captured_logs: io.StringIO,
    ) -> None:
        with TestClient(build_app(log_access=True), raise_server_exceptions=False) as client:
            client.get("/api/tenants")

        lines = _json_lines(captured_logs)
        assert any(line.get("event") == "request completed" for line in lines)
        for line in lines:
            assert "tenant" not in line

    def test_access_lines_are_off_by_default(
        self,
        build_app: Callable[..., FastAPI],
        captured_logs: io.StringIO,
    ) -> None:
        with TestClient(build_app(), raise_server_exceptions=False) as client:
            client.get("/api/tenants")

        lines = _json_lines(captured_logs)
        assert not any(line.get("event") == "request completed" for line in lines)
