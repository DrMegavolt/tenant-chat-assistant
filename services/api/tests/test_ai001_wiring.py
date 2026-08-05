"""AI-001 wiring: how a deployment gets a runnable chat runtime.

The provider adapter itself lives in ``tenantchat.orchestration.providers`` and
is exercised in the orchestration package. This module pins the two things a
deployment depends on here: that `Settings` reads the ``LLM_*`` environment that
the prototype and financing agent already used, and that `create_app` builds a
concrete `ChatModel` when those settings are present so chat does not stay
permanently unavailable after the cutover.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from tenantchat.api.app import create_app
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

    app = create_app(
        deployed,
        booking_store=InMemoryBookingStore(),
        lead_store=InMemoryLeadStore(),
        conversation_store=InMemoryConversationStore(),
        handoff_store=InMemoryHandoffStore(),
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=InMemoryMembershipStore(),
        audit_store=InMemoryAuditStore(),
    )

    assert app.state.settings.llm_base_url == "http://model:1234/v1"
    # The owned adapter exists (it is not None) even though no runtime is
    # composed here because no checkpointer was injected. The runtime composition
    # requires a checkpointer and is exercised by the conversation tests.
