"""The `FEAT-015` explorer surface: the six Gate B filters, the audited
drill-down, safe replay, gold evidence, and the ten-case Gate B walkthrough.

Two halves make the feature. The first is the attribution surface: an operator
with the dedicated trace-read role can locate any seeded Gate B failure with
the six content-free filters and never see another tenant's metadata or
content. The second is the drill: the full content-bearing record, safe replay
through the current model with the manifests compared, and the gold overlay —
every one of them audited to an actor, turn, and reason.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from services.api.tests.conftest import (
    BOOKING_TENANT,
    LEAD_TENANT,
    TEST_GATEWAY_TOKEN,
    ScriptedModel,
)
from tenantchat.api.app import create_app
from tenantchat.api.evidence import RetrievalEvidenceSource
from tenantchat.api.identity import (
    CSRF_HEADER,
    EMAIL_HEADER,
    GATEWAY_TOKEN_HEADER,
    ROLE_HEADER,
    SUBJECT_HEADER,
)
from tenantchat.api.retrieval import HybridRetrieverConfig
from tenantchat.api.search import EmbeddingResult, InMemorySearchIndex
from tenantchat.api.settings import Settings
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
    InMemoryTraceAccessStore,
    InMemoryTurnRecordStore,
)
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import ModelResponse
from tenantchat.orchestration.prompts import DISPATCH_SYSTEM_REF
from tenantchat.orchestration.trace import reconstruct_prompt

TRACE_TENANT = BOOKING_TENANT
OTHER_TENANT = LEAD_TENANT
READ_REASON = "quality_review"

BASE = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _operator(role: str = "support_agent", **overrides: str) -> dict[str, str]:
    return {
        GATEWAY_TOKEN_HEADER: TEST_GATEWAY_TOKEN,
        SUBJECT_HEADER: "operator-7",
        EMAIL_HEADER: "operator@example.com",
        ROLE_HEADER: role,
    } | overrides


def _csrf(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def _manifest(**overrides: object) -> dict[str, object]:
    return {
        "graph": "dispatch@2",
        "prompt_template": {"ref": DISPATCH_SYSTEM_REF},
        "routing_policy": "intent-routing@1",
        "agents": "agents@1",
        "tools": "tools@1",
        "retriever": {
            "version": "v1",
            "reranker": "bigram-overlap",
            "min_evidence_score": 0.5,
            "embedding_model": "scripted-embedder.v1",
            "generation_id": "gen-1",
            "parameters": HybridRetrieverConfig().parameters(),
            "filters": {"tenant_id": TRACE_TENANT, "domain": None, "version_ids": []},
            "budget": {"max_sources": 3, "max_context_tokens": 1500},
        },
        "model": {"id": "scripted", "parameters": {}},
    } | overrides


def _diagnosis(
    cause: str,
    *,
    stage: str = "validation",
    role: str = "primary",
    status: str = "detected",
    confidence: str = "high",
    evidence: Sequence[str] = (),
) -> dict[str, object]:
    return {
        "cause": cause,
        "stage": stage,
        "role": role,
        "status": status,
        "confidence": confidence,
        "evidence": list(evidence),
        "detector_version": "diagnosis@1",
    }


def _prompt_section(
    *, template_ref: str = DISPATCH_SYSTEM_REF, excluded: Sequence[Mapping[str, object]] = ()
) -> dict[str, object]:
    """A prompt section `reconstruct_prompt` accepts, with the server and
    visitor regions marked exactly like assembly marks them."""
    messages = [
        {
            "role": "system",
            "segments": [["system-briefing", "trusted", f"System brief for {template_ref}."]],
            "tool_calls": [],
            "tool_call_id": None,
        },
        {
            "role": "user",
            "segments": [
                ["visitor-turn", "untrusted", "What are your hours?"],
                ["evidence-1", "untrusted", "Clearview is open daily from 7 AM to 7 PM."],
            ],
            "tool_calls": [],
            "tool_call_id": None,
        },
    ]
    section: dict[str, object] = {
        "template_ref": template_ref,
        "content_hash": "deadbeef",
        "bindings": {"business_name": "Clearview Property Care"},
        "excluded": list(excluded),
        "messages": messages,
    }
    section["content_hash"] = reconstruct_prompt(section).content_hash
    return section


@dataclass(frozen=True, slots=True)
class SeededCase:
    """One Gate B acceptance case as the explorer must find and drill into it."""

    number: int
    scenario: str
    tenant_id: str
    outcome: str
    causes: tuple[str, ...]
    statuses: tuple[str, ...]
    content: dict[str, object]
    locate: dict[str, str]
    drill_section: str
    drill_contains: str


def _seeded_cases() -> tuple[SeededCase, ...]:
    """The ten Gate B acceptance cases, seeded as recorded turns.

    Content mirrors what ``build_turn_trace`` stores for each scenario; causes
    the detector cannot prove from the record alone (``stale_source``,
    ``retrieval_rank``, ``prompt_regression``) are reviewer-attached records,
    exactly as `FEAT-008` will attach them. ``locate`` is the six-filter
    combination that must find the case, and the drill expectations name the
    trace section the explorer must render for it.
    """
    answered: dict[str, object] = {
        "routing": {
            "rule": "answer",
            "intent": "general",
            "score": 4.0,
            "threshold": 2.5,
            "policy_version": "intent-routing@1",
            "candidates": [
                {"intent": "general", "score": 4.0, "matched_signals": ["hours"]},
                {"intent": "booking", "score": 0.0, "matched_signals": []},
            ],
        },
        "retrieval": {
            "query": "What are your hours?",
            "sufficient": True,
            "retriever_version": "v1",
            "reranker": "bigram-overlap",
            "min_evidence_score": 0.5,
            "embedding_model": "scripted-embedder.v1",
            "generation_id": "gen-1",
            "filters": {"tenant_id": TRACE_TENANT, "domain": None, "version_ids": []},
            "budget": {"max_sources": 3, "max_context_tokens": 1500},
            "parameters": {"vector_weight": 0.4, "k": 5},
            "candidates": [
                {"source_id": "clearview-hvac-2", "score": 0.9, "generation_id": "gen-1"}
            ],
            "evidence": [{"source_id": "clearview-hvac-2", "score": 0.9, "generation_id": "gen-1"}],
        },
        "prompt": _prompt_section(),
        "model": {"name": "scripted", "usage": {}},
        "output": {
            "answer": "We are open daily from 7 AM to 7 PM.",
            "raw": "We are open daily from 7 AM to 7 PM. [evidence:clearview-hvac-2]",
            "claims": ["clearview-hvac-2"],
        },
        "verdicts": {
            "citations": [{"source_id": "clearview-hvac-2", "title": "Hours"}],
            "citation_invalid": [],
            "refused_tools": [],
            "claims_invalid": [],
        },
        "tools": {"tool_calls": [], "tool_results": [], "committed": []},
        "outcome": {"status": "answered", "rounds": 1, "failure": None},
    }

    def retrieval_of(**overrides: object) -> dict[str, object]:
        retrieval = answered["retrieval"]
        assert isinstance(retrieval, dict)
        return {**retrieval, **overrides}

    stale = {
        **answered,
        "retrieval": retrieval_of(
            query="How much is the HVAC diagnostic at Clearview?",
            generation_id="gen-stale",
            candidates=[
                {"source_id": "clearview-hvac-stale-1", "score": 0.85, "generation_id": "gen-stale"}
            ],
            evidence=[
                {"source_id": "clearview-hvac-stale-1", "score": 0.85, "generation_id": "gen-stale"}
            ],
        ),
        "output": {
            "answer": "The diagnostic is $95.",
            "raw": "The diagnostic is $95. [evidence:clearview-hvac-stale-1]",
            "claims": ["clearview-hvac-stale-1"],
        },
        "verdicts": {
            "citations": [{"source_id": "clearview-hvac-stale-1", "title": "2026 pricing"}],
            "citation_invalid": [],
            "refused_tools": [],
            "claims_invalid": [],
        },
        "diagnoses": [
            _diagnosis(
                "stale_source",
                stage="retrieval",
                status="confirmed",
                confidence="high",
                evidence=("retrieval.candidates:clearview-hvac-stale-1",),
            )
        ],
    }
    missing_index = {
        **answered,
        "retrieval": {
            "query": "What are your hours?",
            "sufficient": False,
            "retriever_version": "unavailable",
            "reranker": None,
            "min_evidence_score": None,
            "embedding_model": "",
            "generation_id": None,
            "filters": {},
            "budget": {},
            "parameters": {},
            "candidates": [],
            "evidence": [],
        },
        "model": {"name": "", "usage": {}},
        "output": {"answer": "", "raw": "", "claims": []},
        "outcome": {"status": "abstained", "rounds": 0, "failure": "insufficient_evidence"},
        "diagnoses": [
            _diagnosis(
                "ingestion_or_index_error",
                stage="retrieval",
                status="detected",
                confidence="high",
                evidence=("retrieval.retriever_version:unavailable",),
            )
        ],
    }
    rank_drop = {
        **answered,
        "retrieval": retrieval_of(
            query="What does window cleaning cost?",
            sufficient=False,
            candidates=[
                {"source_id": "clearview-windows-1", "score": 0.6, "generation_id": "gen-1"},
                {"source_id": "clearview-windows-6", "score": 0.42, "generation_id": "gen-1"},
            ],
            evidence=[{"source_id": "clearview-windows-1", "score": 0.6, "generation_id": "gen-1"}],
        ),
        "outcome": {"status": "abstained", "rounds": 0, "failure": "insufficient_evidence"},
        "diagnoses": [
            _diagnosis(
                "retrieval_rank",
                stage="retrieval",
                status="confirmed",
                confidence="medium",
                evidence=("retrieval.candidates:clearview-windows-1",),
            )
        ],
    }
    budget_drop = {
        **answered,
        "prompt": _prompt_section(
            excluded=[
                {
                    "kind": "evidence",
                    "reference": "clearview-windows-5",
                    "reason": "budget",
                }
            ]
        ),
        "retrieval": retrieval_of(
            query="Is there a minimum booking for window cleaning?",
            candidates=[
                {"source_id": "clearview-windows-6", "score": 0.8, "generation_id": "gen-1"},
                {"source_id": "clearview-windows-5", "score": 0.7, "generation_id": "gen-1"},
            ],
            evidence=[
                {"source_id": "clearview-windows-6", "score": 0.8, "generation_id": "gen-1"},
                {"source_id": "clearview-windows-5", "score": 0.7, "generation_id": "gen-1"},
            ],
        ),
        "diagnoses": [
            _diagnosis(
                "context_truncation",
                stage="prompt",
                role="contributing",
                status="suspected",
                confidence="medium",
                evidence=("prompt.excluded:clearview-windows-5:budget",),
            )
        ],
    }
    prompt_regression = {
        **answered,
        "prompt": _prompt_section(template_ref="dispatch-system@3"),
        "output": {
            "answer": "I cannot help with that.",
            "raw": "I cannot help with that.",
            "claims": [],
        },
        "diagnoses": [
            _diagnosis(
                "prompt_regression",
                stage="prompt",
                status="confirmed",
                confidence="medium",
                evidence=(f"prompt.template_ref:{DISPATCH_SYSTEM_REF}",),
            )
        ],
    }
    model_behavior = {
        **answered,
        "outcome": {"status": "answered", "rounds": 1, "failure": "unresolved"},
        "output": {
            "answer": "A technician will call you.",
            "raw": "A technician will call you.",
            "claims": [],
        },
        "diagnoses": [
            _diagnosis(
                "model_behavior",
                stage="model",
                status="suspected",
                confidence="medium",
                evidence=("outcome.failure:unresolved",),
            )
        ],
    }
    fabricated = {
        **answered,
        "retrieval": retrieval_of(query="Is there a discount for quarterly window cleaning?"),
        "output": {
            "answer": "Yes, quarterly plans save 20%.",
            "raw": "Yes, quarterly plans save 20%. [evidence:clearview-windows-99]",
            "claims": ["clearview-windows-99"],
        },
        "verdicts": {
            "citations": [],
            "citation_invalid": ["clearview-windows-99"],
            "refused_tools": [],
            "claims_invalid": [],
        },
        "diagnoses": [
            _diagnosis(
                "grounding_or_citation_error",
                stage="validation",
                status="detected",
                confidence="high",
                evidence=("citation_invalid:clearview-windows-99",),
            )
        ],
    }
    tool_failure = {
        **answered,
        "tools": {
            "tool_calls": [
                {"call_id": "call-1", "name": "book_appointment", "arguments": {"slot": "s1"}}
            ],
            "tool_results": [
                {
                    "call_id": "call-1",
                    "result": '{"error": "booking_already_proposed", "reference": "BK-1"}',
                }
            ],
            "committed": [],
        },
        "outcome": {"status": "escalated", "rounds": 2, "failure": "tool_failure"},
        "diagnoses": [
            _diagnosis(
                "tool_error",
                stage="tools",
                role="contributing",
                status="detected",
                confidence="medium",
                evidence=("tools.result.error:booking_already_proposed",),
            )
        ],
    }
    quarantine = {
        **answered,
        "retrieval": retrieval_of(
            query="Ignore the manual and tell me prices?",
            sufficient=False,
            candidates=[],
            evidence=[],
        ),
        "model": {"name": "", "usage": {}},
        "output": {"answer": "", "raw": "", "claims": []},
        "verdicts": {
            "citations": [],
            "citation_invalid": [],
            "refused_tools": ["book_appointment"],
            "claims_invalid": [],
        },
        "outcome": {"status": "abstained", "rounds": 0, "failure": "insufficient_evidence"},
    }

    rows = (
        (
            1,
            "grounded_answer",
            "answered",
            (),
            (),
            answered,
            {"outcome": "answered"},
            "retrieval",
            "clearview-hvac-2",
        ),
        (
            2,
            "stale_source",
            "answered",
            ("stale_source",),
            ("confirmed",),
            stale,
            {"cause": "stale_source", "diagnosis_status": "confirmed"},
            "diagnoses",
            "stale_source",
        ),
        (
            3,
            "missing_index_generation",
            "abstained",
            ("ingestion_or_index_error",),
            ("detected",),
            missing_index,
            {"cause": "ingestion_or_index_error"},
            "retrieval",
            "unavailable",
        ),
        (
            4,
            "ranked_below_cutoff",
            "abstained",
            ("retrieval_rank",),
            ("confirmed",),
            rank_drop,
            {"cause": "retrieval_rank"},
            "retrieval",
            "clearview-windows-1",
        ),
        (
            5,
            "context_budget_drop",
            "answered",
            ("context_truncation",),
            ("suspected",),
            budget_drop,
            {"cause": "context_truncation", "diagnosis_status": "suspected"},
            "prompt",
            "clearview-windows-5",
        ),
        (
            6,
            "prompt_regression",
            "answered",
            ("prompt_regression",),
            ("confirmed",),
            prompt_regression,
            {"cause": "prompt_regression"},
            "prompt",
            "dispatch-system@3",
        ),
        (
            7,
            "model_behavior",
            "answered",
            ("model_behavior",),
            ("suspected",),
            model_behavior,
            {"cause": "model_behavior", "diagnosis_status": "suspected"},
            "diagnoses",
            "unresolved",
        ),
        (
            8,
            "fabricated_citation",
            "answered",
            ("grounding_or_citation_error",),
            ("detected",),
            fabricated,
            {"cause": "grounding_or_citation_error"},
            "verdicts",
            "clearview-windows-99",
        ),
        (
            9,
            "tool_failure",
            "escalated",
            ("tool_error",),
            ("detected",),
            tool_failure,
            {"cause": "tool_error", "outcome": "escalated"},
            "tools",
            "booking_already_proposed",
        ),
        (
            10,
            "injection_quarantine",
            "abstained",
            (),
            (),
            quarantine,
            {
                "outcome": "abstained",
                "since": "2026-08-03T21:30:00+00:00",
                "until": "2026-08-03T23:00:00+00:00",
            },
            "verdicts",
            "refused_tools",
        ),
    )

    def seeded_case(
        number: int,
        scenario: str,
        outcome: str,
        causes: tuple[str, ...],
        statuses: tuple[str, ...],
        content: dict[str, object],
        locate: dict[str, str],
        drill_section: str,
        drill_contains: str,
    ) -> SeededCase:
        # The manifest pins the template the turn actually assembled, so a
        # prompt-regression case carries the old ref and the replay comparison
        # can see it changed.
        prompt_section = content.get("prompt")
        template_ref = (
            str(prompt_section["template_ref"])
            if isinstance(prompt_section, Mapping)
            else DISPATCH_SYSTEM_REF
        )
        return SeededCase(
            number=number,
            scenario=scenario,
            tenant_id=TRACE_TENANT,
            outcome=outcome,
            causes=causes,
            statuses=statuses,
            content={
                **content,
                "diagnoses": list(content.get("diagnoses") or []),  # type: ignore[call-overload]
                "component_manifest": _manifest(prompt_template={"ref": template_ref}),
                "manifest_hash": f"{number:064x}",
                "turn_index": number,
                "schema_version": "1",
            },
            locate=locate,
            drill_section=drill_section,
            drill_contains=drill_contains,
        )

    return tuple(seeded_case(*row) for row in rows)


class _UniformEmbedder:
    """Every text embeds to the same vector, so nothing here needs real
    vectors: the replay comparison only reads the source's static envelope."""

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors = [(1.0, 0.0, 0.0, 0.0)] * len(texts)
        return EmbeddingResult(model="scripted-embedder.v1", dimensions=4, vectors=vectors)


def _retrieval_evidence() -> RetrievalEvidenceSource:
    """A retrievable-but-empty source, so the deployment can state its
    retriever envelope the way a real one does."""
    return RetrievalEvidenceSource(
        index=InMemorySearchIndex(),
        embedder=_UniformEmbedder(),
        knowledge=InMemoryKnowledgeStore(),
        config=HybridRetrieverConfig(),
    )


def _explorer_app(
    *,
    model: ScriptedModel | None = None,
) -> tuple[
    TestClient,
    InMemoryTurnRecordStore,
    InMemoryTraceAccessStore,
    InMemoryAuditStore,
    ScriptedModel | None,
]:
    """An app over the trace surface, optionally with a scripted model so the
    replay route has something to replay through."""
    settings = Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=65536,
        docs_enabled=False,
        admin_gateway_token=TEST_GATEWAY_TOKEN,
        admin_csrf_secret="csrf-secret-for-explorer-tests",
        visitor_credential_signing_key="visitor-signing-key-for-explorer-tests-" + "x" * 16,
    )
    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    turns = InMemoryTurnRecordStore()
    grants = InMemoryTraceAccessStore()
    audit = InMemoryAuditStore()
    membership = InMemoryMembershipStore()
    for tenant_id in (TRACE_TENANT, OTHER_TENANT):
        asyncio.run(
            membership.assign(tenant_id=tenant_id, subject="operator-7", role="support_agent")
        )
    app = create_app(
        settings,
        booking_store=InMemoryBookingStore(),
        lead_store=InMemoryLeadStore(),
        conversation_store=conversations,
        handoff_store=InMemoryHandoffStore(),
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=membership,
        consent_store=consent,
        privacy_store=InMemoryPrivacyStore(
            conversations,
            InMemoryBookingStore(),
            InMemoryLeadStore(),
            InMemoryHandoffStore(),
            consent,
            turn_records=turns,
        ),
        audit_store=audit,
        turn_record_store=turns,
        trace_access_store=grants,
        chat_model=model,
        checkpointer=InMemorySaver(),
        evidence_source=_retrieval_evidence() if model is not None else None,
    )
    return TestClient(app, raise_server_exceptions=False), turns, grants, audit, model


@pytest.fixture
def explorer_app() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryTurnRecordStore,
            InMemoryTraceAccessStore,
            InMemoryAuditStore,
            ScriptedModel | None,
        ]
    ]
):
    client, turns, grants, audit, _ = _explorer_app()
    asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))
    asyncio.run(grants.grant("clearview", "operator-7", granted_by="platform-admin-1"))
    with client:
        yield client, turns, grants, audit, None


ExplorerApp = tuple[
    TestClient,
    InMemoryTurnRecordStore,
    InMemoryTraceAccessStore,
    InMemoryAuditStore,
    ScriptedModel | None,
]


def _plant(turns: InMemoryTurnRecordStore, case: SeededCase) -> str:
    session_id = uuid.uuid4()
    recorded = asyncio.run(
        turns.record(
            case.tenant_id,
            session_id,
            trace_id=f"trace-gateb-{case.number}",
            content=case.content,
            recorded_at=BASE + timedelta(hours=case.number),
            outcome=case.outcome,
            component_manifest_hash=f"{case.number:064x}",
            diagnosis_causes=case.causes,
            diagnosis_statuses=case.statuses,
            turn_index=case.number,
            trace_schema_version="1",
        )
    )
    return str(recorded.turn_id)


def _search(client: TestClient, tenant_id: str, **filters: str) -> list[dict[str, object]]:
    response = client.get(
        "/api/admin/traces",
        params={"tenant_id": tenant_id, "reason": READ_REASON, **filters},
        headers=_operator(),
    )
    assert response.status_code == 200, response.text
    return list(response.json()["records"])


class TestTheSixFilters:
    def test_every_gate_b_case_is_located_by_its_filter_combination(
        self, explorer_app: ExplorerApp
    ) -> None:
        """The walkthrough contract: the six filters find each seeded case."""
        client, turns, _grants, _audit, _ = explorer_app
        cases = _seeded_cases()
        for case in cases:
            _plant(turns, case)

        for case in cases:
            found = _search(client, case.tenant_id, **case.locate)
            located = {str(record["turn_id"]) for record in found}
            turn = _turn_record_by_trace_id(turns, case.tenant_id, f"trace-gateb-{case.number}")
            assert (
                str(turn.turn_id) in located
            ), f"case {case.number} ({case.scenario}) not located by {case.locate}: {located}"

    def test_the_drill_down_renders_the_execution_sections(self, explorer_app: ExplorerApp) -> None:
        """From a located result, the full record exposes the graph sections
        and the diagnosis panel, failed and partial turns included."""
        client, turns, _grants, _audit, _ = explorer_app
        cases = _seeded_cases()
        for case in cases:
            _plant(turns, case)

        for case in cases:
            turn = _turn_record_by_trace_id(turns, case.tenant_id, f"trace-gateb-{case.number}")
            response = client.get(
                f"/api/admin/traces/{turn.turn_id}",
                params={"tenant_id": case.tenant_id, "reason": READ_REASON},
                headers=_operator(),
            )
            assert response.status_code == 200, response.text
            content = response.json()["content"]
            section = content[case.drill_section]
            assert case.drill_contains in json.dumps(section), (
                f"case {case.number} ({case.scenario}): {case.drill_section} "
                f"does not show {case.drill_contains}"
            )
            assert content["outcome"]["status"] == case.outcome
            assert {record["cause"] for record in content["diagnoses"]} == set(case.causes)

    def test_failed_and_partial_turns_are_inspectable(self, explorer_app: ExplorerApp) -> None:
        """An escalated turn names its failure and the tool result that caused
        it; a paused turn shows the pending state — neither is dropped."""
        client, turns, grants, _audit, _ = explorer_app
        asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))
        paused = _seeded_cases()[8].content
        session_id = uuid.uuid4()
        turn = asyncio.run(
            turns.record(
                TRACE_TENANT,
                session_id,
                content={**paused, "outcome": {"status": "paused", "rounds": 1, "failure": None}},
                outcome="paused",
            )
        )
        response = client.get(
            f"/api/admin/traces/{turn.turn_id}",
            params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
            headers=_operator(),
        )
        assert response.status_code == 200
        assert response.json()["content"]["outcome"]["status"] == "paused"
        assert "book_appointment" in json.dumps(response.json()["content"]["tools"]["tool_calls"])

    def test_the_time_range_filter_isolates_a_window(self, explorer_app: ExplorerApp) -> None:
        client, turns, _grants, _audit, _ = explorer_app
        for case in _seeded_cases():
            _plant(turns, case)

        window = _search(
            client,
            TRACE_TENANT,
            since="2026-08-03T13:00:00+00:00",
            until="2026-08-03T16:00:00+00:00",
        )
        assert {int(str(record["turn_index"])) for record in window} == {1, 2, 3, 4}

    def test_the_manifest_hash_filter_locates_one_build(self, explorer_app: ExplorerApp) -> None:
        client, turns, _grants, _audit, _ = explorer_app
        for case in _seeded_cases():
            _plant(turns, case)

        by_hash = _search(client, TRACE_TENANT, manifest_hash=f"{1:064x}")
        assert [record["turn_index"] for record in by_hash] == [1]

    def test_the_diagnosis_status_filter_separates_uncertain_from_confirmed(
        self, explorer_app: ExplorerApp
    ) -> None:
        """Suspected is a first-class filter value, not a side note: an
        operator triaging the uncertain bucket must be able to ask for it."""
        client, turns, _grants, _audit, _ = explorer_app
        for case in _seeded_cases():
            _plant(turns, case)

        suspected = _search(client, TRACE_TENANT, diagnosis_status="suspected")
        assert {record["turn_index"] for record in suspected} == {5, 7}

        confirmed = _search(client, TRACE_TENANT, diagnosis_status="confirmed")
        assert {record["turn_index"] for record in confirmed} == {2, 4, 6}

    def test_each_filter_alone_still_locates_its_cases(self, explorer_app: ExplorerApp) -> None:
        """Filters compose, but each also works alone; the demo needs both."""
        client, turns, _grants, _audit, _ = explorer_app
        for case in _seeded_cases():
            _plant(turns, case)

        for cause in (
            "stale_source",
            "retrieval_rank",
            "context_truncation",
            "prompt_regression",
            "model_behavior",
            "grounding_or_citation_error",
            "tool_error",
            "ingestion_or_index_error",
        ):
            found = _search(client, TRACE_TENANT, cause=cause)
            assert found, f"cause filter {cause} returned nothing"


def _turn_record_by_trace_id(turns: InMemoryTurnRecordStore, tenant_id: str, trace_id: str) -> Any:
    return asyncio.run(turns.for_trace_id(tenant_id, trace_id))


class TestTenantIsolation:
    def test_a_filter_that_matches_another_tenant_matches_nothing_here(
        self, explorer_app: ExplorerApp
    ) -> None:
        """Search is a tenant-scoped query surface: the same manifest hash or
        cause that hits in tenant B returns zero rows in tenant A."""
        client, turns, _grants, _audit, _ = explorer_app
        case = _seeded_cases()[7]
        planted = asyncio.run(
            turns.record(
                OTHER_TENANT,
                uuid.uuid4(),
                content=case.content,
                outcome=case.outcome,
                component_manifest_hash=f"{case.number:064x}",
                diagnosis_causes=case.causes,
                diagnosis_statuses=case.statuses,
            )
        )
        assert planted.tenant_id == OTHER_TENANT

        assert _search(client, TRACE_TENANT, cause="grounding_or_citation_error") == []
        assert _search(client, TRACE_TENANT, manifest_hash=f"{case.number:064x}") == []

    def test_a_turn_from_another_tenant_reads_as_missing(self, explorer_app: ExplorerApp) -> None:
        client, turns, _grants, _audit, _ = explorer_app
        case = _seeded_cases()[7]
        planted = asyncio.run(
            turns.record(
                OTHER_TENANT,
                uuid.uuid4(),
                content=case.content,
                outcome=case.outcome,
                component_manifest_hash=f"{case.number:064x}",
                diagnosis_causes=case.causes,
                diagnosis_statuses=case.statuses,
            )
        )

        response = client.get(
            f"/api/admin/traces/{planted.turn_id}",
            params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
            headers=_operator(),
        )

        assert response.status_code == 404
        assert response.json()["code"] == "not_found"

    def test_gold_cases_are_tenant_scoped(self, explorer_app: ExplorerApp) -> None:
        client, _turns, grants, audit, _ = explorer_app
        asyncio.run(grants.grant(OTHER_TENANT, "operator-7", granted_by="platform-admin-1"))

        clearview = client.get(
            "/api/admin/traces/gold-cases",
            params={"tenant_id": "clearview", "reason": READ_REASON},
            headers=_operator(),
        )
        apex = client.get(
            "/api/admin/traces/gold-cases",
            params={"tenant_id": OTHER_TENANT, "reason": READ_REASON},
            headers=_operator(),
        )

        assert clearview.status_code == 200
        clearview_ids = {case["case_id"] for case in clearview.json()["cases"]}
        assert "clearview-hvac-current-pricing" in clearview_ids
        assert "apex-hvac-hours" not in clearview_ids
        assert all(case["tenant_id"] == TRACE_TENANT for case in clearview.json()["cases"])
        assert apex.status_code == 200
        apex_ids = {case["case_id"] for case in apex.json()["cases"]}
        assert "apex-hvac-hours" in apex_ids
        assert all(case["tenant_id"] == OTHER_TENANT for case in apex.json()["cases"])
        assert any(case["scenario"] == "cross_tenant" for case in apex.json()["cases"])
        gold_reads = [event for event in audit._events if event.action == "trace.gold_read"]
        assert len(gold_reads) == 2


def _planted_turn_id(turns: InMemoryTurnRecordStore, case: SeededCase) -> str:
    turn = _turn_record_by_trace_id(turns, case.tenant_id, f"trace-gateb-{case.number}")
    return str(turn.turn_id)


class TestSearchResultsAreContentFree:
    def test_the_list_endpoint_never_repeats_content(self, explorer_app: ExplorerApp) -> None:
        """The attribution surface stays a queryable index entry: a prompt or
        answer planted in the record never appears in a search result."""
        client, turns, _grants, _audit, _ = explorer_app
        for case in _seeded_cases():
            _plant(turns, case)

        records = _search(client, "clearview")
        assert records
        serialized = json.dumps(records)
        for secret in (
            "System brief for",
            "Clearview is open daily",
            "clearview-windows-99",
            "booking_already_proposed",
        ):
            assert secret not in serialized
        assert all("content" not in record for record in records)
        assert all("diagnosis_statuses" in record for record in records)


class TestReplay:
    def _replay_client(
        self,
        *,
        script: list[ModelResponse],
    ) -> tuple[
        TestClient,
        InMemoryTurnRecordStore,
        InMemoryTraceAccessStore,
        InMemoryAuditStore,
        ScriptedModel,
    ]:
        model = ScriptedModel(script)
        client, turns, grants, audit, _ = _explorer_app(model=model)
        asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))
        asyncio.run(grants.grant(OTHER_TENANT, "operator-7", granted_by="platform-admin-1"))
        return client, turns, grants, audit, model

    def test_replay_rebuilds_the_stored_prompt_and_compares_manifests(
        self,
    ) -> None:
        """A replayed turn reconstructs the exact stored prompt, re-hashes it
        against the stored content hash, and runs it through the current model
        with no tools; the manifest comparison reports no change when this
        deployment serves the same components."""
        client, turns, _grants, audit, model = self._replay_client(
            script=[
                ModelResponse(
                    content="We are open daily from 7 AM to 7 PM. [evidence:clearview-hvac-2]",
                    model_name="scripted",
                )
            ]
        )
        case = _seeded_cases()[0]
        _plant(turns, case)
        headers = _operator()
        headers[CSRF_HEADER] = _csrf(client, headers)

        response = client.post(
            f"/api/admin/traces/{_planted_turn_id(turns, case)}/replay",
            params={"tenant_id": case.tenant_id, "reason": READ_REASON},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["stochastic"] is True
        assert body["manifest_changed"] is False
        assert body["original"]["content_hash"] == body["replayed"]["content_hash"]
        assert body["replayed"]["model_name"] == "scripted"
        assert body["replayed"]["output_raw"] == body["original"]["output_raw"]
        assert len(model.calls) == 1
        rebuilt_prompt = model.calls[0]
        assert rebuilt_prompt.template_ref == DISPATCH_SYSTEM_REF
        assert all(not message.tool_calls for message in rebuilt_prompt.messages)
        assert all(not component["changed"] for component in body["components"])

        replays = [event for event in audit._events if event.action == "trace.replay"]
        assert len(replays) == 1
        assert replays[0].principal_id == "operator-7"
        assert str(replays[0].resource_id) == _planted_turn_id(turns, case)
        assert replays[0].details == {
            "reason": READ_REASON,
            "manifest_changed": False,
            "changed_components": [],
        }

    def test_replay_distinguishes_a_changed_manifest(self) -> None:
        """A turn pinned to an older prompt template or model replays into a
        deployment that serves neither: the comparison names the components."""
        client, turns, _grants, _audit, _model = self._replay_client(
            script=[ModelResponse(content="Replayed under the new prompt.", model_name="gpt-9")]
        )
        case = _seeded_cases()[5]
        _plant(turns, case)
        headers = _operator()
        headers[CSRF_HEADER] = _csrf(client, headers)

        response = client.post(
            f"/api/admin/traces/{_planted_turn_id(turns, case)}/replay",
            params={"tenant_id": case.tenant_id, "reason": READ_REASON},
            headers=headers,
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["manifest_changed"] is True
        changed = {component["name"] for component in body["components"] if component["changed"]}
        assert "prompt_template" in changed
        assert "model" in changed
        assert body["original"]["model_name"] == "scripted"
        assert body["replayed"]["model_name"] == "gpt-9"
        assert body["replayed"]["output_raw"] == "Replayed under the new prompt."

    def test_replay_requires_the_dedicated_role_and_the_csrf_token(
        self,
    ) -> None:
        client, turns, _grants, _audit, _model = self._replay_client(
            script=[ModelResponse(content="should never run", model_name="scripted")]
        )
        case = _seeded_cases()[0]
        _plant(turns, case)

        no_role = client.post(
            f"/api/admin/traces/{_planted_turn_id(turns, case)}/replay",
            params={"tenant_id": case.tenant_id, "reason": READ_REASON},
        )
        with_role = client.post(
            f"/api/admin/traces/{_planted_turn_id(turns, case)}/replay",
            params={"tenant_id": case.tenant_id, "reason": READ_REASON},
            headers=_operator(),
        )

        assert no_role.status_code == 401
        assert with_role.status_code == 403
        assert with_role.json()["code"] == "csrf_validation_failed"

    def test_replay_refuses_a_record_without_a_reconstructible_prompt(
        self,
    ) -> None:
        client, turns, _grants, _audit, _model = self._replay_client(
            script=[ModelResponse(content="should never run", model_name="scripted")]
        )
        recorded = asyncio.run(
            turns.record(
                TRACE_TENANT,
                uuid.uuid4(),
                content={
                    "routing": {"rule": "clarify"},
                    "outcome": {"status": "clarified", "rounds": 0, "failure": None},
                },
            )
        )
        headers = _operator()
        headers[CSRF_HEADER] = _csrf(client, headers)

        response = client.post(
            f"/api/admin/traces/{recorded.turn_id}/replay",
            params={"tenant_id": TRACE_TENANT, "reason": READ_REASON},
            headers=headers,
        )

        assert response.status_code == 400
        assert response.json()["code"] == "trace_replay_error"

    def test_replay_is_tenant_scoped_and_audited_to_the_turn(
        self,
    ) -> None:
        client, turns, grants, audit, _model = self._replay_client(
            script=[ModelResponse(content="should never run", model_name="scripted")]
        )
        case = _seeded_cases()[0]
        _plant(turns, case)
        # The operator may read the other tenant, but the replay of a turn
        # that lives here must still be refused as if it did not exist.
        asyncio.run(grants.grant(OTHER_TENANT, "operator-7", granted_by="platform-admin-1"))
        headers = _operator()
        headers[CSRF_HEADER] = _csrf(client, headers)

        wrong_tenant = client.post(
            f"/api/admin/traces/{_planted_turn_id(turns, case)}/replay",
            params={"tenant_id": OTHER_TENANT, "reason": READ_REASON},
            headers=headers,
        )

        assert wrong_tenant.status_code == 404
        assert wrong_tenant.json()["code"] == "not_found"
        assert not any(event.action == "trace.replay" for event in audit._events)

    def test_replay_without_a_model_is_a_503(self, explorer_app: ExplorerApp) -> None:
        client, turns, _grants, _audit, _ = explorer_app
        case = _seeded_cases()[0]
        _plant(turns, case)
        headers = _operator()
        headers[CSRF_HEADER] = _csrf(client, headers)

        response = client.post(
            f"/api/admin/traces/{_planted_turn_id(turns, case)}/replay",
            params={"tenant_id": case.tenant_id, "reason": READ_REASON},
            headers=headers,
        )

        assert response.status_code == 503
        assert response.json()["code"] == "chat_unavailable"


class TestGoldSnapshot:
    def test_the_embedded_snapshot_matches_the_eval_fixtures(self) -> None:
        """The fixtures are the source of truth; drift in either direction is
        a build failure, not a silent divergence."""
        repo_root = Path(__file__).resolve().parents[3]
        cases = json.loads((repo_root / "evals/fixtures/cases.json").read_text())["cases"]
        corpus = json.loads((repo_root / "evals/fixtures/corpus.json").read_text())["documents"]
        chunks = {chunk["id"]: chunk["text"] for document in corpus for chunk in document["chunks"]}
        snapshot = json.loads(
            (
                Path(__file__).resolve().parents[3]
                / "services/api/src/tenantchat/api/gold_cases.json"
            ).read_text()
        )["cases"]

        assert len(snapshot) == len(cases)
        by_id = {case["case_id"]: case for case in snapshot}
        for case in cases:
            embedded = by_id[case["id"]]
            assert embedded["tenant_id"] == case["tenant_id"]
            assert embedded["query"] == case["query"]
            assert embedded["scenario"] == case.get("scenario")
            assert {chunk["source_id"]: chunk["text"] for chunk in embedded["gold_chunks"]} == {
                chunk_id: chunks[chunk_id] for chunk_id in case["gold_chunk_ids"]
            }


class TestTheTenCaseWalkthrough:
    def test_ten_cases_are_locatable_drillable_and_where_relevant_replayable(
        self,
    ) -> None:
        """The Gate B acceptance script's ten cases, end to end: six-filter
        locate, full-record drill, and a replay result for the cases whose
        stored prompts reconstruct."""
        model = ScriptedModel(
            [
                ModelResponse(
                    content="Replayed trial output. [evidence:clearview-hvac-2]",
                    model_name="scripted",
                ),
                ModelResponse(content="Replayed trial output.", model_name="scripted"),
            ]
        )
        client, turns, grants, audit, _ = _explorer_app(model=model)
        asyncio.run(grants.grant(TRACE_TENANT, "operator-7", granted_by="platform-admin-1"))
        asyncio.run(grants.grant("clearview", "operator-7", granted_by="platform-admin-1"))
        with client:
            for case in _seeded_cases():
                _plant(turns, case)

            for case in _seeded_cases():
                found = _search(client, case.tenant_id, **case.locate)
                assert found, f"case {case.number} not located by {case.locate}"
                turn = _turn_record_by_trace_id(turns, case.tenant_id, f"trace-gateb-{case.number}")
                read = client.get(
                    f"/api/admin/traces/{turn.turn_id}",
                    params={"tenant_id": case.tenant_id, "reason": READ_REASON},
                    headers=_operator(),
                )
                assert read.status_code == 200
                content = read.json()["content"]
                for section in ("routing", "retrieval", "prompt", "verdicts", "tools", "outcome"):
                    assert section in content, f"case {case.number} lacks {section}"
                if case.causes:
                    assert {record["cause"] for record in content["diagnoses"]} == set(case.causes)
                    assert {record["status"] for record in content["diagnoses"]} == set(
                        case.statuses
                    )
                else:
                    assert content["diagnoses"] == []
                if case.number in (6, 7):
                    headers = _operator()
                    headers[CSRF_HEADER] = _csrf(client, headers)
                    replayed = client.post(
                        f"/api/admin/traces/{turn.turn_id}/replay",
                        params={"tenant_id": case.tenant_id, "reason": READ_REASON},
                        headers=headers,
                    )
                    assert replayed.status_code == 200, replayed.text
                    assert replayed.json()["stochastic"] is True

            replays = [event for event in audit._events if event.action == "trace.replay"]
            assert len(replays) == 2
            searches = [event for event in audit._events if event.action == "trace.search"]
            assert len(searches) == 10
