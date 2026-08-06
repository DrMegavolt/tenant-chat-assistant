"""The `OBS-004` inference trace: what one turn is attributable to.

Everything here is a pure function over the checkpointed state — never a
provider call, never a store write, never a raise. The graph stays fast and the
trace stays impossible to forget: :class:`~tenantchat.orchestration.runtime.DispatchRuntime`
builds it from the same state it already persisted, and the HTTP layer writes
it into the `PRIV-002` envelope the trace store owns.

The trace is JSON-safe content and belongs to the inference plane only
(`ADR-0010`): prompts, retrieved passages, answers, and the router's evidence
live here and nowhere in the operational plane. The manifest and its hash are
the content-free part — the same versions must hash the same, regardless of
what was said — which is what makes "answers got worse last Tuesday" a
question with an answer.

Attribution is :func:`diagnose`, a deterministic detector over the record
alone. Only causes decidable from the record are ever emitted; the rest of the
Gate B taxonomy — ``stale_source``, ``filter_exclusion``, ``retrieval_rank``,
``prompt_regression``, ``query_rewrite_error`` — needs replay, review, or a
retained index generation and stays empty until that evidence exists (Gate C).

Reconstruction is :func:`reconstruct_prompt`: the stored ``prompt`` section
mirrors the canonical form :attr:`AssembledPrompt.content_hash` hashes, so the
exact prompt the provider received can be rebuilt from a turn record alone and
re-hashed against the stored value — the "every answer is reconstructible"
contract made executable.

Deliberately not recorded here: the executed graph's nodes, edges, attempts,
and timing. That is LangGraph execution data, not checkpoint state, and
capturing it needs a callback listener that would tax the hot path this
checkpoint state already pays for; it is documented in `BACKLOG.md` as the
instrumentation follow-up.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tenantchat.core.commands import HandoffReason
from tenantchat.core.routing import ROUTING_POLICY_VERSION, RoutingOutcome, RoutingRule
from tenantchat.orchestration.agents import AGENTS_VERSION
from tenantchat.orchestration.graph import GRAPH_VERSION
from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    MessageRole,
    PromptRegion,
    PromptSegment,
    ToolCall,
)
from tenantchat.orchestration.nodes import citation_ids
from tenantchat.orchestration.prompts import DISPATCH_SYSTEM_REF
from tenantchat.orchestration.tools import TOOLS_VERSION

# The shape version of the trace's ``content`` object. Bumping it is a reviewable
# change: the store's governance never parses content, so readers (the trace
# viewer, export, erasure) must branch on this to stay honest across releases.
TRACE_SCHEMA_VERSION: Final = "1"

DETECTOR_VERSION: Final = "diagnosis@1"


class TurnOutcome(StrEnum):
    """How the turn ended, as the trace's closed status vocabulary."""

    ANSWERED = "answered"
    PAUSED = "paused"
    ESCALATED = "escalated"
    ABSTAINED = "abstained"
    CLARIFIED = "clarified"


class DiagnosisCause(StrEnum):
    """The bounded Gate B causes, safe as metric dimensions.

    ``stale_source`` through ``query_rewrite_error`` are part of the taxonomy
    but are not decidable from the record alone, so :func:`diagnose` never
    emits them: a cause that can only be suspected is a review note, not a
    dimension.
    """

    STALE_SOURCE = "stale_source"
    INGESTION_OR_INDEX_ERROR = "ingestion_or_index_error"
    ROUTING_ERROR = "routing_error"
    QUERY_REWRITE_ERROR = "query_rewrite_error"
    FILTER_EXCLUSION = "filter_exclusion"
    RETRIEVAL_MISS = "retrieval_miss"
    RETRIEVAL_RANK = "retrieval_rank"
    CONTEXT_TRUNCATION = "context_truncation"
    PROMPT_REGRESSION = "prompt_regression"
    MODEL_BEHAVIOR = "model_behavior"
    GROUNDING_OR_CITATION_ERROR = "grounding_or_citation_error"
    TOOL_ERROR = "tool_error"
    APPLICATION_ERROR = "application_error"
    PROVIDER_FAILURE = "provider_failure"


class DiagnosisStage(StrEnum):
    """Which stage of the pipeline the cause is attributed to."""

    ROUTING = "routing"
    RETRIEVAL = "retrieval"
    PROMPT = "prompt"
    MODEL = "model"
    VALIDATION = "validation"
    TOOLS = "tools"
    OUTCOME = "outcome"


class DiagnosisRole(StrEnum):
    """Whether the cause is the reason the turn failed or a contributing one."""

    PRIMARY = "primary"
    CONTRIBUTING = "contributing"


class DiagnosisStatus(StrEnum):
    """How far the evidence goes, from observed to reviewed."""

    DETECTED = "detected"
    SUSPECTED = "suspected"
    CONFIRMED = "confirmed"
    INCONCLUSIVE = "inconclusive"


class DiagnosisConfidence(StrEnum):
    """The detector's confidence, low to high."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True, slots=True)
class DiagnosisRecord:
    """One attributed cause, with the record references that support it.

    ``evidence`` names the trace fields that fired the detector — e.g.
    ``citation_invalid:clearview-hvac-999`` — so a diagnosis can be checked
    against the record it was read from, and a later reviewer decision can
    attach without rewriting this record.
    """

    cause: DiagnosisCause
    stage: DiagnosisStage
    role: DiagnosisRole
    status: DiagnosisStatus
    confidence: DiagnosisConfidence
    evidence: tuple[str, ...] = ()
    detector_version: str = DETECTOR_VERSION

    def to_dict(self) -> dict[str, object]:
        return {
            "cause": self.cause.value,
            "stage": self.stage.value,
            "role": self.role.value,
            "status": self.status.value,
            "confidence": self.confidence.value,
            "evidence": list(self.evidence),
            "detector_version": self.detector_version,
        }


def build_turn_trace(
    state: Mapping[str, object], *, pending: Mapping[str, object] | None
) -> dict[str, object]:
    """The `OBS-004` trace of one completed turn, as JSON-safe data.

    Everything the record can know is included: the full router decision, the
    retrieval that ran and the candidates it admitted, the one assembled
    prompt (rendered messages, so the exact bytes the provider received can be
    reproduced and re-hashed), model usage, the raw output and its parsed
    claims, the citation verdicts, the tool effects with their idempotency
    keys, the outcome, the component manifest with its content-free hash, and
    the auto-detected diagnoses.
    """
    routing = _mapping(state.get("routing_decision"))
    prompt = _mapping(state.get("prompt_assembly"))
    evidence = _list_of_dicts(state.get("evidence"))
    history = _list_of_dicts(state.get("transcript"))
    raw_output, claims = _published_output(history)
    manifest = _component_manifest(state, prompt, routing)
    verdicts = {
        "citations": _list_of_dicts(state.get("citations")),
        "citation_invalid": _list_of_str(state.get("citation_invalid")),
        "refused_tools": _list_of_str(state.get("refused_tools")),
        "claims_invalid": [dict(claim) for claim in _list_of_dicts(state.get("claims_invalid"))],
    }
    trace: dict[str, object] = {
        "schema_version": TRACE_SCHEMA_VERSION,
        "turn_index": _int(state.get("turn_index")),
        "routing": routing or None,
        "retrieval": _retrieval_section(state, history, evidence),
        "prompt": prompt or None,
        "model": {
            "name": str(state.get("model_name", "")),
            "usage": _mapping(state.get("model_usage")),
        },
        "output": {
            "answer": str(state.get("answer", "")),
            "raw": raw_output,
            "claims": list(claims),
        },
        "verdicts": verdicts,
        "tools": _tools_section(state, history),
        "outcome": _outcome_section(state, pending),
        "component_manifest": manifest,
    }
    trace["manifest_hash"] = manifest_hash(manifest)
    trace["diagnoses"] = [record.to_dict() for record in diagnose(trace)]
    return trace


def reconstruct_prompt(section: Mapping[str, object]) -> AssembledPrompt:
    """Rebuild the exact prompt the provider received from its trace section.

    Raises:
        ValueError: the stored section does not name a resolvable template or
            carries a message a :class:`MessageRole` does not know.
    """
    ref = str(section.get("template_ref", ""))
    try:
        template_id, raw_version = ref.rsplit("@", 1)
        version = int(raw_version)
    except ValueError as error:
        raise ValueError(f"trace prompt has no template ref: {ref!r}") from error
    messages = tuple(
        AssembledMessage(
            role=MessageRole(str(message["role"])),
            segments=tuple(
                PromptSegment(
                    segment_id=str(segment[0]),
                    region=PromptRegion(str(segment[1])),
                    text=str(segment[2]),
                )
                for segment in _list_of_lists(message.get("segments"))
            ),
            tool_calls=tuple(
                ToolCall(
                    call_id=str(call["id"]),
                    name=str(call["name"]),
                    arguments=_mapping(call.get("arguments")),
                )
                for call in _list_of_dicts(message.get("tool_calls"))
            ),
            tool_call_id=(
                str(message.get("tool_call_id"))
                if message.get("tool_call_id") is not None
                else None
            ),
        )
        for message in _list_of_dicts(section.get("messages"))
    )
    return AssembledPrompt(
        template_id=template_id,
        template_version=version,
        bindings={str(key): str(value) for key, value in _mapping(section.get("bindings")).items()},
        messages=messages,
    )


def manifest_hash(manifest: Mapping[str, object]) -> str:
    """SHA-256 over the canonical content-free manifest.

    Deterministic and content-free by construction: the manifest carries only
    versions, so two turns with the same components hash the same no matter
    what was said.
    """
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def diagnose(trace: Mapping[str, object]) -> tuple[DiagnosisRecord, ...]:
    """Auto-detect the anomalous Gate B causes the record alone decides.

    Everything here is deliberately conservative: the router asking a question
    is ``suspected`` rather than ``confirmed``, because a genuinely ambiguous
    message routes the same way; only effects the record proves — a fabricated
    citation id, an index that did not run, a model call that failed — are
    ``detected`` or ``confirmed``. Causes that need replay or review stay
    unemitted.
    """
    records: list[DiagnosisRecord] = []
    routing = _mapping(trace.get("routing"))
    retrieval = _mapping(trace.get("retrieval"))
    prompt = _mapping(trace.get("prompt"))
    verdicts = _mapping(trace.get("verdicts"))
    tools = _mapping(trace.get("tools"))
    outcome = _mapping(trace.get("outcome"))

    if routing:
        rule = str(routing.get("rule", ""))
        if rule in (RoutingRule.CLARIFY.value, RoutingRule.BOUNDED_CLARIFY.value):
            records.append(
                DiagnosisRecord(
                    cause=DiagnosisCause.ROUTING_ERROR,
                    stage=DiagnosisStage.ROUTING,
                    role=DiagnosisRole.PRIMARY,
                    status=DiagnosisStatus.SUSPECTED,
                    confidence=DiagnosisConfidence.LOW,
                    evidence=(f"routing.rule:{rule}",),
                )
            )

    if retrieval:
        if str(retrieval.get("retriever_version", "")) == "unavailable":
            records.append(
                DiagnosisRecord(
                    cause=DiagnosisCause.INGESTION_OR_INDEX_ERROR,
                    stage=DiagnosisStage.RETRIEVAL,
                    role=DiagnosisRole.PRIMARY,
                    status=DiagnosisStatus.DETECTED,
                    confidence=DiagnosisConfidence.HIGH,
                    evidence=("retrieval.retriever_version:unavailable",),
                )
            )
        elif str(outcome.get("status", "")) == TurnOutcome.ABSTAINED.value and not bool(
            retrieval.get("sufficient")
        ):
            records.append(
                DiagnosisRecord(
                    cause=DiagnosisCause.RETRIEVAL_MISS,
                    stage=DiagnosisStage.RETRIEVAL,
                    role=DiagnosisRole.PRIMARY,
                    status=DiagnosisStatus.DETECTED,
                    confidence=DiagnosisConfidence.HIGH,
                    evidence=("retrieval.sufficient:false",),
                )
            )

    if prompt:
        excluded_evidence = [
            item
            for item in _list_of_dicts(prompt.get("excluded"))
            if str(item.get("kind", "")) == "evidence"
        ]
        if excluded_evidence:
            records.append(
                DiagnosisRecord(
                    cause=DiagnosisCause.CONTEXT_TRUNCATION,
                    stage=DiagnosisStage.PROMPT,
                    role=DiagnosisRole.CONTRIBUTING,
                    status=DiagnosisStatus.SUSPECTED,
                    confidence=DiagnosisConfidence.MEDIUM,
                    evidence=tuple(
                        f"prompt.excluded:{item.get('reference')}:{item.get('reason')}"
                        for item in excluded_evidence
                    ),
                )
            )

    invalid = _list_of_str(verdicts.get("citation_invalid"))
    if invalid:
        records.append(
            DiagnosisRecord(
                cause=DiagnosisCause.GROUNDING_OR_CITATION_ERROR,
                stage=DiagnosisStage.VALIDATION,
                role=DiagnosisRole.PRIMARY,
                status=DiagnosisStatus.DETECTED,
                confidence=DiagnosisConfidence.HIGH,
                evidence=tuple(f"citation_invalid:{source_id}" for source_id in invalid),
            )
        )

    failure = str(outcome.get("failure", "")) if outcome.get("failure") else ""
    if failure == HandoffReason.TOOL_FAILURE.value:
        records.append(
            DiagnosisRecord(
                cause=DiagnosisCause.PROVIDER_FAILURE,
                stage=DiagnosisStage.MODEL,
                role=DiagnosisRole.PRIMARY,
                status=DiagnosisStatus.CONFIRMED,
                confidence=DiagnosisConfidence.HIGH,
                evidence=(f"outcome.failure:{failure}",),
            )
        )
    elif failure == HandoffReason.UNRESOLVED.value:
        records.append(
            DiagnosisRecord(
                cause=DiagnosisCause.MODEL_BEHAVIOR,
                stage=DiagnosisStage.MODEL,
                role=DiagnosisRole.PRIMARY,
                status=DiagnosisStatus.SUSPECTED,
                confidence=DiagnosisConfidence.MEDIUM,
                evidence=(f"outcome.failure:{failure}",),
            )
        )
    elif failure not in (
        "",
        HandoffReason.CUSTOMER_REQUEST.value,
        HandoffReason.OUTSIDE_POLICY.value,
    ):
        records.append(
            DiagnosisRecord(
                cause=DiagnosisCause.APPLICATION_ERROR,
                stage=DiagnosisStage.OUTCOME,
                role=DiagnosisRole.PRIMARY,
                status=DiagnosisStatus.DETECTED,
                confidence=DiagnosisConfidence.MEDIUM,
                evidence=(f"outcome.failure:{failure}",),
            )
        )

    for result in _list_of_dicts(tools.get("tool_results")):
        payload = _parsed_json(str(result.get("result", "")))
        if not isinstance(payload, Mapping):
            continue
        code = str(payload.get("error", ""))
        if code in ("unknown_tool", "tool_not_allowed", "booking_already_proposed"):
            records.append(
                DiagnosisRecord(
                    cause=DiagnosisCause.TOOL_ERROR,
                    stage=DiagnosisStage.TOOLS,
                    role=DiagnosisRole.CONTRIBUTING,
                    status=DiagnosisStatus.DETECTED,
                    confidence=DiagnosisConfidence.MEDIUM,
                    evidence=(f"tools.result.error:{code}",),
                )
            )

    return tuple(records)


def _retrieval_section(
    state: Mapping[str, object],
    history: Sequence[Mapping[str, object]],
    evidence: list[dict[str, object]],
) -> dict[str, object] | None:
    meta = _mapping(state.get("evidence_meta"))
    if not meta:
        return None
    return {
        "query": _latest_user_message(history),
        "sufficient": bool(meta.get("sufficient")),
        "retriever_version": str(meta.get("retriever_version", "")),
        "reranker": meta.get("reranker"),
        "min_evidence_score": meta.get("min_evidence_score"),
        "embedding_model": meta.get("embedding_model"),
        "generation_id": meta.get("generation_id"),
        "filters": _mapping(meta.get("filters")),
        "budget": _mapping(meta.get("budget")),
        "parameters": _mapping(meta.get("parameters")),
        "candidates": [
            {
                "source_id": str(item.get("source_id", "")),
                "score": item.get("score"),
                "generation_id": item.get("generation_id"),
                "embedding_model": item.get("embedding_model"),
            }
            for item in evidence
        ],
        "evidence": evidence,
    }


def _tools_section(
    state: Mapping[str, object], history: Sequence[Mapping[str, object]]
) -> dict[str, object]:
    tool_calls: list[dict[str, object]] = []
    tool_results: list[dict[str, object]] = []
    for entry in history:
        if entry.get("role") == "assistant":
            for call in _list_of_dicts(entry.get("tool_calls")):
                arguments = _parsed_json(str(call.get("arguments_json", "")))
                tool_calls.append(
                    {
                        "call_id": str(call.get("call_id", "")),
                        "name": str(call.get("name", "")),
                        "arguments": dict(arguments) if isinstance(arguments, Mapping) else {},
                    }
                )
        elif entry.get("role") == "tool":
            tool_results.append(
                {
                    "call_id": str(entry.get("tool_call_id", "")),
                    "result": str(entry.get("content", "")),
                }
            )
    committed = [
        {
            "action": str(action.get("action", "")),
            "reference": str(action.get("reference", "")),
            "replayed": bool(action.get("replayed")),
            "idempotency_key": str(action.get("key", "")),
        }
        for action in _list_of_dicts(state.get("committed"))
    ]
    return {"tool_calls": tool_calls, "tool_results": tool_results, "committed": committed}


def _outcome_section(
    state: Mapping[str, object], pending: Mapping[str, object] | None
) -> dict[str, object]:
    if pending is not None:
        status = TurnOutcome.PAUSED
    elif str(state.get("failure", "")):
        status = TurnOutcome.ESCALATED
    elif str(state.get("routing_outcome", "")) == RoutingOutcome.CLARIFY.value:
        status = TurnOutcome.CLARIFIED
    elif not str(state.get("model_name", "")):
        status = TurnOutcome.ABSTAINED
    else:
        status = TurnOutcome.ANSWERED
    failure = str(state.get("failure", ""))
    return {
        "status": status.value,
        "rounds": _int(state.get("rounds")),
        "failure": failure or None,
    }


def _component_manifest(
    state: Mapping[str, object],
    prompt: Mapping[str, object] | None,
    routing: Mapping[str, object] | None,
) -> dict[str, object]:
    """The canonical, content-free component list the turn is pinned to.

    Only versions appear, never content: the rendered prompt's text is the
    content hash in the ``prompt`` section, and this manifest names the
    artifact that hash belongs to. The model port carries no parameters today
    (no temperature or seed is sent), so ``parameters`` is empty by contract
    and grows when `AI-001`'s port does. Parser and chunker versions are pinned
    by the ingestion generation record the ``generation_id`` names, not here.
    """
    ref = prompt.get("template_ref") if prompt else None
    meta = _mapping(state.get("evidence_meta"))
    retriever = (
        {
            "version": meta.get("retriever_version"),
            "reranker": meta.get("reranker"),
            "min_evidence_score": meta.get("min_evidence_score"),
            "embedding_model": meta.get("embedding_model"),
            "generation_id": meta.get("generation_id"),
            "parameters": _mapping(meta.get("parameters")),
            "filters": _mapping(meta.get("filters")),
            "budget": _mapping(meta.get("budget")),
        }
        if meta
        else None
    )
    return {
        "graph": GRAPH_VERSION,
        "prompt_template": {"ref": str(ref) if ref else DISPATCH_SYSTEM_REF},
        "routing_policy": (
            str(routing.get("policy_version", "")) if routing else ROUTING_POLICY_VERSION
        ),
        "agents": AGENTS_VERSION,
        "tools": TOOLS_VERSION,
        "retriever": retriever,
        "model": {"id": str(state.get("model_name", "")) or None, "parameters": {}},
    }


def _published_output(history: Sequence[Mapping[str, object]]) -> tuple[str, tuple[str, ...]]:
    """The model's raw output for the published answer and its citation claims.

    Scans to the most recent assistant message with content — the same scan
    finalize uses — and returns it with the ``[evidence:...]`` markers intact,
    so the trace holds both what the model wrote and what the validator parsed.
    """
    for entry in reversed(history):
        if entry.get("role") == "user":
            break
        content = str(entry.get("content", ""))
        if entry.get("role") == "assistant" and content.strip():
            return content, citation_ids(content)
    return "", ()


def _latest_user_message(history: Sequence[Mapping[str, object]]) -> str:
    for entry in reversed(history):
        if entry.get("role") == "user":
            return str(entry.get("content", ""))
    return ""


def _parsed_json(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _mapping(value: object) -> dict[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _list_of_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _list_of_lists(value: object) -> list[list[object]]:
    if not isinstance(value, list):
        return []
    return [list(item) for item in value if isinstance(item, list)]


def _list_of_str(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]
