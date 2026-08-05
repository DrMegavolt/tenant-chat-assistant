"""The OpenAI-compatible adapter driving the deployed runtime over Postgres.

The hermetic suites prove the adapter's wire contract and the runtime's
behavior, each against its own double. This test is the composition a deployment
actually runs: production PostgreSQL stores and checkpointer, the real
OpenAI-compatible adapter, and only the transport faked. A full booking flows
from a wire-level tool call to a committed reservation, so the adapter is
proven inside the loop it exists for.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from tenantchat.api.app import create_app
from tenantchat.api.settings import Settings
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel
from tests.agent_runtime.conftest import BOOKING_TENANT, OFFERED_SLOT, booking_arguments

pytestmark = pytest.mark.integration

_SIGNING_KEY = "a" * 64
_PROVIDER_KEY = "test-provider-secret"


def _tool_call_response() -> dict[str, object]:
    return {
        "choices": [
            {
                "message": {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call-book",
                            "type": "function",
                            "function": {
                                "name": "book_appointment",
                                "arguments": json.dumps(booking_arguments()),
                            },
                        }
                    ],
                }
            }
        ],
        "model": "local-model",
        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
    }


def _prose_response() -> dict[str, object]:
    return {
        "choices": [{"message": {"content": "You are booked for Monday at 2pm."}}],
        "model": "local-model",
        "usage": {"prompt_tokens": 2, "completion_tokens": 4, "total_tokens": 6},
    }


def _provider(requests: list[httpx.Request]) -> OpenAICompatibleChatModel:
    """The adapter with a fake transport, recording every request it made."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        for message in body["messages"]:
            assert _PROVIDER_KEY not in json.dumps(message)
        # A transcript that already holds an assistant tool call is the loop
        # asking for the result's answer; the first call gets the proposal.
        assistant_calls = [
            entry
            for message in body["messages"]
            if message["role"] == "assistant"
            for entry in message.get("tool_calls", ())
        ]
        if assistant_calls:
            return httpx.Response(200, json=_prose_response())
        return httpx.Response(200, json=_tool_call_response())

    return OpenAICompatibleChatModel(
        base_url="http://provider/v1",
        model="local-model",
        api_key=_PROVIDER_KEY,
        transport=httpx.MockTransport(handler),
    )


def test_a_booking_flows_through_the_openai_compatible_adapter(
    agent_database_url: str,
) -> None:
    """One conversation: wire-level tool call, confirmation, committed booking.

    The provider is faked at the transport only. The adapter parses the wire
    tool call, the graph runs it against the production stores, pauses for the
    customer, and commits the reservation on approval — the same path a
    deployment answers turns with.
    """
    requests: list[httpx.Request] = []
    settings = Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=65536,
        docs_enabled=False,
        database_url=agent_database_url,
        database_pool_size=2,
        database_max_overflow=0,
        admin_gateway_token="gateway-token-for-tests",
        admin_csrf_secret="csrf-secret-for-tests",
        visitor_credential_signing_key=_SIGNING_KEY,
    )
    with TestClient(create_app(settings, chat_model=_provider(requests))) as client:
        opened = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
        headers = {"X-Visitor-Credential": opened.json()["credential"]}
        granted = client.post(
            "/api/chat/consent",
            json={"purposes": ["booking", "follow_up"]},
            headers=headers,
        )
        assert granted.status_code == 200, granted.text

        paused = client.post("/api/chat", json={"message": "Book HVAC for Monday"}, headers=headers)
        assert paused.status_code == 200, paused.text
        assert paused.json()["pending"]["slot"] == OFFERED_SLOT
        assert paused.json()["committed"] == []

        confirmed = client.post(
            "/api/chat/confirmation", json={"decision": "approved"}, headers=headers
        )

    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["reply"] == "You are booked for Monday at 2pm."
    assert [action["action"] for action in body["committed"]] == ["book_appointment"]
    assert body["committed"][0]["replayed"] is False
    assert body["provenance"]["model_name"] == "local-model"

    # The provider was reached over its own wire contract with the secret in
    # the Authorization header and in no payload.
    assert len(requests) == 2
    for request in requests:
        assert request.headers["Authorization"] == f"Bearer {_PROVIDER_KEY}"
        assert _PROVIDER_KEY not in json.dumps(json.loads(request.content))
