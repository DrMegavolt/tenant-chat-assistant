"""The SEC-003 abuse surface as regression tests.

The guarantee is the contract, not the numbers: refusals are RFC 9457 problem
documents carrying a request ID and a bounded ``Retry-After``; identity keys
never appear in a refusal body or a log line; the size caps hold for chunked
uploads; and each budget counts what its name says it counts. The defaults are
tuned per test so a burst of a handful of requests is already over budget.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import Any, NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from starlette.types import Receive, Scope, Send

from tenantchat.api.app import create_app
from tenantchat.api.guards import BodySizeLimitMiddleware
from tenantchat.api.limits import RateLimitPolicy
from tenantchat.api.problems import PROBLEM_CONTENT_TYPE, REQUEST_ID_HEADER
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    InMemoryAuditStore,
    InMemoryBookingStore,
    InMemoryConversationStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryLeadStore,
    InMemoryMembershipStore,
)
from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import ModelMessage, ModelResponse, ToolSpec

BOOKING_TENANT = "clearview"


def _stores() -> dict[str, Any]:
    return {
        "booking_store": InMemoryBookingStore(),
        "lead_store": InMemoryLeadStore(),
        "conversation_store": InMemoryConversationStore(),
        "handoff_store": InMemoryHandoffStore(),
        "idempotency_store": InMemoryIdempotencyStore(),
        "membership_store": InMemoryMembershipStore(),
        "audit_store": InMemoryAuditStore(),
    }


class SlowModel:
    """Answers after a delay, so a test can hold a request in flight."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay = delay_seconds

    async def complete(
        self, messages: Sequence[ModelMessage], *, tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        await asyncio.sleep(self._delay)
        return ModelResponse(content="Done.", model_name="slow")


@pytest.fixture
def build_app(settings: Settings, model: Any) -> Callable[..., FastAPI]:
    """Compose an app with test-scaled settings; each call gets fresh stores."""

    def build(**overrides: Any) -> FastAPI:
        resolved = dataclasses.replace(settings, **overrides)
        return create_app(
            resolved,
            chat_model=model,
            checkpointer=InMemorySaver(),
            **_stores(),
        )

    return build


@pytest.fixture
def client(build_app: Callable[..., FastAPI]) -> Iterator[TestClient]:
    """A client over a freshly built app with fresh rate budgets."""
    with TestClient(build_app(), raise_server_exceptions=False) as test_client:
        yield test_client


class OpenedSession(NamedTuple):
    """A conversation and the credential that names it (SEC-002)."""

    session_id: str
    headers: dict[str, str]


def _open_session(client: TestClient, tenant_id: str = BOOKING_TENANT) -> OpenedSession:
    response = client.post("/api/chat/session", json={"tenant_id": tenant_id})
    assert response.status_code == 201, response.text
    body = response.json()
    return OpenedSession(
        session_id=body["session"]["session_id"],
        headers={VISITOR_CREDENTIAL_HEADER: body["credential"]},
    )


class TestRateLimits:
    def test_a_burst_from_one_identity_hits_the_ip_cap(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        policy = RateLimitPolicy(ip_requests=3, window_seconds=60)
        with TestClient(build_app(rate_limits=policy), raise_server_exceptions=False) as client:
            for _ in range(3):
                assert client.get("/api/tenants").status_code == 200
            refusal = client.get("/api/tenants")

        assert refusal.status_code == 429
        assert refusal.headers["content-type"] == PROBLEM_CONTENT_TYPE
        document = refusal.json()
        assert document["code"] == "rate_limited"
        assert document["status"] == 429
        assert document["requestId"] == refusal.headers[REQUEST_ID_HEADER]
        retry_after = int(refusal.headers["Retry-After"])
        assert 1 <= retry_after <= policy.window_seconds
        assert document["retryAfterSeconds"] == retry_after
        assert document["limitScope"] == "ip"

    def test_healthz_is_exempt_from_rate_budgets(self, build_app: Callable[..., FastAPI]) -> None:
        policy = RateLimitPolicy(ip_requests=1, window_seconds=60)
        with TestClient(build_app(rate_limits=policy), raise_server_exceptions=False) as client:
            for _ in range(3):
                assert client.get("/healthz").status_code == 200

    def test_the_session_budget_keys_on_the_server_issued_id(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        """Two turns in one conversation exceed the budget; a new conversation does not."""
        policy = RateLimitPolicy(
            ip_requests=100,
            session_requests=1,
            tenant_requests=100,
            window_seconds=60,
        )
        with TestClient(build_app(rate_limits=policy), raise_server_exceptions=False) as client:
            session = _open_session(client)
            first_turn = client.post("/api/chat", json={"message": "hi"}, headers=session.headers)
            assert first_turn.status_code == 200, first_turn.text
            second_turn = client.post(
                "/api/chat", json={"message": "again"}, headers=session.headers
            )
            other_session = _open_session(client)
            other_turn = client.post(
                "/api/chat", json={"message": "elsewhere"}, headers=other_session.headers
            )

        assert second_turn.status_code == 429
        assert second_turn.json()["limitScope"] == "session"
        assert other_turn.status_code == 200, other_turn.text

    def test_a_body_field_cannot_move_the_session_budget(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        """The budget keys on the credential, so extra body fields cannot shift it.

        The keys used to come from the request body. Once SEC-002 moved visitor
        identity into the signed header, a body-keyed extractor would have found
        nothing on `/api/chat` and quietly dropped the tenant and session budgets
        to an IP-only bound.
        """
        policy = RateLimitPolicy(
            ip_requests=100,
            session_requests=1,
            tenant_requests=100,
            window_seconds=60,
        )
        with TestClient(build_app(rate_limits=policy), raise_server_exceptions=False) as client:
            session = _open_session(client)
            first = client.post("/api/chat", json={"message": "hi"}, headers=session.headers)
            assert first.status_code == 200, first.text
            # A forged session label in the body must not buy a fresh budget.
            second = client.post(
                "/api/chat",
                json={"message": "again", "session_id": str(uuid.uuid4())},
                headers=session.headers,
            )

        assert second.status_code == 429
        assert second.json()["limitScope"] == "session"

    def test_a_refusal_never_leaks_the_key(
        self, build_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture
    ) -> None:
        """The IP, tenant, and session values appear neither in the body nor the log."""
        policy = RateLimitPolicy(
            ip_requests=100,
            session_requests=1,
            tenant_requests=100,
            window_seconds=60,
        )
        caplog.set_level(logging.WARNING)
        with TestClient(build_app(rate_limits=policy), raise_server_exceptions=False) as client:
            session = _open_session(client)
            session_id = session.session_id
            client.post("/api/chat", json={"message": "hi"}, headers=session.headers)
            refusal = client.post("/api/chat", json={"message": "again"}, headers=session.headers)

        assert refusal.status_code == 429
        assert session_id not in refusal.text
        assert BOOKING_TENANT not in refusal.text
        records = [record for record in caplog.records if "rate-limited" in record.getMessage()]
        assert records
        for record in records:
            assert session_id not in record.getMessage()
            assert BOOKING_TENANT not in record.getMessage()
            # The scoped log carries only the budget name, never the key value.
            assert record.__dict__.get("scope") in {"session", "tenant", "ip"}


class TestConcurrencyLimit:
    def test_in_flight_turns_share_the_session_concurrency_budget(
        self, settings: Settings, model: Any
    ) -> None:
        """A slow turn blocks the second turn on the same conversation."""
        policy = RateLimitPolicy(
            ip_requests=100,
            session_requests=100,
            tenant_requests=100,
            session_concurrency=1,
            window_seconds=60,
        )
        resolved = dataclasses.replace(settings, rate_limits=policy)
        app = create_app(
            resolved,
            chat_model=SlowModel(delay_seconds=0.25),
            checkpointer=InMemorySaver(),
            **_stores(),
        )

        async def scenario() -> tuple[int, int]:
            async with AsyncClient(
                transport=ASGITransport(app), base_url="http://test", timeout=10.0
            ) as client:
                opened = await client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
                assert opened.status_code == 201, opened.text
                headers = {VISITOR_CREDENTIAL_HEADER: opened.json()["credential"]}
                first, second = await asyncio.gather(
                    client.post("/api/chat", json={"message": "first"}, headers=headers),
                    client.post("/api/chat", json={"message": "second"}, headers=headers),
                )
                return first.status_code, second.status_code

        first_status, second_status = asyncio.run(scenario())
        assert sorted((first_status, second_status)) == [200, 429]


class TestBodyLimits:
    def test_a_declared_oversized_body_is_refused_before_the_router_sees_it(
        self, build_app: Callable[..., FastAPI], settings: Settings
    ) -> None:
        """The 413 is a problem document with the cap attached, not an HTML page."""
        payload = json.dumps({"tenant_id": BOOKING_TENANT, "padding": "x" * 4096}).encode()
        with TestClient(build_app(), raise_server_exceptions=False) as client:
            response = client.post(
                "/api/tenants",
                content=payload,
                headers={"content-type": "application/json"},
            )

        assert response.status_code == 413
        assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
        document = response.json()
        assert document["code"] == "request_too_large"
        assert document["maxBytes"] == settings.max_request_bytes
        assert document["requestId"] == response.headers[REQUEST_ID_HEADER]
        assert "padding" not in response.text

    def test_a_chunked_oversized_body_is_refused_too(self) -> None:
        """No Content-Length is declared, so only the receive-channel counter can stop it."""

        async def echo_app(scope: Scope, receive: Receive, send: Send) -> None:
            total = 0
            while True:
                message = await receive()
                if message["type"] == "http.request":
                    total += len(message.get("body", b""))
                    if not message.get("more_body", False):
                        break
                elif message["type"] == "http.disconnect":
                    return
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": str(total).encode()})

        parts = [b"0123456789", b"0123456789", b"0123456789"]

        async def receive() -> dict[str, Any]:
            if not parts:
                return {"type": "http.request", "body": b"", "more_body": False}
            body = parts.pop(0)
            return {"type": "http.request", "body": body, "more_body": bool(parts)}

        async def scenario() -> int:
            sent: list[dict[str, Any]] = []
            wrapped = BodySizeLimitMiddleware(echo_app, max_bytes=16)
            await wrapped(
                {"type": "http", "method": "POST", "path": "/upload", "headers": [], "state": {}},
                receive,
                sent.append,  # type: ignore[arg-type]
            )
            for message in sent:
                if message["type"] == "http.response.start":
                    status: int = message["status"]
                    return status
            raise AssertionError("no response emitted")

        assert asyncio.run(scenario()) == 413


class TestResponseLimits:
    def test_a_response_over_the_cap_is_replaced_by_a_413(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        """The tenant catalog is far larger than this cap, so the guard must refuse."""
        with TestClient(build_app(max_response_bytes=400), raise_server_exceptions=False) as client:
            response = client.get("/api/tenants")

        assert response.status_code == 413
        document = response.json()
        assert document["code"] == "response_too_large"
        assert document["maxBytes"] == 400
        assert response.headers[REQUEST_ID_HEADER]

    def test_small_responses_pass_under_the_same_cap(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        with TestClient(build_app(max_response_bytes=400), raise_server_exceptions=False) as client:
            assert client.get("/healthz").status_code == 200


class TestSecurityHeaders:
    @pytest.mark.parametrize(
        "path",
        ["/healthz", "/api/tenants", "/no/such/route", "/api/chat/session"],
    )
    def test_every_response_pins_the_header_posture(self, client: TestClient, path: str) -> None:
        response = client.get(path)

        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers[REQUEST_ID_HEADER]

    def test_guard_refusals_carry_the_same_posture(self, build_app: Callable[..., FastAPI]) -> None:
        policy = RateLimitPolicy(ip_requests=1, window_seconds=60)
        with TestClient(build_app(rate_limits=policy), raise_server_exceptions=False) as client:
            assert client.get("/api/tenants").status_code == 200
            response = client.get("/api/tenants")

        assert response.status_code == 429
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["cache-control"] == "no-store"


class TestHistoryCap:
    def test_the_transcript_read_is_truncated_to_the_history_cap(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        with TestClient(build_app(max_history_messages=2), raise_server_exceptions=False) as client:
            session = _open_session(client)
            for _ in range(3):
                turn = client.post(
                    "/api/chat",
                    json={"message": "what are your hours?"},
                    headers=session.headers,
                )
                assert turn.status_code == 200, turn.text
            transcript = client.get("/api/chat/session", headers=session.headers)

        messages = transcript.json()["messages"]
        assert len(messages) == 2
        assert [message["role"] for message in messages] == ["visitor", "assistant"]


class TestSchemaBounds:
    def test_an_overlong_message_hits_the_schema_bound_not_the_body_cap(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        """The message limit is the domain's 4000, so the request must fit the body."""
        with TestClient(
            build_app(max_request_bytes=16 * 1024), raise_server_exceptions=False
        ) as client:
            session = _open_session(client)
            response = client.post(
                "/api/chat", json={"message": "x" * 4001}, headers=session.headers
            )

        assert response.status_code == 422
        document = response.json()
        assert document["code"] == "malformed_request"
        assert any("message" in field["location"] for field in document["invalidFields"])
        assert "xxxx" not in response.text


class TestCrossOriginSurface:
    def test_an_allowed_origin_receives_cors_headers(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        with TestClient(build_app(), raise_server_exceptions=False) as client:
            response = client.get("/api/tenants", headers={"Origin": "https://widget.example"})

        assert response.headers.get("access-control-allow-origin") == "https://widget.example"

    def test_a_disallowed_origin_receives_none(self, build_app: Callable[..., FastAPI]) -> None:
        with TestClient(build_app(), raise_server_exceptions=False) as client:
            response = client.get("/api/tenants", headers={"Origin": "https://evil.example"})

        assert "access-control-allow-origin" not in response.headers

    def test_admin_routes_never_expose_cors_headers(
        self, build_app: Callable[..., FastAPI]
    ) -> None:
        """The allowlist is the widget's surface; the console stays out of it."""
        with TestClient(build_app(), raise_server_exceptions=False) as client:
            response = client.get("/api/admin/chats", headers={"Origin": "https://widget.example"})

        assert response.status_code == 401
        assert "access-control-allow-origin" not in response.headers
