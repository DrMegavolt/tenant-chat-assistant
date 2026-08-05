"""The chat endpoint over the composition a deployment actually runs.

The hermetic API tests drive the routes against in-memory stores and an
in-memory saver, which proves the handlers but not the wiring: in production the
checkpointer is opened during startup, over its own PostgreSQL pool, by the
lifespan in :func:`tenantchat.api.app.create_app`. That path has one chance to
be wrong and no unit test can see it.

So these run the endpoints against a real database, and the second app instance
is a genuine restart — a new process's worth of objects, sharing nothing with
the first but PostgreSQL. A customer who is mid-confirmation when a rolling
deploy lands is the case being checked.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from tenantchat.api.app import create_app
from tenantchat.api.settings import Settings
from tenantchat.api.visitor import VISITOR_CREDENTIAL_HEADER
from tenantchat.orchestration.model import ModelResponse
from tests.agent_runtime.conftest import (
    BOOKING_TENANT,
    OFFERED_SLOT,
    ScriptedModel,
    booking_arguments,
    tool_call,
)

pytestmark = pytest.mark.integration

_SIGNING_KEY = "a" * 64


def proposal() -> ModelResponse:
    return ModelResponse(
        content="",
        tool_calls=(tool_call("book_appointment", **booking_arguments()),),
        model_name="scripted",
    )


def confirmation() -> ModelResponse:
    return ModelResponse(content="You are booked for Monday at 2pm.", model_name="scripted")


def deployment(database_url: str, script: list[ModelResponse]) -> TestClient:
    """One running instance, composed the way ``make api`` composes it."""
    settings = Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=65536,
        docs_enabled=False,
        database_url=database_url,
        database_pool_size=2,
        database_max_overflow=0,
        admin_gateway_token="gateway-token-for-tests",
        admin_csrf_secret="csrf-secret-for-tests",
        visitor_credential_signing_key=_SIGNING_KEY,
        ingestion_storage_root=tempfile.mkdtemp(prefix="tenantchat-agent-runtime-"),
    )
    return TestClient(create_app(settings, chat_model=ScriptedModel(script=script)))


@dataclass
class Visitor:
    credential: str

    @property
    def headers(self) -> dict[str, str]:
        return {VISITOR_CREDENTIAL_HEADER: self.credential}


@pytest.fixture
def booking_session(agent_database_url: str) -> Iterator[Visitor]:
    """A conversation paused on a booking confirmation, by an instance now gone."""
    with deployment(agent_database_url, [proposal(), confirmation()]) as client:
        opened = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
        visitor = Visitor(credential=opened.json()["credential"])
        granted = client.post(
            "/api/chat/consent",
            json={"purposes": ["booking", "follow_up"]},
            headers=visitor.headers,
        )
        assert granted.status_code == 200, granted.text
        paused = client.post(
            "/api/chat",
            headers=visitor.headers,
            json={"message": "Book HVAC for Monday"},
        )
        assert paused.status_code == 200, paused.text
        assert paused.json()["pending"]["slot"] == OFFERED_SLOT
        assert paused.json()["committed"] == []
    yield visitor


def test_a_booking_paused_before_a_restart_is_confirmed_after_it(
    agent_database_url: str, booking_session: Visitor
) -> None:
    with deployment(agent_database_url, [confirmation()]) as restarted:
        pending = restarted.get("/api/chat/session", headers=booking_session.headers)
        assert pending.json()["pending"]["slot"] == OFFERED_SLOT

        response = restarted.post(
            "/api/chat/confirmation",
            headers=booking_session.headers,
            json={"decision": "approved"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "You are booked for Monday at 2pm."
    assert [action["action"] for action in body["committed"]] == ["book_appointment"]
    assert body["committed"][0]["replayed"] is False


def test_a_transcript_written_by_one_instance_is_read_by_the_next(
    agent_database_url: str, booking_session: Visitor
) -> None:
    """The store is the record, so what was said outlives the process that heard it."""
    with deployment(agent_database_url, [confirmation()]) as restarted:
        response = restarted.get("/api/chat/session", headers=booking_session.headers)

    messages = response.json()["messages"]
    assert [(entry["role"], entry["content"]) for entry in messages] == [
        ("visitor", "Book HVAC for Monday"),
    ]
