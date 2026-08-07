"""The `OBS-004` trace: what the record holds, what the manifest pins, what the
detector concludes, and what a stored record can reconstruct."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

import pytest

from tenantchat.core.routing import ROUTING_POLICY_VERSION, RoutingOutcome, RoutingRule
from tenantchat.orchestration.agents import AGENTS_VERSION
from tenantchat.orchestration.graph import GRAPH_VERSION
from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    MessageRole,
    PromptRegion,
    PromptSegment,
)
from tenantchat.orchestration.nodes import _prompt_assembly_dict
from tenantchat.orchestration.prompts import DISPATCH_SYSTEM_REF
from tenantchat.orchestration.prompts.assembly import AssemblyOutcome
from tenantchat.orchestration.state import initial_state
from tenantchat.orchestration.tools import TOOLS_VERSION
from tenantchat.orchestration.trace import (
    TRACE_SCHEMA_VERSION,
    DiagnosisCause,
    DiagnosisConfidence,
    DiagnosisRecord,
    DiagnosisRole,
    DiagnosisStage,
    DiagnosisStatus,
    build_turn_trace,
    manifest_hash,
    reconstruct_prompt,
)


def _state(**overrides: object) -> dict[str, object]:
    state = dict(initial_state("clearview", "session-1", "What are your hours?"))
    state.update(overrides)
    return state


def _routing(**fields: object) -> dict[str, object]:
    return {
        "policy_version": ROUTING_POLICY_VERSION,
        "outcome": RoutingOutcome.DIRECT.value,
        "rule": RoutingRule.MATCHED.value,
        "chosen": "general",
        "confidence": 4.0,
        "direct_threshold": 4.0,
        "clarify_threshold": 2.5,
        "conflict_gap": 2.0,
        "candidates": [
            {"intent": "general", "score": 4.0, "matched_signals": ["hours"]},
            {"intent": "booking", "score": 0.0, "matched_signals": []},
        ],
    } | fields


def _prompt(**fields: object) -> dict[str, object]:
    return {
        "template_ref": DISPATCH_SYSTEM_REF,
        "content_hash": "deadbeef",
        "bindings": {"business_name": "Clearview Property Care"},
        "excluded": [],
        "messages": [
            {"role": "system", "content": "You are the Clearview assistant."},
            {"role": "user", "content": "What are your hours?"},
        ],
    } | fields


def _evidence_meta(**fields: object) -> dict[str, object]:
    return {
        "sufficient": True,
        "retriever_version": "search@1",
        "reranker": "bigram-overlap",
        "min_evidence_score": 0.5,
        "embedding_model": "scripted-embedder.v1",
        "generation_id": "gen-1",
        "filters": {"tenant_id": "clearview", "domain": None, "version_ids": []},
        "budget": {"max_sources": 3, "max_context_tokens": 1500},
        "parameters": {"vector_weight": 0.4, "k": 5},
    } | fields


def _answered(**overrides: object) -> dict[str, object]:
    """The checkpoint of a turn the model answered with one verified citation."""
    return (
        _state(
            routing_decision=_routing(),
            prompt_assembly=_prompt(),
            evidence=[
                {
                    "source_id": "doc-1",
                    "score": 1.0,
                    "generation_id": "gen-1",
                    "embedding_model": "scripted-embedder.v1",
                }
            ],
            evidence_meta=_evidence_meta(),
            transcript=[
                {
                    "role": "user",
                    "content": "What are your hours?",
                    "tool_calls": [],
                    "tool_call_id": "",
                },
                {
                    "role": "assistant",
                    "content": "We are open daily. [evidence:doc-1]",
                    "tool_calls": [],
                    "tool_call_id": "",
                },
            ],
            citations=[{"source_id": "doc-1", "title": "Hours"}],
            citation_invalid=[],
            model_name="scripted",
            model_usage={"input_tokens": 120, "output_tokens": 24},
            rounds=1,
        )
        | overrides
    )


def _diagnosis(
    cause: DiagnosisCause,
    stage: DiagnosisStage,
    role: DiagnosisRole,
    status: DiagnosisStatus,
    confidence: DiagnosisConfidence,
    evidence: tuple[str, ...],
) -> dict[str, object]:
    return {
        "cause": cause.value,
        "stage": stage.value,
        "role": role.value,
        "status": status.value,
        "confidence": confidence.value,
        "evidence": list(evidence),
        "detector_version": "diagnosis@1",
    }


def _section(trace: Mapping[str, object], key: str) -> dict[str, object]:
    """One trace section as the mapping the record holds it as."""
    value = trace[key]
    assert isinstance(value, Mapping)
    return dict(value)


def _diagnoses(trace: Mapping[str, object]) -> list[dict[str, object]]:
    value = trace["diagnoses"]
    assert isinstance(value, list)
    return [dict(item) for item in value if isinstance(item, Mapping)]


def test_a_trace_round_trips_through_json() -> None:
    """The record is JSON-safe: it survives the same serialization a store uses."""
    trace = build_turn_trace(_answered(), pending=None)

    assert json.loads(json.dumps(trace)) == trace


def test_the_trace_carries_the_schema_version_and_turn_index() -> None:
    """The shape version is the reader's contract; the index the query key."""
    trace = build_turn_trace(_answered(), pending=None)

    assert trace["schema_version"] == TRACE_SCHEMA_VERSION
    assert trace["turn_index"] == 1


def test_the_trace_carries_every_router_candidate_and_signal() -> None:
    """A misrouted turn is explainable down to the scores and matched signals."""
    trace = build_turn_trace(_answered(), pending=None)

    assert _section(trace, "routing") == _routing()


def test_the_retrieval_section_names_candidates_filters_and_budgets() -> None:
    """The candidate list is the retrieval envelope; the full passages stay beside it."""
    trace = build_turn_trace(_answered(), pending=None)
    retrieval = _section(trace, "retrieval")
    assert retrieval["query"] == "What are your hours?"
    assert retrieval["sufficient"] is True
    assert retrieval["retriever_version"] == "search@1"
    assert retrieval["reranker"] == "bigram-overlap"
    assert retrieval["min_evidence_score"] == 0.5
    assert retrieval["embedding_model"] == "scripted-embedder.v1"
    assert retrieval["generation_id"] == "gen-1"
    assert retrieval["filters"] == {"tenant_id": "clearview", "domain": None, "version_ids": []}
    assert retrieval["budget"] == {"max_sources": 3, "max_context_tokens": 1500}
    assert retrieval["parameters"] == {"vector_weight": 0.4, "k": 5}
    assert retrieval["candidates"] == [
        {
            "source_id": "doc-1",
            "score": 1.0,
            "generation_id": "gen-1",
            "embedding_model": "scripted-embedder.v1",
        }
    ]
    assert retrieval["evidence"] == retrieval["candidates"]


def test_the_published_output_keeps_the_raw_markers_and_the_parsed_claims() -> None:
    """The trace holds both what the model wrote and what the validator parsed."""
    trace = build_turn_trace(_answered(), pending=None)

    assert _section(trace, "output")["raw"] == "We are open daily. [evidence:doc-1]"
    assert _section(trace, "output")["claims"] == ["doc-1"]


def test_the_manifest_pins_every_component_and_hashes_content_free() -> None:
    """Same components, different conversation, same hash; any component change, new hash."""
    first = build_turn_trace(_answered(), pending=None)
    second = build_turn_trace(
        _answered(
            transcript=[
                {
                    "role": "user",
                    "content": "Do you work weekends?",
                    "tool_calls": [],
                    "tool_call_id": "",
                },
                {
                    "role": "assistant",
                    "content": "Yes. [evidence:doc-1]",
                    "tool_calls": [],
                    "tool_call_id": "",
                },
            ]
        ),
        pending=None,
    )
    manifest = _section(first, "component_manifest")
    assert manifest["graph"] == GRAPH_VERSION
    assert manifest["prompt_template"] == {"ref": DISPATCH_SYSTEM_REF}
    assert manifest["routing_policy"] == ROUTING_POLICY_VERSION
    assert manifest["agents"] == AGENTS_VERSION
    assert manifest["tools"] == TOOLS_VERSION
    assert manifest["retriever"] == {
        "version": "search@1",
        "reranker": "bigram-overlap",
        "min_evidence_score": 0.5,
        "embedding_model": "scripted-embedder.v1",
        "generation_id": "gen-1",
        "parameters": {"vector_weight": 0.4, "k": 5},
        "filters": {"tenant_id": "clearview", "domain": None, "version_ids": []},
        "budget": {"max_sources": 3, "max_context_tokens": 1500},
    }
    assert manifest["model"] == {"id": "scripted", "parameters": {}}
    assert re.fullmatch(r"[0-9a-f]{64}", str(first["manifest_hash"]))
    assert first["manifest_hash"] == second["manifest_hash"]
    assert (
        build_turn_trace(_answered(model_name="other-model"), pending=None)["manifest_hash"]
        != first["manifest_hash"]
    )
    assert (
        build_turn_trace(_answered(evidence_meta={}), pending=None)["manifest_hash"]
        != first["manifest_hash"]
    )


def test_the_manifest_hash_is_deterministic_regardless_of_key_order() -> None:
    """sort_keys canonicalization makes ordering irrelevant, so hashing never racers."""
    assert manifest_hash({"a": "dispatch@2", "b": {"c": 1}}) == manifest_hash(
        {"b": {"c": 1}, "a": "dispatch@2"}
    )


def test_a_stored_prompt_reconstructs_the_exact_prompt_and_its_hash() -> None:
    """The reconstruction contract: the record alone rebuilds what the provider
    received, byte for byte, and the re-derived hash matches the stored one."""
    original = AssembledPrompt(
        template_id="dispatch-system",
        template_version=4,
        bindings={"business_name": "Clearview Property Care"},
        messages=(
            AssembledMessage(
                role=MessageRole.SYSTEM,
                segments=(
                    PromptSegment(
                        "intro", PromptRegion.TRUSTED, "You are the Clearview assistant."
                    ),
                    PromptSegment(
                        "evidence:doc-1",
                        PromptRegion.UNTRUSTED,
                        "Hours\nWe are open daily.",
                    ),
                ),
            ),
            AssembledMessage(
                role=MessageRole.USER,
                segments=(PromptSegment("user:0", PromptRegion.UNTRUSTED, "What are your hours?"),),
            ),
        ),
    )
    section = _prompt_assembly_dict(AssemblyOutcome(prompt=original, excluded=()))
    trace = build_turn_trace(
        _answered(prompt_assembly=section, evidence_meta=_evidence_meta()),
        pending=None,
    )

    rebuilt = reconstruct_prompt(_section(trace, "prompt"))

    assert rebuilt.content_hash == section["content_hash"]
    assert rebuilt.template_ref == DISPATCH_SYSTEM_REF
    assert [message.content for message in rebuilt.messages] == [
        "You are the Clearview assistant.Hours\nWe are open daily.",
        "What are your hours?",
    ]


def test_reconstruct_prompt_refuses_a_section_without_a_template_ref() -> None:
    with pytest.raises(ValueError, match="no template ref"):
        reconstruct_prompt({"messages": []})


def test_an_abstention_records_the_outcome_and_a_retrieval_miss() -> None:
    trace = build_turn_trace(
        _answered(
            model_name="",
            evidence_meta=_evidence_meta(sufficient=False),
            transcript=[
                {
                    "role": "user",
                    "content": "What are your hours?",
                    "tool_calls": [],
                    "tool_call_id": "",
                }
            ],
        ),
        pending=None,
    )

    assert _section(trace, "outcome") == {"status": "abstained", "rounds": 1, "failure": None}
    assert _diagnoses(trace) == [
        _diagnosis(
            DiagnosisCause.RETRIEVAL_MISS,
            DiagnosisStage.RETRIEVAL,
            DiagnosisRole.PRIMARY,
            DiagnosisStatus.DETECTED,
            DiagnosisConfidence.HIGH,
            ("retrieval.sufficient:false",),
        )
    ]


def test_an_unavailable_retriever_is_detected_as_an_index_error() -> None:
    """The graph cannot tell an empty index from a missing one; the record can."""
    trace = build_turn_trace(
        _answered(model_name="", evidence_meta=_evidence_meta(retriever_version="unavailable")),
        pending=None,
    )

    assert _section(trace, "outcome")["status"] == "abstained"
    assert _diagnoses(trace)[0] == _diagnosis(
        DiagnosisCause.INGESTION_OR_INDEX_ERROR,
        DiagnosisStage.RETRIEVAL,
        DiagnosisRole.PRIMARY,
        DiagnosisStatus.DETECTED,
        DiagnosisConfidence.HIGH,
        ("retrieval.retriever_version:unavailable",),
    )


def test_a_clarify_route_is_suspected_as_a_routing_error() -> None:
    """Ambiguity is only suspected: a genuinely unclear message routes the same way."""
    trace = build_turn_trace(
        _answered(
            routing_outcome=RoutingOutcome.CLARIFY.value,
            routing_decision=_routing(outcome="clarify", rule=RoutingRule.CLARIFY.value),
        ),
        pending=None,
    )

    assert _section(trace, "outcome")["status"] == "clarified"
    assert _diagnoses(trace) == [
        _diagnosis(
            DiagnosisCause.ROUTING_ERROR,
            DiagnosisStage.ROUTING,
            DiagnosisRole.PRIMARY,
            DiagnosisStatus.SUSPECTED,
            DiagnosisConfidence.LOW,
            (f"routing.rule:{RoutingRule.CLARIFY.value}",),
        )
    ]


def test_an_escaped_evidence_item_suspects_context_truncation() -> None:
    trace = build_turn_trace(
        _answered(
            prompt_assembly=_prompt(
                excluded=[{"kind": "evidence", "reference": "doc-9", "reason": "budget"}]
            )
        ),
        pending=None,
    )

    assert _diagnoses(trace) == [
        _diagnosis(
            DiagnosisCause.CONTEXT_TRUNCATION,
            DiagnosisStage.PROMPT,
            DiagnosisRole.CONTRIBUTING,
            DiagnosisStatus.SUSPECTED,
            DiagnosisConfidence.MEDIUM,
            ("prompt.excluded:doc-9:budget",),
        )
    ]


def test_a_fabricated_citation_is_a_detected_grounding_error() -> None:
    trace = build_turn_trace(_answered(citation_invalid=["doc-999"]), pending=None)

    assert _diagnoses(trace) == [
        _diagnosis(
            DiagnosisCause.GROUNDING_OR_CITATION_ERROR,
            DiagnosisStage.VALIDATION,
            DiagnosisRole.PRIMARY,
            DiagnosisStatus.DETECTED,
            DiagnosisConfidence.HIGH,
            ("citation_invalid:doc-999",),
        )
    ]


def test_a_tool_failure_escalation_is_a_confirmed_provider_failure() -> None:
    trace = build_turn_trace(_answered(failure="tool_failure"), pending=None)

    assert _section(trace, "outcome") == {
        "status": "escalated",
        "rounds": 1,
        "failure": "tool_failure",
    }
    assert _diagnoses(trace) == [
        _diagnosis(
            DiagnosisCause.PROVIDER_FAILURE,
            DiagnosisStage.MODEL,
            DiagnosisRole.PRIMARY,
            DiagnosisStatus.CONFIRMED,
            DiagnosisConfidence.HIGH,
            ("outcome.failure:tool_failure",),
        )
    ]


def test_an_unresolved_escalation_suspects_model_behavior() -> None:
    trace = build_turn_trace(_answered(failure="unresolved"), pending=None)

    assert _diagnoses(trace) == [
        _diagnosis(
            DiagnosisCause.MODEL_BEHAVIOR,
            DiagnosisStage.MODEL,
            DiagnosisRole.PRIMARY,
            DiagnosisStatus.SUSPECTED,
            DiagnosisConfidence.MEDIUM,
            ("outcome.failure:unresolved",),
        )
    ]


def test_an_unexpected_failure_is_an_application_error() -> None:
    trace = build_turn_trace(_answered(failure="provider_quota"), pending=None)

    assert _diagnoses(trace)[0] == _diagnosis(
        DiagnosisCause.APPLICATION_ERROR,
        DiagnosisStage.OUTCOME,
        DiagnosisRole.PRIMARY,
        DiagnosisStatus.DETECTED,
        DiagnosisConfidence.MEDIUM,
        ("outcome.failure:provider_quota",),
    )


@pytest.mark.parametrize(
    "reason", ["customer_request", "outside_policy"], ids=["customer", "policy"]
)
def test_a_visitor_requested_escalation_is_not_a_diagnosis(reason: str) -> None:
    """The expected handoffs are outcomes, not failures; no cause is invented."""
    trace = build_turn_trace(_answered(failure=reason), pending=None)

    assert _section(trace, "outcome")["status"] == "escalated"
    assert _diagnoses(trace) == []


@pytest.mark.parametrize(
    "code",
    ["unknown_tool", "tool_not_allowed", "booking_already_proposed"],
    ids=["unknown", "forbidden", "already-proposed"],
)
def test_a_failed_tool_result_is_a_contributing_tool_error(code: str) -> None:
    transcript = [
        {
            "role": "user",
            "content": "Book the slot.",
            "tool_calls": [],
            "tool_call_id": "",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"call_id": "c1", "name": "book_appointment", "arguments_json": "{}"}],
            "tool_call_id": "",
        },
        {"role": "tool", "content": f'{{"error": "{code}"}}', "tool_call_id": "c1"},
        {
            "role": "assistant",
            "content": "That slot is gone.",
            "tool_calls": [],
            "tool_call_id": "",
        },
    ]
    trace = build_turn_trace(
        _answered(transcript=transcript, rounds=2),
        pending=None,
    )

    assert _diagnoses(trace)[0] == _diagnosis(
        DiagnosisCause.TOOL_ERROR,
        DiagnosisStage.TOOLS,
        DiagnosisRole.CONTRIBUTING,
        DiagnosisStatus.DETECTED,
        DiagnosisConfidence.MEDIUM,
        (f"tools.result.error:{code}",),
    )


def test_a_refused_tool_call_is_a_detected_quarantine() -> None:
    """The guard refused a model-proposed call; the refusal is the
    attributable quarantine event (`RAG-007`), proven by the record no matter
    what the model meant by the call."""
    trace = build_turn_trace(
        _answered(refused_tools=["book_appointment"]),
        pending=None,
    )

    assert _diagnoses(trace) == [
        _diagnosis(
            DiagnosisCause.INJECTION_QUARANTINE,
            DiagnosisStage.TOOLS,
            DiagnosisRole.PRIMARY,
            DiagnosisStatus.DETECTED,
            DiagnosisConfidence.HIGH,
            ("verdicts.refused_tools:book_appointment",),
        )
    ]


def test_committed_effects_carry_their_idempotency_keys() -> None:
    """The trace names every side effect and the key that makes it replay-safe."""
    transcript = [
        {
            "role": "user",
            "content": "Book the slot.",
            "tool_calls": [],
            "tool_call_id": "",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "call_id": "c1",
                    "name": "book_appointment",
                    "arguments_json": '{"slot": "2026-08-06T10:00" }',
                }
            ],
            "tool_call_id": "",
        },
        {"role": "tool", "content": '{"receipt": "b-1"}', "tool_call_id": "c1"},
    ]
    trace = build_turn_trace(
        _answered(
            transcript=transcript,
            committed=[
                {
                    "action": "book_appointment",
                    "reference": "b-1",
                    "replayed": False,
                    "key": "ik-1",
                }
            ],
        ),
        pending=None,
    )

    assert _section(trace, "tools")["tool_calls"] == [
        {"call_id": "c1", "name": "book_appointment", "arguments": {"slot": "2026-08-06T10:00"}}
    ]
    assert _section(trace, "tools")["tool_results"] == [
        {"call_id": "c1", "result": '{"receipt": "b-1"}'}
    ]
    assert _section(trace, "tools")["committed"] == [
        {
            "action": "book_appointment",
            "reference": "b-1",
            "replayed": False,
            "idempotency_key": "ik-1",
        }
    ]


def test_a_pending_confirmation_pauses_the_turn() -> None:
    trace = build_turn_trace(
        _answered(model_name=""),
        pending={"booking": {"slot": "2026-08-06T10:00"}},
    )

    assert _section(trace, "outcome") == {"status": "paused", "rounds": 1, "failure": None}


def test_a_turn_without_a_model_call_has_no_model_section_and_no_diagnoses() -> None:
    """The abstention that skipped the call still has an honest model record."""
    trace = build_turn_trace(_state(evidence_meta=_evidence_meta(sufficient=False)), pending=None)

    assert _section(trace, "model") == {"name": "", "usage": {}}
    assert _section(trace, "outcome") == {"status": "abstained", "rounds": 0, "failure": None}
    assert _diagnoses(trace) == [
        _diagnosis(
            DiagnosisCause.RETRIEVAL_MISS,
            DiagnosisStage.RETRIEVAL,
            DiagnosisRole.PRIMARY,
            DiagnosisStatus.DETECTED,
            DiagnosisConfidence.HIGH,
            ("retrieval.sufficient:false",),
        )
    ]


def test_a_diagnosis_record_serializes_to_the_store_shape() -> None:
    """The API never imports the enum; the store keeps what the detector emitted."""
    record = DiagnosisRecord(
        cause=DiagnosisCause.RETRIEVAL_MISS,
        stage=DiagnosisStage.RETRIEVAL,
        role=DiagnosisRole.PRIMARY,
        status=DiagnosisStatus.DETECTED,
        confidence=DiagnosisConfidence.HIGH,
        evidence=("retrieval.sufficient:false",),
    )

    assert json.loads(json.dumps(record.to_dict())) == {
        "cause": "retrieval_miss",
        "stage": "retrieval",
        "role": "primary",
        "status": "detected",
        "confidence": "high",
        "evidence": ["retrieval.sufficient:false"],
        "detector_version": "diagnosis@1",
    }
