"""AI-002 wiring: quotas, policy blocks, fallbacks, and cache through the app.

The unit surface (ledger, fallback chain, cache) lives in the core and
orchestration packages. This module pins the composition: that `create_app`
enforces the budget in real turns, that an exhausted quota degrades without
executing an action, that blocks and spend alerts are measurable, and that a
deployment with a fallback or a cache setting actually composes one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client.samples import Sample

from services.api.tests.conftest import BOOKING_TENANT, LEAD_TENANT, ScriptedModel, VisitorSession
from tenantchat.api.app import create_app
from tenantchat.api.metrics import METRICS
from tenantchat.api.registry import TenantRecord, TenantRegistry
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
from tenantchat.core.budgets import BudgetEnforcer, TenantBudget
from tenantchat.core.metrics import AlertLevel
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import ModelResponse, ToolCall
from tenantchat.orchestration.providers.cache import CachingChatModel
from tenantchat.orchestration.providers.fallback import FallbackChatModel
from tenantchat.orchestration.providers.recording import MetricRecordingChatModel

TEMPLATE_REF = "dispatch-system@4"


def _metrics_settings() -> Settings:
    return Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=2048,
        docs_enabled=True,
        admin_gateway_token="gateway-token-for-tests",
        admin_csrf_secret="csrf-secret-for-tests",
        dev_auth=False,
    )


@pytest.fixture(autouse=True)
def reset_metrics() -> Iterator[None]:
    METRICS.reset()
    yield
    METRICS.reset()


def _registry_with(tenant_id: str, budgets: TenantBudget) -> TenantRegistry:
    """The seeded registry with one tenant's budgets replaced."""
    records = TenantRegistry.seeded().all()
    record = records[tenant_id]
    records = {**records, tenant_id: TenantRecord(policy=replace(record.policy, budgets=budgets))}
    return TenantRegistry(records)


def _app(
    *,
    model: ScriptedModel,
    registry: TenantRegistry | None = None,
    budgets: BudgetEnforcer | None = None,
    settings: Settings | None = None,
    lead_store: InMemoryLeadStore | None = None,
) -> FastAPI:
    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    return create_app(
        settings or _metrics_settings(),
        registry=registry,
        booking_store=InMemoryBookingStore(),
        lead_store=lead_store or InMemoryLeadStore(),
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
        budgets=budgets,
    )


def _open_session(client: TestClient, *, tenant_id: str = BOOKING_TENANT) -> VisitorSession:
    opened = client.post("/api/chat/session", json={"tenant_id": tenant_id})
    assert opened.status_code == 201, opened.text
    visitor = VisitorSession(
        tenant_id,
        opened.json()["session"]["session_id"],
        opened.json()["credential"],
    )
    granted = client.post(
        "/api/chat/consent",
        json={"purposes": ["booking", "follow_up"]},
        headers=visitor.headers,
    )
    assert granted.status_code == 200, granted.text
    return visitor


def tenantchat_samples() -> list[Sample]:
    return [
        sample
        for metric in METRICS.registry.collect()
        for sample in metric.samples
        if sample.name.startswith("tenantchat_")
    ]


def _sample_value(name: str, **labels: str) -> float:
    for sample in tenantchat_samples():
        if sample.name == name and all(sample.labels.get(k) == v for k, v in labels.items()):
            return sample.value
    return 0.0


class TestQuotaExhaustion:
    def test_an_exhausted_token_budget_blocks_the_turn_without_a_model_call(
        self,
    ) -> None:
        ledger = BudgetEnforcer()
        model = ScriptedModel(
            [
                ModelResponse(
                    content="Open until 7pm.",
                    model_name="scripted",
                    usage={"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
                )
            ]
        )
        app = _app(
            model=model,
            registry=_registry_with(BOOKING_TENANT, TenantBudget(daily_token_budget=60)),
            budgets=ledger,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            visitor = _open_session(client)
            first = client.post(
                "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
            )
            second = client.post(
                "/api/chat", json={"message": "Are you open on Sunday?"}, headers=visitor.headers
            )

        assert first.status_code == second.status_code == 200
        assert "Open until 7pm." in first.json()["reply"]
        # The second turn is refused predictably: a server reply, no model call.
        assert "usage limit" in second.json()["reply"]
        assert len(model.calls) == 1
        assert ledger.snapshot(BOOKING_TENANT).tokens_used == 60
        assert _sample_value("tenantchat_policy_blocks_total", reason="budget_exhausted") == 1

    def test_usage_is_attributed_to_the_tenant_that_spent_it(self) -> None:
        ledger = BudgetEnforcer()
        model = ScriptedModel(
            [
                ModelResponse(
                    content="Open until 7pm.",
                    model_name="scripted",
                    usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
                )
            ]
        )
        app = _app(
            model=model,
            registry=_registry_with(BOOKING_TENANT, TenantBudget()),
            budgets=ledger,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            visitor = _open_session(client)
            client.post(
                "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
            )

        assert ledger.snapshot(BOOKING_TENANT).tokens_used == 12
        assert ledger.snapshot("some-other-tenant").tokens_used == 0


class TestActionBudget:
    def test_an_exhausted_action_budget_refuses_a_lead_without_committing(self) -> None:
        ledger = BudgetEnforcer()
        lead_store = InMemoryLeadStore()
        # Preload one action so the tenant is at its daily cap before the turn.
        asyncio.run(ledger.record_action(LEAD_TENANT, turn_index=0))
        model = ScriptedModel(
            [
                ModelResponse(
                    content="",
                    tool_calls=(
                        ToolCall(
                            call_id="call-lead",
                            name="create_lead",
                            arguments={
                                "customer_name": "Dana Ruiz",
                                "customer_phone_or_email": "555-222-1919",
                                "service": "HVAC",
                                "summary": "Furnace is noisy",
                            },
                        ),
                    ),
                    model_name="scripted",
                ),
                ModelResponse(content="The team will call you back.", model_name="scripted"),
            ]
        )
        app = _app(
            model=model,
            registry=_registry_with(LEAD_TENANT, TenantBudget(max_actions_per_day=1)),
            budgets=ledger,
            lead_store=lead_store,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            visitor = _open_session(client, tenant_id=LEAD_TENANT)
            response = client.post(
                "/api/chat", json={"message": "Call me back about HVAC"}, headers=visitor.headers
            )

        assert response.status_code == 200
        assert "call you back" in response.json()["reply"]
        assert ledger.snapshot(LEAD_TENANT).actions_committed == 1
        assert _sample_value("tenantchat_policy_blocks_total", reason="action_limit") == 1
        # No partial action: the refused lead created no row, and the refusal
        # reached the model as a tool result it could work around.
        leads = asyncio.run(lead_store.for_tenant(LEAD_TENANT))
        assert leads == ()
        tool_payloads = [
            message.content
            for prompt in model.calls
            for message in prompt.messages
            if message.role == "tool"
        ]
        assert any("action_quota_exceeded" in payload for payload in tool_payloads)


class TestSpendAlerts:
    def test_crossing_the_warn_threshold_fires_a_one_shot_alert(self) -> None:
        ledger = BudgetEnforcer(metrics=METRICS)
        model = ScriptedModel(
            [
                ModelResponse(
                    content="Open until 7pm.",
                    model_name="scripted",
                    usage={"prompt_tokens": 50, "completion_tokens": 0, "total_tokens": 50},
                )
            ]
        )
        app = _app(
            model=model,
            registry=_registry_with(
                BOOKING_TENANT,
                TenantBudget(daily_token_budget=1000, spend_warn_threshold_tokens=40),
            ),
            budgets=ledger,
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            visitor = _open_session(client)
            response = client.post(
                "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
            )

        assert response.status_code == 200
        assert _sample_value("tenantchat_budget_alerts_total", level="warn") == 1
        assert AlertLevel.WARN in ledger.snapshot(BOOKING_TENANT).alerts_fired


class TestContentPolicy:
    def test_an_over_length_message_is_blocked_before_any_model_call(self) -> None:
        model = ScriptedModel([ModelResponse(content="should never run", model_name="scripted")])
        app = _app(
            model=model,
            registry=_registry_with(BOOKING_TENANT, TenantBudget(max_message_chars=20)),
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            visitor = _open_session(client)
            response = client.post(
                "/api/chat",
                json={
                    "message": "I would like to know what your hours are for today and tomorrow",
                },
                headers=visitor.headers,
            )

        assert response.status_code == 200
        assert "could not read that message" in response.json()["reply"]
        assert len(model.calls) == 0
        assert _sample_value("tenantchat_policy_blocks_total", reason="input_too_long") == 1

    def test_over_length_model_output_is_refused_whole(self) -> None:
        model = ScriptedModel(
            [
                ModelResponse(
                    content="We are open until 7pm on weekdays and this is long",
                    model_name="scripted",
                )
            ]
        )
        app = _app(
            model=model,
            registry=_registry_with(BOOKING_TENANT, TenantBudget(max_output_chars=10)),
        )
        with TestClient(app, raise_server_exceptions=False) as client:
            visitor = _open_session(client)
            response = client.post(
                "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
            )

        assert response.status_code == 200
        assert "could not finish that answer" in response.json()["reply"]
        assert "Open until 7pm" not in response.json()["reply"]
        assert len(model.calls) == 1
        assert _sample_value("tenantchat_policy_blocks_total", reason="output_too_long") == 1


class TestFallbackComposition:
    def test_configured_primary_and_fallback_are_composed_as_a_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_BASE_URL", "http://primary/v1")
        monkeypatch.setenv("LLM_MODEL", "primary-model")
        monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "http://secondary/v1")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "secondary-model")

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

        model = app.state.chat_model
        assert isinstance(model, MetricRecordingChatModel)
        assert isinstance(model._inner, FallbackChatModel)
        assert settings.llm_fallback_base_url == "http://secondary/v1"


class TestResponseCacheComposition:
    def test_a_cached_turn_skips_the_model_and_records_the_hit(self) -> None:
        settings = replace(_metrics_settings(), llm_response_cache=True)
        model = ScriptedModel([ModelResponse(content="Open until 7pm.", model_name="scripted")])
        app = _app(model=model, settings=settings)

        with TestClient(app, raise_server_exceptions=False) as client:
            # Two separate conversations asking the same first question produce
            # byte-identical assembled prompts, so the second is served from the
            # cache — that is exactly the safe, non-personalized case.
            first = _open_session(client)
            second = _open_session(client)
            first_turn = client.post(
                "/api/chat", json={"message": "What are your hours?"}, headers=first.headers
            )
            second_turn = client.post(
                "/api/chat", json={"message": "What are your hours?"}, headers=second.headers
            )

        assert first_turn.status_code == second_turn.status_code == 200
        assert first_turn.json()["reply"] == second_turn.json()["reply"] == "Open until 7pm."
        assert len(model.calls) == 1
        assert _sample_value("tenantchat_response_cache_total", result="miss") == 1
        assert _sample_value("tenantchat_response_cache_total", result="hit") == 1
        assert _sample_value("tenantchat_llm_calls_total", status="ok", template=TEMPLATE_REF) == 1

    def test_the_cache_wrapper_is_composed_when_enabled(self) -> None:
        settings = replace(_metrics_settings(), llm_response_cache=True)
        model = ScriptedModel([ModelResponse(content="Open until 7pm.", model_name="scripted")])
        app = _app(model=model, settings=settings)

        assert isinstance(app.state.chat_model, CachingChatModel)


class TestConfigurationSurface:
    def test_settings_read_the_fallback_and_cache_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("LLM_FALLBACK_BASE_URL", "http://backup:8080/v1")
        monkeypatch.setenv("LLM_FALLBACK_MODEL", "backup-model")
        monkeypatch.setenv("LLM_FALLBACK_API_KEY", "backup-key")
        monkeypatch.setenv("CHAT_API_LLM_RESPONSE_CACHE", "true")
        monkeypatch.setenv("CHAT_API_RESPONSE_CACHE_TTL_SECONDS", "120")

        settings = Settings.from_environment()

        assert settings.llm_fallback_base_url == "http://backup:8080/v1"
        assert settings.llm_fallback_model == "backup-model"
        assert settings.llm_fallback_api_key == "backup-key"
        assert settings.llm_response_cache is True
        assert settings.llm_response_cache_ttl_seconds == 120

    def test_settings_default_to_no_fallback_and_no_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("LLM_FALLBACK_BASE_URL", raising=False)
        monkeypatch.delenv("LLM_FALLBACK_MODEL", raising=False)
        monkeypatch.delenv("CHAT_API_LLM_RESPONSE_CACHE", raising=False)

        settings = Settings.from_environment()

        assert settings.llm_fallback_base_url is None
        assert settings.llm_fallback_model is None
        assert settings.llm_response_cache is False
