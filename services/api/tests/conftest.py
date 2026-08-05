"""Test fixtures for the HTTP edge.

Settings are constructed explicitly rather than read from the environment, so a
variable left in a developer's shell cannot change what these tests assert.

Payload builders are fixtures rather than importable helpers: pytest injects them
without a cross-module import, and no test module ends up depending on another's
import path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient

from tenantchat.api.app import create_app
from tenantchat.api.identity import (
    EMAIL_HEADER,
    GATEWAY_TOKEN_HEADER,
    ROLE_HEADER,
    SUBJECT_HEADER,
)
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
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import ModelMessage, ModelResponse, ToolCall, ToolSpec

# Small enough that a body-limit test can exceed it without building a megabyte.
TEST_MAX_REQUEST_BYTES = 2048

TEST_GATEWAY_TOKEN = "gateway-token-for-tests"
TEST_CSRF_SECRET = "csrf-secret-for-tests"

BOOKING_TENANT = "clearview"
LEAD_TENANT = "apex"
OFFERED_SLOT = "Mon Jul 1, 2:00 PM"

# The default operator every test uses; it is a support agent in both seeded
# tenants unless a test re-seeds the membership store.
TEST_OPERATOR_SUBJECT = "operator-7"


@dataclass
class ScriptedModel:
    """Replays a fixed list of responses, then repeats the last one.

    A script rather than a recording: every assertion in these tests is about
    what the HTTP layer stores and publishes, none of which should change when
    a provider rephrases an answer. Tests that need different behavior assign to
    ``script`` before their first request.
    """

    script: list[ModelResponse]
    calls: list[tuple[ModelMessage, ...]] = field(default_factory=list)

    async def complete(
        self, messages: Sequence[ModelMessage], *, tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        self.calls.append(tuple(messages))
        return self.script[min(len(self.calls) - 1, len(self.script) - 1)]


def booking_call() -> ToolCall:
    """A complete, valid booking proposal against the booking-enabled tenant."""
    return ToolCall(
        call_id="call-book",
        name="book_appointment",
        arguments={
            "service": "HVAC",
            "slot": OFFERED_SLOT,
            "customer_name": "Dana Ruiz",
            "customer_phone_or_email": "555-222-1919",
            "address": "12 Alder Court, Portland, OR 97205",
        },
    )


@pytest.fixture
def settings() -> Settings:
    return Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=TEST_MAX_REQUEST_BYTES,
        docs_enabled=True,
        admin_gateway_token=TEST_GATEWAY_TOKEN,
        admin_csrf_secret=TEST_CSRF_SECRET,
        dev_auth=False,
    )


@pytest.fixture
def membership_store() -> InMemoryMembershipStore:
    """The operator is a support agent in both seeded tenants by default."""
    store = InMemoryMembershipStore()
    for tenant_id in (BOOKING_TENANT, LEAD_TENANT):
        asyncio.run(
            store.assign(
                tenant_id=tenant_id,
                subject=TEST_OPERATOR_SUBJECT,
                role="support_agent",
            )
        )
    return store


@pytest.fixture
def audit_store() -> InMemoryAuditStore:
    """A fresh accountability log per test, so rows never leak between tests."""
    return InMemoryAuditStore()


@pytest.fixture
def model() -> ScriptedModel:
    """A model that answers in one call and asks for nothing."""
    return ScriptedModel([ModelResponse(content="We are open until 7pm.", model_name="scripted")])


@pytest.fixture
def client(
    settings: Settings,
    model: ScriptedModel,
    membership_store: InMemoryMembershipStore,
    audit_store: InMemoryAuditStore,
) -> Iterator[TestClient]:
    """A client over a freshly built app, so stored records never leak between tests."""
    # `raise_server_exceptions=False` returns the 500 an operator would see
    # instead of re-raising, which would hide whether the handler ran at all.
    with TestClient(
        create_app(
            settings,
            booking_store=InMemoryBookingStore(),
            lead_store=InMemoryLeadStore(),
            conversation_store=InMemoryConversationStore(),
            handoff_store=InMemoryHandoffStore(),
            idempotency_store=InMemoryIdempotencyStore(),
            membership_store=membership_store,
            audit_store=audit_store,
            chat_model=model,
            checkpointer=InMemorySaver(),
        ),
        raise_server_exceptions=False,
    ) as test_client:
        yield test_client


@pytest.fixture
def modelless_client(
    settings: Settings,
    membership_store: InMemoryMembershipStore,
) -> Iterator[TestClient]:
    """The deployment `AI-001` has not reached yet: stores, but no model."""
    with TestClient(
        create_app(
            settings,
            booking_store=InMemoryBookingStore(),
            lead_store=InMemoryLeadStore(),
            conversation_store=InMemoryConversationStore(),
            handoff_store=InMemoryHandoffStore(),
            idempotency_store=InMemoryIdempotencyStore(),
            membership_store=membership_store,
            audit_store=InMemoryAuditStore(),
        ),
        raise_server_exceptions=False,
    ) as test_client:
        yield test_client


@pytest.fixture
def operator_headers() -> Callable[..., dict[str, str]]:
    """The headers the gateway injects for an authenticated operator."""

    def build(role: str = "support_agent", **overrides: str) -> dict[str, str]:
        return {
            GATEWAY_TOKEN_HEADER: TEST_GATEWAY_TOKEN,
            SUBJECT_HEADER: "operator-7",
            EMAIL_HEADER: "operator@example.com",
            ROLE_HEADER: role,
        } | overrides

    return build


@pytest.fixture
def open_session(client: TestClient) -> Callable[..., str]:
    """Open a conversation and return its server-issued ID."""

    def build(tenant_id: str = BOOKING_TENANT) -> str:
        response = client.post("/api/chat/session", json={"tenant_id": tenant_id})
        assert response.status_code == 201, response.text
        session_id: str = response.json()["session"]["session_id"]
        return session_id

    return build


@pytest.fixture
def booking_payload() -> Callable[..., dict[str, object]]:
    """A complete, valid booking against the seeded booking-enabled tenant."""

    def build(**overrides: object) -> dict[str, object]:
        return {
            "tenant_id": "clearview",
            "session_id": "session-test",
            "service": "HVAC",
            "slot": "Mon Jul 1, 2:00 PM",
            "customer_name": "Dana Ruiz",
            "address": "12 Alder Court, Portland, OR 97205",
            "contact": "555-222-1919",
        } | overrides

    return build


@pytest.fixture
def lead_payload() -> Callable[..., dict[str, object]]:
    """A complete, valid lead against the seeded lead-enabled tenant."""

    def build(**overrides: object) -> dict[str, object]:
        return {
            "tenant_id": "apex",
            "session_id": "session-test",
            "customer_name": "Dana Ruiz",
            "contact": "dana@example.com",
            "service": "HVAC",
            "summary": "Furnace is making a grinding noise.",
        } | overrides

    return build
