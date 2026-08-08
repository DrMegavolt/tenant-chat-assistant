"""OBS-002: the metrics plane records what matters, and leaks nothing.

The acceptance criteria are pinned here: every critical action has success,
failure, and latency metrics; every metric records per turn under the request's
trace exemplar; error paths (model failure, retrieval outage, refusals) still
record; and — the property the whole plane exists for — no label of any metric
carries a session ID, user ID, free text, or PII. The label-safety assertions
mirror the redaction suite: they run real turns carrying deliberately hostile
content, then check the actual samples a scraper would see.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Sequence
from enum import StrEnum
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import Histogram
from prometheus_client.samples import Sample

from services.api.tests.conftest import (
    BOOKING_TENANT,
    OFFERED_SLOT,
    ScriptedModel,
    VisitorSession,
    booking_call,
)
from services.api.tests.test_citations import _chunk, _published_version, _UniformEmbedder
from tenantchat.api.app import create_app
from tenantchat.api.evidence import RetrievalEvidenceSource
from tenantchat.api.index_integrity import InMemoryIndexIntegrityStore
from tenantchat.api.metrics import METRIC_DEFINITIONS, METRICS
from tenantchat.api.retrieval import HybridRetrieverConfig
from tenantchat.api.search import IndexedChunk, InMemorySearchIndex, SearchIndex
from tenantchat.api.settings import Settings
from tenantchat.api.storage import MemoryObjectStore
from tenantchat.api.store import (
    InMemoryAuditStore,
    InMemoryBookingStore,
    InMemoryConsentStore,
    InMemoryConversationStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryKnowledgeStore,
    InMemoryLeadStore,
    InMemoryMembershipStore,
    InMemoryPrivacyStore,
)
from tenantchat.core.metrics import (
    BOUNDED_LABEL_VALUE_ENUMS,
    METRIC_CARDINALITY_CEILING,
    METRIC_LABELS,
    MetricKind,
    MetricLabelError,
    MetricLabelName,
    MetricName,
    TruncationKind,
    label_value_is_safe,
)
from tenantchat.core.resilience import CircuitState, Dependency, FailureKind
from tenantchat.core.routing import IntentName, RoutingOutcome, RoutingRule
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import AssembledPrompt, ModelResponse, ToolCall, ToolSpec
from tenantchat.orchestration.nodes import DispatchNode
from tenantchat.orchestration.prompts import DEFAULT_REGISTRY, DISPATCH_SYSTEM_TEMPLATE_ID
from tenantchat.orchestration.tools import ToolName

TEMPLATE_REF = "dispatch-system@4"


def _histogram_bucket_boundaries() -> frozenset[str]:
    """The ``le`` values the histograms in this inventory can emit.

    The adapter builds every histogram with the library's default buckets, so
    the closed set of boundary labels is the library constant itself.
    """
    return frozenset(str(boundary) for boundary in Histogram.DEFAULT_BUCKETS) | {"+Inf"}


def _reachable_vocabulary() -> frozenset[str]:
    """Every value any label of any metric may carry.

    The closed enums of the core metrics package, the routing and tool
    vocabularies the graph records, the versioned template refs the registry
    can produce, and the ``none`` sentinel for a clarification's missing
    intent. A value outside this set appearing on a real sample is a leak.
    """
    families: tuple[type[StrEnum], ...] = (
        *BOUNDED_LABEL_VALUE_ENUMS,
        IntentName,
        RoutingOutcome,
        RoutingRule,
        ToolName,
        Dependency,
        FailureKind,
        CircuitState,
        DispatchNode,
        TruncationKind,
    )
    vocabulary = {member.value for family in families for member in family.__members__.values()}
    vocabulary.update(
        template.ref for template in DEFAULT_REGISTRY.versions(DISPATCH_SYSTEM_TEMPLATE_ID)
    )
    vocabulary.add("none")
    return frozenset(vocabulary)


def _exemplar_trace_ids() -> list[str]:
    """The trace exemplar of every turn-latency observation.

    The exemplar rides the bucket sample the observation landed in, so it is
    read from whichever sample carries it. It is the drill-through handle: one
    trace ID per request, and a Grafana spike names the turn without carrying
    any of its content.
    """
    trace_ids: list[str] = []
    for sample in tenantchat_samples():
        if not sample.name.startswith("tenantchat_turn_latency_seconds"):
            continue
        if sample.exemplar is not None:
            trace_ids.append(sample.exemplar.labels["trace_id"])
    return trace_ids


def tenantchat_samples() -> list[Sample]:
    """Every sample of every metric this product owns, as the scraper sees it."""
    return [
        sample
        for metric in METRICS.registry.collect()
        for sample in metric.samples
        if sample.name.startswith("tenantchat_")
    ]


def sample_values() -> dict[tuple[str, frozenset[tuple[str, str]]], float]:
    """Sample values keyed by ``(series name, label pairs)``."""
    return {
        (sample.name, frozenset(sample.labels.items())): sample.value
        for sample in tenantchat_samples()
    }


@pytest.fixture(autouse=True)
def reset_metrics() -> Iterator[None]:
    """Drop every collector the adapter registered, before and after each test.

    The shared registry would otherwise accumulate series across tests, which
    makes an assertion like "exactly one turn latency sample" depend on which
    test ran first.
    """
    METRICS.reset()
    yield
    METRICS.reset()


def _metrics_settings() -> Settings:
    return Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=2048,
        docs_enabled=True,
        admin_gateway_token="gateway-token-for-tests",
        admin_csrf_secret="csrf-secret-for-tests",
        dev_auth=False,
    )


def _stores() -> dict[str, Any]:
    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    bookings = InMemoryBookingStore()
    leads = InMemoryLeadStore()
    handoffs = InMemoryHandoffStore()
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


def _open_session(client: TestClient) -> VisitorSession:
    """Open a consented conversation for the booking tenant."""
    opened = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
    assert opened.status_code == 201, opened.text
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
    assert granted.status_code == 200, granted.text
    return visitor


def _open_unconsented(client: TestClient, *, tenant_id: str) -> dict[str, str]:
    """Open a conversation without granting anything, for refusal paths."""
    opened = client.post("/api/chat/session", json={"tenant_id": tenant_id})
    assert opened.status_code == 201, opened.text
    return {"X-Visitor-Credential": opened.json()["credential"]}


class _FailingIndex:
    """A search index whose retrieval is down, for the outage path."""

    async def active_chunks(self, *, tenant_id: str) -> tuple[IndexedChunk, ...]:
        del tenant_id
        raise RuntimeError("elasticsearch is down")

    async def chunk_by_id(self, *, tenant_id: str, chunk_id: str) -> IndexedChunk | None:
        del tenant_id, chunk_id
        return None


class TestLabelContract:
    def test_the_adapter_defines_every_metric_of_the_inventory(self) -> None:
        """The adapter and the core inventory cannot drift apart.

        A metric in core but not in the adapter would never be scraped; one in
        the adapter but not core would be recordable without review.
        """
        assert set(METRIC_DEFINITIONS) == set(MetricName)
        kinds = {kind for kind, _ in METRIC_DEFINITIONS.values()}
        assert kinds == {MetricKind.COUNTER, MetricKind.HISTOGRAM, MetricKind.GAUGE}
        for name, (_, help_text) in METRIC_DEFINITIONS.items():
            assert help_text, name

    def test_free_text_label_values_are_refused_before_the_registry(self) -> None:
        """A label value a visitor message could produce raises instead of recording.

        The adapter is the last line before the registry; a call site that
        accidentally passes query text must fail loudly in that request, not
        start a new series in the scrape.
        """
        with pytest.raises(MetricLabelError):
            METRICS.observe(
                MetricName.TURN_OUTCOMES,
                1,
                labels={"outcome": "Book an appointment Tuesday"},
            )
        with pytest.raises(MetricLabelError):
            METRICS.observe(
                MetricName.TOOL_CALLS,
                1,
                labels={"tool": "check_service_area", "outcome": "12 Alder Court"},
            )

    def test_unknown_label_names_are_refused(self) -> None:
        """A dimension the contract does not name cannot be invented at a call site."""
        with pytest.raises(MetricLabelError):
            METRICS.observe(
                MetricName.LLM_CALLS,
                1,
                labels={"status": "ok", "template": TEMPLATE_REF, "model": "qwen"},
            )

    def test_a_metric_cannot_take_a_label_outside_its_own_contract(self) -> None:
        """Each metric's label set is closed, not just the union of all labels."""
        with pytest.raises(MetricLabelError):
            METRICS.observe(
                MetricName.LLM_TOKENS,
                1,
                labels={"kind": "prompt", "template": TEMPLATE_REF, "status": "ok"},
            )

    def test_every_contract_label_name_is_a_closed_enum_member(self) -> None:
        """Label names on every metric are bounded by the enum, not by prose."""
        for name, labels in METRIC_LABELS.items():
            assert all(isinstance(label, MetricLabelName) for label in labels), name

    def test_histograms_and_counters_render_the_expected_families(self) -> None:
        """The collector kind matches the name suffix convention.

        A counter named ``_seconds`` or a histogram named ``_total`` would
        confuse every dashboard and alert built on the convention.
        """
        for name, (kind, _) in METRIC_DEFINITIONS.items():
            if name.value.endswith("_total"):
                assert kind is MetricKind.COUNTER, name
            if name.value.endswith("_seconds"):
                assert kind is MetricKind.HISTOGRAM, name


class TestAnAnsweredTurn:
    def test_one_turn_records_the_whole_plane_with_its_trace(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """A general question records routing, LLM, latency, and outcome metrics.

        The latency sample carries the request's trace ID as its exemplar: a
        Grafana spike resolves to the one turn, and the content stays in the
        inference plane.
        """
        model.script = [
            ModelResponse(
                content="We are open until 7pm.",
                model_name="scripted",
                usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
            )
        ]
        visitor = _open_session(client)

        response = client.post(
            "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
        )

        assert response.status_code == 200
        trace = response.headers["x-trace-id"]
        values = sample_values()
        assert (
            values[
                (
                    "tenantchat_routing_decisions_total",
                    frozenset({("intent", "general"), ("outcome", "direct"), ("rule", "matched")}),
                )
            ]
            == 1
        )
        assert (
            values[
                (
                    "tenantchat_llm_calls_total",
                    frozenset({("status", "ok"), ("template", TEMPLATE_REF)}),
                )
            ]
            == 1
        )
        assert (
            values[
                (
                    "tenantchat_llm_tokens_total",
                    frozenset({("kind", "prompt"), ("template", TEMPLATE_REF)}),
                )
            ]
            == 5
        )
        assert (
            values[
                (
                    "tenantchat_llm_tokens_total",
                    frozenset({("kind", "completion"), ("template", TEMPLATE_REF)}),
                )
            ]
            == 3
        )
        assert values[("tenantchat_turn_outcomes_total", frozenset({("outcome", "answered")}))] == 1

        assert any(
            sample.name.startswith("tenantchat_router_confidence")
            and sample.name.endswith("_count")
            and sample.value >= 1.0
            for sample in tenantchat_samples()
        )
        assert any(
            sample.name == "tenantchat_token_cost_total"
            and frozenset(sample.labels.items())
            == frozenset({("kind", "prompt"), ("template", TEMPLATE_REF)})
            for sample in tenantchat_samples()
        )
        assert any(
            sample.name == "tenantchat_token_cost_total"
            and frozenset(sample.labels.items())
            == frozenset({("kind", "completion"), ("template", TEMPLATE_REF)})
            for sample in tenantchat_samples()
        )

        counts = [
            sample
            for sample in tenantchat_samples()
            if sample.name == "tenantchat_turn_latency_seconds_count"
        ]
        assert len(counts) == 1
        assert counts[0].value == 1
        assert _exemplar_trace_ids() == [trace]
        llm_counts = [
            sample
            for sample in tenantchat_samples()
            if sample.name == "tenantchat_llm_latency_seconds_count"
        ]
        assert len(llm_counts) == 1
        assert llm_counts[0].value == 1
        assert any(
            sample.name.startswith("tenantchat_llm_latency_seconds")
            and sample.exemplar is not None
            and sample.exemplar.labels == {"trace_id": trace}
            for sample in tenantchat_samples()
        )

    def test_the_latest_latency_exemplar_names_its_own_request(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """Per-turn correlation: the spike on a series resolves to one turn.

        Prometheus exemplars keep the most recent observation per bucket, so
        after two turns the latency series carries the second request's trace:
        a Grafana drill-through lands on the exact request the spike is about.
        """
        visitor = _open_session(client)
        first = client.post("/api/chat", json={"message": "Hours?"}, headers=visitor.headers)
        second = client.post(
            "/api/chat", json={"message": "Where are you?"}, headers=visitor.headers
        )

        assert first.status_code == second.status_code == 200
        # Exemplars persist per histogram bucket, so two observations may share
        # a bucket (one retained trace) or straddle buckets (two retained).
        # Either way the drill-through must resolve to requests this test made,
        # and the spike of the latest turn must name that turn.
        exemplars = _exemplar_trace_ids()
        assert set(exemplars) <= {first.headers["x-trace-id"], second.headers["x-trace-id"]}
        assert second.headers["x-trace-id"] in exemplars
        counts = [
            sample
            for sample in tenantchat_samples()
            if sample.name == "tenantchat_turn_latency_seconds_count"
        ]
        assert len(counts) == 1
        assert counts[0].value == 2

    def test_invalid_citations_are_counted_by_verdict(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The citation-validation failure rate comes from real verdicts.

        The answer cites one source that was in context and one that was not;
        the counter records exactly that split, and nothing about the sources
        themselves.
        """
        model.script = [
            ModelResponse(
                content="We are open. [evidence:chunk-a][evidence:chunk-fake]",
                model_name="scripted",
            )
        ]
        visitor = _open_session(client)
        response = client.post(
            "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
        )

        assert response.status_code == 200
        values = sample_values()
        assert (
            values[("tenantchat_citation_validation_total", frozenset({("verdict", "valid")}))] == 0
        )
        assert (
            values[("tenantchat_citation_validation_total", frozenset({("verdict", "invalid")}))]
            == 2
        )

    def test_per_node_latency_is_recorded_with_bounded_labels(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The `OBS-006` executed-graph capture feeds the node-latency histogram.

        One sample per node the graph actually ran, labelled with the closed
        node vocabulary and the bounded ok/error status — the cardinality test
        proves the series cannot grow.
        """
        model.script = [ModelResponse(content="We are open until 7pm.", model_name="scripted")]
        visitor = _open_session(client)
        response = client.post(
            "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
        )

        assert response.status_code == 200
        node_counts = {
            sample.labels["node"]: sample.value
            for sample in tenantchat_samples()
            if sample.name == "tenantchat_node_latency_seconds_count"
        }
        assert set(node_counts) == {"route", "model", "finalize"}
        assert all(value == 1 for value in node_counts.values())
        # Every latency sample carries the bounded ok status and a trace exemplar.
        latency = [
            sample
            for sample in tenantchat_samples()
            if sample.name == "tenantchat_node_latency_seconds_count"
        ]
        assert all(sample.labels["status"] == "ok" for sample in latency)
        assert _exemplar_trace_ids()


class TestErrorPaths:
    def test_a_model_failure_records_error_latency_and_a_handoff(self) -> None:
        """A provider outage is counted, not just logged.

        The error series, the error latency, the handed-off turn class, and
        the handoff business action all record even though the turn produced
        no answer.
        """

        class FailingModel:
            async def complete(
                self, prompt: AssembledPrompt, *, tools: Sequence[ToolSpec]
            ) -> ModelResponse:
                del prompt, tools
                raise RuntimeError("provider refused")

        app = create_app(
            _metrics_settings(),
            chat_model=FailingModel(),
            checkpointer=InMemorySaver(),
            **_stores(),
        )
        with TestClient(app, raise_server_exceptions=False) as failing:
            visitor = _open_session(failing)
            response = failing.post(
                "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
            )

        assert response.status_code == 200
        assert "passed it to the team" in response.json()["reply"]
        values = sample_values()
        assert (
            values[
                (
                    "tenantchat_llm_calls_total",
                    frozenset({("status", "error"), ("template", TEMPLATE_REF)}),
                )
            ]
            == 1
        )
        assert (
            values[("tenantchat_turn_outcomes_total", frozenset({("outcome", "handed_off")}))] == 1
        )
        assert (
            values[
                (
                    "tenantchat_business_actions_total",
                    frozenset({("operation", "handoff"), ("status", "committed")}),
                )
            ]
            == 1
        )
        assert any(
            sample.name.startswith("tenantchat_llm_latency_seconds")
            and sample.labels.get("status") == "error"
            for sample in tenantchat_samples()
        )

    def test_a_provider_timeout_records_the_timeout_status(self) -> None:
        """A provider that exceeds its deadline is a distinct status.

        Timeouts are the most actionable provider failure — distinguishing
        them from generic errors is what makes the error series useful.
        """

        class TimingOutModel:
            async def complete(
                self, prompt: AssembledPrompt, *, tools: Sequence[ToolSpec]
            ) -> ModelResponse:
                del prompt, tools
                raise httpx.TimeoutException("provider did not answer")

        app = create_app(
            _metrics_settings(),
            chat_model=TimingOutModel(),
            checkpointer=InMemorySaver(),
            **_stores(),
        )
        with TestClient(app, raise_server_exceptions=False) as timing_out:
            visitor = _open_session(timing_out)
            response = timing_out.post(
                "/api/chat", json={"message": "Hello"}, headers=visitor.headers
            )

        assert response.status_code == 200
        assert (
            "tenantchat_llm_calls_total",
            frozenset({("status", "timeout"), ("template", TEMPLATE_REF)}),
        ) in sample_values()
        assert any(
            sample.name.startswith("tenantchat_llm_latency_seconds")
            and sample.labels.get("status") == "timeout"
            for sample in tenantchat_samples()
        )

    def test_a_consent_refusal_records_the_refused_action(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """A refused business action is counted as refused, not as committed.

        A lead attempt without consent is a normal, expected outcome — the
        metric must show it as such, because a support funnel that only counts
        successes hides exactly the refusals a team acts on.
        """
        model.script = [
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
                            "summary": "Furnace is making noise",
                        },
                    ),
                ),
                model_name="scripted",
            ),
            ModelResponse(content="The team will call you back.", model_name="scripted"),
        ]
        headers = _open_unconsented(client, tenant_id="apex")
        response = client.post(
            "/api/chat", json={"message": "Call me back about HVAC"}, headers=headers
        )

        assert response.status_code == 200
        values = sample_values()
        assert (
            values[
                (
                    "tenantchat_tool_calls_total",
                    frozenset({("tool", "create_lead"), ("outcome", "refused")}),
                )
            ]
            == 1
        )
        assert (
            values[
                (
                    "tenantchat_business_actions_total",
                    frozenset({("operation", "lead"), ("status", "refused")}),
                )
            ]
            == 1
        )


class TestRetrieval:
    def _evidence_client(
        self,
        model: ScriptedModel,
        *,
        evidence: RetrievalEvidenceSource,
        knowledge: InMemoryKnowledgeStore,
        index: InMemorySearchIndex,
    ) -> TestClient:
        app = create_app(
            _metrics_settings(),
            knowledge_store=knowledge,
            generation_findings=InMemoryIndexIntegrityStore(),
            object_store=MemoryObjectStore(),
            search_index=index,
            evidence_source=evidence,
            chat_model=model,
            checkpointer=InMemorySaver(),
            **_stores(),
        )
        return TestClient(app, raise_server_exceptions=False)

    def test_insufficient_evidence_records_verdict_latency_and_abstention(
        self, model: ScriptedModel
    ) -> None:
        """The abstention boundary is observable: verdict, latency, and class.

        A question with no evidence never calls the model; the retrieval run
        records ``insufficient`` and the turn records ``abstained``.
        """
        knowledge = InMemoryKnowledgeStore()
        index = InMemorySearchIndex()
        evidence = RetrievalEvidenceSource(
            index=index,
            embedder=_UniformEmbedder(),
            knowledge=knowledge,
            config=HybridRetrieverConfig(),
            metrics=METRICS,
        )
        client = self._evidence_client(model, evidence=evidence, knowledge=knowledge, index=index)
        visitor = _open_session(client)

        response = client.post(
            "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
        )

        assert response.status_code == 200
        assert "I do not have approved material" in response.json()["reply"]
        assert model.calls == []
        values = sample_values()
        assert (
            values[
                (
                    "tenantchat_retrieval_runs_total",
                    frozenset({("status", "ok"), ("verdict", "insufficient")}),
                )
            ]
            == 1
        )
        assert (
            values[("tenantchat_turn_outcomes_total", frozenset({("outcome", "abstained")}))] == 1
        )
        assert any(
            sample.name.startswith("tenantchat_retrieval_latency_seconds")
            and sample.labels.get("status") == "ok"
            for sample in tenantchat_samples()
        )
        assert not any(
            sample.name == "tenantchat_llm_calls_total" for sample in tenantchat_samples()
        )

    def test_a_retrieval_outage_records_unavailable_and_still_abstains(
        self, model: ScriptedModel
    ) -> None:
        """An index failure is its own status: the error is countable.

        The graph abstains exactly as it does for insufficient evidence; the
        metric keeps the two causes distinguishable, because an outage and a
        quality gap need different pages on call.
        """
        knowledge = InMemoryKnowledgeStore()
        index = InMemorySearchIndex()
        evidence = RetrievalEvidenceSource(
            index=cast(SearchIndex, _FailingIndex()),
            embedder=_UniformEmbedder(),
            knowledge=knowledge,
            config=HybridRetrieverConfig(),
            metrics=METRICS,
        )
        client = self._evidence_client(model, evidence=evidence, knowledge=knowledge, index=index)
        visitor = _open_session(client)

        response = client.post(
            "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
        )

        assert response.status_code == 200
        values = sample_values()
        assert (
            values[
                (
                    "tenantchat_retrieval_runs_total",
                    frozenset({("status", "unavailable"), ("verdict", "insufficient")}),
                )
            ]
            == 1
        )
        assert (
            values[("tenantchat_turn_outcomes_total", frozenset({("outcome", "abstained")}))] == 1
        )

    def test_sufficient_evidence_records_the_verdict_and_candidates(
        self, model: ScriptedModel
    ) -> None:
        """A grounded answer records the sufficient verdict and candidate count."""
        knowledge = InMemoryKnowledgeStore()
        version = _published_version(knowledge, title="Clearview hours")
        index = InMemorySearchIndex()
        chunk = _chunk(
            "clearview-hvac-2",
            "Clearview is open daily from 7 AM to 7 PM. Hours of operation are "
            "seven days a week.",
            document_id=version.document_id,
            version_id=version.version_id,
        )
        asyncio.run(index.index_chunks((chunk,)))
        evidence = RetrievalEvidenceSource(
            index=index,
            embedder=_UniformEmbedder(),
            knowledge=knowledge,
            config=HybridRetrieverConfig(),
            metrics=METRICS,
        )
        model.script = [ModelResponse(content="We are open daily.", model_name="scripted")]
        client = self._evidence_client(model, evidence=evidence, knowledge=knowledge, index=index)
        visitor = _open_session(client)

        response = client.post(
            "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
        )

        assert response.status_code == 200
        values = sample_values()
        assert (
            values[
                (
                    "tenantchat_retrieval_runs_total",
                    frozenset({("status", "ok"), ("verdict", "sufficient")}),
                )
            ]
            == 1
        )
        assert values[("tenantchat_retrieval_candidates_total", frozenset())] == 1


class TestBusinessFunnel:
    def test_a_booking_records_tool_latency_and_the_exactly_once_count(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The full booking funnel: pause once, commit once, answer once.

        The paused turn and the resumed turn each record latency under their
        own trace; the booking action records exactly one committed count even
        though the confirmation passes through the idempotent service.
        """
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

        paused = client.post("/api/chat", json={"message": "Book HVAC"}, headers=visitor.headers)
        assert paused.status_code == 200
        resumed = client.post(
            "/api/chat/confirmation",
            json={"decision": "approved"},
            headers=visitor.headers,
        )
        assert resumed.status_code == 200

        values = sample_values()
        assert values[("tenantchat_turn_outcomes_total", frozenset({("outcome", "paused")}))] == 1
        assert values[("tenantchat_turn_outcomes_total", frozenset({("outcome", "answered")}))] == 1
        assert (
            values[
                (
                    "tenantchat_tool_calls_total",
                    frozenset({("tool", "book_appointment"), ("outcome", "succeeded")}),
                )
            ]
            == 1
        )
        assert (
            values[
                (
                    "tenantchat_business_actions_total",
                    frozenset({("operation", "booking"), ("status", "committed")}),
                )
            ]
            == 1
        )
        assert any(
            sample.name.startswith("tenantchat_tool_latency_seconds")
            for sample in tenantchat_samples()
        )
        assert any(
            sample.name.startswith("tenantchat_business_latency_seconds")
            for sample in tenantchat_samples()
        )
        exemplars = _exemplar_trace_ids()
        assert set(exemplars) <= {paused.headers["x-trace-id"], resumed.headers["x-trace-id"]}
        assert resumed.headers["x-trace-id"] in exemplars

    def test_a_clarification_is_its_own_outcome_and_routes_to_no_intent(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The router declining to guess is observable as a class.

        A clarification has no chosen intent, so the intent label records the
        ``none`` sentinel — one of the bounded vocabulary's few non-enum
        values, asserted here.
        """
        visitor = _open_session(client)
        response = client.post(
            "/api/chat",
            json={"message": "callback on tuesday for a repair"},
            headers=visitor.headers,
        )

        assert response.status_code == 200
        values = sample_values()
        assert (
            values[
                (
                    "tenantchat_routing_decisions_total",
                    frozenset({("intent", "none"), ("outcome", "clarify"), ("rule", "clarify")}),
                )
            ]
            == 1
        )
        assert (
            values[("tenantchat_turn_outcomes_total", frozenset({("outcome", "clarified")}))] == 1
        )


class TestTheOutcomePartition:
    def test_every_completed_turn_lands_in_exactly_one_outcome_class(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The outcome distribution sums to the turns the API completed.

        `OBS-002` calls this metric a quality distribution, which is only true
        if it partitions. Two terminal paths used to record nothing at all — a
        claim refusal and the server-written fallback reply — so the series
        under-counted turns and the gap looked like traffic that never arrived.
        Asserting the total rather than each class is what catches the next
        terminal path added without one.
        """
        model.script = [
            # A plain answer.
            ModelResponse(content="We are open until 7pm.", model_name="scripted"),
            # A fabricated price: refused whole by the `RAG-007` validator.
            ModelResponse(content="It is $4,999 and fully covered.", model_name="scripted"),
        ]
        visitor = _open_session(client)

        answered = client.post(
            "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
        )
        refused = client.post(
            "/api/chat",
            json={"message": "how much for a repair?"},
            headers=visitor.headers,
        )
        clarified = client.post(
            "/api/chat",
            json={"message": "callback on tuesday for a repair"},
            headers=visitor.headers,
        )

        assert [answered.status_code, refused.status_code, clarified.status_code] == [200] * 3
        assert "$4,999" not in refused.json()["reply"]

        values = sample_values()
        outcomes = {
            next(iter(labels))[1]: value
            for (name, labels), value in values.items()
            if name == "tenantchat_turn_outcomes_total"
        }
        assert outcomes.get("answer_refused") == 1
        assert sum(outcomes.values()) == 3, outcomes

    def test_the_refused_class_is_distinct_from_a_refused_tool_call(self) -> None:
        """``outcome`` is a shared label name, so its values must not collide.

        ``ToolOutcome.REFUSED`` already spends ``refused`` on the same label,
        and a query for refused turns that also matched refused tool calls
        would silently over-report the validator's work.
        """
        from tenantchat.core.metrics import ToolOutcome, TurnOutcome

        turn_values = {member.value for member in TurnOutcome}
        tool_values = {member.value for member in ToolOutcome}
        assert turn_values.isdisjoint(tool_values)
        assert TurnOutcome.ANSWER_REFUSED.value == "answer_refused"


class TestLabelSafety:
    def test_no_label_of_a_real_turn_carries_pii_or_free_text(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The leak test: hostile content in a real turn reaches no label.

        Mirrors the redaction suite: the message, the tool arguments, and the
        model output all carry contact details and free text, and every sample
        a scraper would read still restricts its label values to the bounded
        vocabulary. A value outside the vocabulary would fail here even if it
        passes the charset — a bare phone number would, and so would a session
        ID.
        """
        model.script = [
            ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        call_id="call-book-pii",
                        name="book_appointment",
                        arguments={
                            "service": "HVAC",
                            "slot": OFFERED_SLOT,
                            "customer_name": "Dana PII-Marker Ruiz",
                            "customer_phone_or_email": "555-222-1919",
                            "address": "12 PII-Marker Lane, Portland, OR 97205",
                        },
                    ),
                ),
                model_name="scripted",
            ),
            ModelResponse(
                content="Booked for dana.pii@example.com at 555-222-1919.",
                model_name="scripted",
            ),
        ]
        visitor = _open_session(client)
        send = client.post(
            "/api/chat",
            json={"message": "Book HVAC for dana.pii@example.com"},
            headers=visitor.headers,
        )
        assert send.status_code == 200
        resume = client.post(
            "/api/chat/confirmation",
            json={"decision": "approved"},
            headers=visitor.headers,
        )
        assert resume.status_code == 200

        vocabulary = _reachable_vocabulary()
        assert tenantchat_samples(), "the turn recorded no metrics at all"
        bucket_boundaries = _histogram_bucket_boundaries()
        for sample in tenantchat_samples():
            for label_name, label_value in sample.labels.items():
                if label_name == "le":
                    # The bucket-boundary label is owned by the histogram
                    # collector, not by any call site: its values are the
                    # bounded bucket definitions the metric was built with.
                    assert (
                        label_value in bucket_boundaries
                    ), f"{sample.name} carries an undeclared bucket boundary {label_value!r}"
                    continue
                assert label_value_is_safe(
                    label_value
                ), f"{sample.name} label {label_name} carries unsafe value {label_value!r}"
                assert label_value in vocabulary, (
                    f"{sample.name} label {label_name} carries out-of-vocabulary "
                    f"value {label_value!r}"
                )

    def test_the_reachable_label_vocabulary_stays_under_the_ceiling(self) -> None:
        """A new label value cannot quietly multiply series.

        The whole vocabulary any label may carry — enums, the routing and tool
        sets, the registered template refs, the ``none`` sentinel — stays under
        the documented ceiling. Raising the ceiling is a deliberate, reviewed
        decision, not a side effect of adding an enum member.
        """
        vocabulary = _reachable_vocabulary()
        assert all(label_value_is_safe(value) for value in vocabulary)
        assert len(vocabulary) <= METRIC_CARDINALITY_CEILING


class TestNewMetrics:
    def test_router_confidence_records_with_no_call_site_labels(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The confidence histogram records per turn with no labels.

        Confidence is a pure distribution — no outcome, intent, or template
        label rides alongside it. The Prometheus `le` bucket labels are
        collector-owned, not call-site-controlled.
        """
        model.script = [ModelResponse(content="We are open until 7pm.", model_name="scripted")]
        visitor = _open_session(client)

        response = client.post(
            "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
        )

        assert response.status_code == 200
        confidence_samples = [
            sample
            for sample in tenantchat_samples()
            if sample.name.startswith("tenantchat_router_confidence")
        ]
        assert confidence_samples, "router confidence recorded no samples"
        non_le_labels = {
            label_name
            for sample in confidence_samples
            for label_name in sample.labels
            if label_name != "le"
        }
        assert not non_le_labels, f"router confidence carries call-site labels: {non_le_labels}"

    def test_router_confidence_is_not_emitted_as_a_label(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The raw confidence value is in the histogram, not a label.

        A label would carry the raw score and create a series per distinct
        confidence value, which is unbounded. The histogram buckets the value
        without exposing it as a label.
        """
        model.script = [ModelResponse(content="We are open.", model_name="scripted")]
        visitor = _open_session(client)

        client.post("/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers)

        vocabulary = _reachable_vocabulary()
        for sample in tenantchat_samples():
            if sample.name.startswith("tenantchat_router_confidence"):
                for label_name, label_value in sample.labels.items():
                    if label_name == "le":
                        continue
                    assert label_value in vocabulary, (
                        f"router confidence label {label_name} carries "
                        f"out-of-vocabulary value {label_value!r}"
                    )

    def test_token_cost_records_prompt_and_completion_kinds(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The cost counter mirrors the token metric: prompt and completion kinds.

        The template label matches the assembled prompt's registry reference.
        """
        model.script = [
            ModelResponse(
                content="We are open until 7pm.",
                model_name="scripted",
                usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )
        ]
        visitor = _open_session(client)

        response = client.post(
            "/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers
        )

        assert response.status_code == 200
        values = sample_values()
        assert (
            "tenantchat_token_cost_total",
            frozenset({("kind", "prompt"), ("template", TEMPLATE_REF)}),
        ) in values, "prompt cost not recorded"
        assert (
            "tenantchat_token_cost_total",
            frozenset({("kind", "completion"), ("template", TEMPLATE_REF)}),
        ) in values, "completion cost not recorded"
        prompt_cost = values[
            (
                "tenantchat_token_cost_total",
                frozenset({("kind", "prompt"), ("template", TEMPLATE_REF)}),
            )
        ]
        completion_cost = values[
            (
                "tenantchat_token_cost_total",
                frozenset({("kind", "completion"), ("template", TEMPLATE_REF)}),
            )
        ]
        assert prompt_cost > 0, "prompt cost is zero"
        assert completion_cost > 0, "completion cost is zero"

    def test_token_cost_labels_are_bounded(self, client: TestClient, model: ScriptedModel) -> None:
        """The cost counter's label values stay in the closed vocabulary."""
        model.script = [
            ModelResponse(
                content="Hi.",
                model_name="scripted",
                usage={"prompt_tokens": 3, "completion_tokens": 1},
            )
        ]
        visitor = _open_session(client)

        client.post("/api/chat", json={"message": "Hello"}, headers=visitor.headers)

        vocabulary = _reachable_vocabulary()
        for sample in tenantchat_samples():
            if sample.name == "tenantchat_token_cost_total":
                for label_name, label_value in sample.labels.items():
                    assert label_value in vocabulary, (
                        f"token cost label {label_name} carries "
                        f"out-of-vocabulary value {label_value!r}"
                    )

    def test_context_truncation_labels_are_bounded(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """The truncation counter uses the closed `TruncationKind` enum.

        Even when no truncation occurred, the contract that the `kind` label
        only carries ``history`` or ``evidence`` must hold.
        """
        model.script = [ModelResponse(content="We are open.", model_name="scripted")]
        visitor = _open_session(client)

        client.post("/api/chat", json={"message": "What are your hours?"}, headers=visitor.headers)

        vocabulary = _reachable_vocabulary()
        for sample in tenantchat_samples():
            if sample.name == "tenantchat_context_truncation_total":
                for label_name, label_value in sample.labels.items():
                    assert label_value in vocabulary, (
                        f"context truncation label {label_name} carries "
                        f"out-of-vocabulary value {label_value!r}"
                    )


class TestScrapeSurface:
    def test_the_metrics_endpoint_serves_the_plane(
        self, client: TestClient, model: ScriptedModel
    ) -> None:
        """A scraper gets the exposition format over the scrape endpoint.

        The endpoint is an operations surface: it is deliberately absent from
        the OpenAPI document, exactly like the side services' ``/metrics``.
        """
        visitor = _open_session(client)
        turn = client.post("/api/chat", json={"message": "Hours?"}, headers=visitor.headers)
        assert turn.status_code == 200

        response = client.get("/metrics", headers={"Accept": "application/openmetrics-text"})

        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/openmetrics-text")
        assert "tenantchat_llm_calls_total" in response.text
        assert "tenantchat_turn_latency_seconds" in response.text
        assert 'trace_id="' in response.text
        classic = client.get("/metrics", headers={"Accept": "text/plain"})
        assert classic.status_code == 200
        assert classic.headers["content-type"].startswith("text/plain")
        # The scrape surface carries no content plane: no request body text,
        # no contact detail, no session ID appears anywhere in the exposition.
        for forbidden in ("Hours?", "session", "555-222-1919"):
            assert forbidden not in response.text
