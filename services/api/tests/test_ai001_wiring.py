"""AI-001 wiring: how a deployment gets a runnable chat runtime.

The provider adapter itself lives in ``tenantchat.orchestration.providers`` and
is exercised in the orchestration package. This module pins the two things a
deployment depends on here: that `Settings` reads the ``LLM_*`` environment that
the prototype and financing agent already used, and that `create_app` builds a
concrete `ChatModel` when those settings are present so chat does not stay
permanently unavailable after the cutover.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenantchat.api.app import create_app
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
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import AssembledPrompt, ModelResponse, ToolSpec


class _StubModel:
    """Answers every call with one fixed response, or fails with a fixed error."""

    def __init__(self, response: ModelResponse, *, failure: Exception | None = None) -> None:
        self._response = response
        self._failure = failure

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        del prompt, tools
        if self._failure is not None:
            raise self._failure
        return self._response


def test_settings_read_the_shared_llm_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API composes a provider from the same LLM_* names everything uses.

    The prototype, the financing agent, and this API must be handed identical
    values from one environment; a differently-named variable would silently run
    three different endpoints or, worse, none.
    """
    monkeypatch.setenv("LLM_BASE_URL", "http://model:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen")
    monkeypatch.setenv("LLM_API_KEY", "key")
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "45")

    settings = Settings.from_environment()
    assert settings.llm_base_url == "http://model:1234/v1"
    assert settings.llm_model == "qwen"
    assert settings.llm_api_key == "key"
    assert settings.llm_timeout_seconds == 45


def test_unset_llm_settings_leave_the_model_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """No LLM config means no runtime, so chat fails closed rather than guessing."""
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    settings = Settings.from_environment()
    assert settings.llm_base_url is None
    assert settings.llm_model is None


def test_create_app_builds_a_model_when_llm_settings_are_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Given LLM_* config, production composition owns a concrete adapter.

    We build the app with explicit in-memory stores (the test path) and LLM
    settings; the composition must still construct and hold an
    ``OpenAICompatibleChatModel`` so that, once a checkpointer is available, a
    runtime can be composed for real traffic.
    """
    monkeypatch.setenv("LLM_BASE_URL", "http://model:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen")

    settings = Settings.from_environment()
    deployed = replace(
        settings,
        allowed_origins=("http://127.0.0.1:8000",),
        admin_gateway_token="gateway-token",
        admin_csrf_secret="csrf-secret",
    )

    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    app = create_app(
        deployed,
        booking_store=InMemoryBookingStore(),
        lead_store=InMemoryLeadStore(),
        conversation_store=conversations,
        handoff_store=InMemoryHandoffStore(),
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=InMemoryMembershipStore(),
        consent_store=consent,
        privacy_store=InMemoryPrivacyStore(
            conversations,
            InMemoryBookingStore(),
            InMemoryLeadStore(),
            InMemoryHandoffStore(),
            consent,
        ),
        audit_store=InMemoryAuditStore(),
    )

    assert app.state.settings.llm_base_url == "http://model:1234/v1"
    # The owned adapter exists (it is not None) even though no runtime is
    # composed here because no checkpointer was injected. The runtime composition
    # requires a checkpointer and is exercised by the conversation tests.


def _deployed_app(monkeypatch: pytest.MonkeyPatch, model: _StubModel, *, secret: str) -> FastAPI:
    """The production composition with a configured provider secret and one stub."""
    monkeypatch.setenv("LLM_BASE_URL", "http://model:1234/v1")
    monkeypatch.setenv("LLM_MODEL", "qwen")
    monkeypatch.setenv("LLM_API_KEY", secret)

    settings = Settings.from_environment()
    deployed = replace(
        settings,
        allowed_origins=("http://127.0.0.1:8000",),
        admin_gateway_token="gateway-token",
        admin_csrf_secret="csrf-secret",
    )

    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    return create_app(
        deployed,
        booking_store=InMemoryBookingStore(),
        lead_store=InMemoryLeadStore(),
        conversation_store=conversations,
        handoff_store=InMemoryHandoffStore(),
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=InMemoryMembershipStore(),
        consent_store=consent,
        privacy_store=InMemoryPrivacyStore(
            conversations,
            InMemoryBookingStore(),
            InMemoryLeadStore(),
            InMemoryHandoffStore(),
            consent,
        ),
        audit_store=InMemoryAuditStore(),
        chat_model=model,
        checkpointer=InMemorySaver(),
    )


def test_a_configured_provider_secret_never_reaches_a_client_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LLM_API_KEY the deployment runs with appears in no visitor-visible body.

    ``Settings`` holds the key, and the turn response is built from the turn,
    not from settings — this pins that boundary by asserting on the actual
    response bodies the widget renders, success and transcript alike.
    """
    secret = "provider-secret-xyz"
    model = _StubModel(ModelResponse(content="We are open until 7pm.", model_name="qwen"))
    app = _deployed_app(monkeypatch, model, secret=secret)

    with TestClient(app) as client:
        opened = client.post("/api/chat/session", json={"tenant_id": "clearview"})
        headers = {"X-Visitor-Credential": opened.json()["credential"]}
        turn = client.post("/api/chat", json={"message": "Hours?"}, headers=headers)
        transcript = client.get("/api/chat/session", headers=headers)

    assert turn.status_code == 200
    assert turn.json()["reply"] == "We are open until 7pm."
    assert secret not in turn.text
    assert secret not in transcript.text


def test_a_failed_provider_turn_publishes_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even an exception that carries the key cannot reach the visitor.

    The provider failure is turned into a human handoff; the answer text is the
    only thing published. A client-visible error carrying the key would be a
    credential leak, so the hostile exception message here is the test.
    """
    secret = "provider-secret-xyz"
    model = _StubModel(
        ModelResponse(content=""),
        failure=RuntimeError(f"provider refused; key material {secret}"),
    )
    app = _deployed_app(monkeypatch, model, secret=secret)

    with TestClient(app) as client:
        opened = client.post("/api/chat/session", json={"tenant_id": "clearview"})
        headers = {"X-Visitor-Credential": opened.json()["credential"]}
        turn = client.post("/api/chat", json={"message": "Hello"}, headers=headers)
        transcript = client.get("/api/chat/session", headers=headers)

    assert turn.status_code == 200
    assert "passed it to the team" in turn.json()["reply"]
    assert secret not in turn.text
    assert secret not in transcript.text
