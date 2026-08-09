"""Scoring: recall@k, citation precision, abstention correctness, grounding.

Four scores make up the scoreboard (`RAG-009` plus the `RAG-008` release gate):

- **recall@k** — the fraction of a case's labelled gold chunks the retriever
  returned. Undefined for cases with no gold chunks; those are excluded from
  the aggregate.
- **citation precision** — of the citations a case declares, the fraction
  that reference a chunk the retriever actually returned *and* that is in the
  gold set. A fabricated citation therefore scores zero by construction.
- **abstention correctness** — the fraction of cases where the decision
  boundary (no retrieved chunk at or above the threshold) agrees with the
  case's ``expect_abstain`` label.
- **grounding correctness** — the fraction of grounded-answer cases where
  :func:`validate_sensitive_claims` (the exact validator `RAG-005` runs
  online) agrees with the ``expect_grounded`` label, so the property gated in
  CI is the property enforced at request time.

The cross-tenant check is a hard invariant, not a score: a retriever that
leaks another tenant's chunks fails the run regardless of thresholds. A
dimension that no case exercises is not scored and cannot fail the run.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from evals.corpus import FixtureCorpus
from evals.retriever import RetrievalResult, RetrieverConfig
from evals.versions import component_manifest, corpus_digest
from tenantchat.core.claims import (
    ClaimVerdict,
)
from tenantchat.core.claims import (
    validate_sensitive_claims as validate_sensitive_claims,
)

_GROUNDING_THRESHOLD = 0.9


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One labelled evaluation case from a versioned dataset."""

    id: str
    tenant_id: str
    query: str
    gold_chunk_ids: tuple[str, ...]
    expect_abstain: bool
    citations: tuple[str, ...]
    scenario: str | None
    prior_turns: tuple[str, ...] = ()
    answer: str | None = None
    expect_grounded: bool | None = None
    review_id: str | None = None
    trace_id: str | None = None
    turn_id: str | None = None

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> EvalCase:
        return cls(
            id=str(raw["id"]),
            tenant_id=str(raw["tenant_id"]),
            query=str(raw["query"]),
            gold_chunk_ids=tuple(str(item) for item in raw["gold_chunk_ids"]),
            expect_abstain=bool(raw["expect_abstain"]),
            citations=tuple(str(item) for item in raw["citations"]),
            scenario=None if raw.get("scenario") is None else str(raw["scenario"]),
            prior_turns=tuple(str(item) for item in raw.get("prior_turns", ())),
            answer=None if raw.get("answer") is None else str(raw["answer"]),
            expect_grounded=(
                None if raw.get("expect_grounded") is None else bool(raw["expect_grounded"])
            ),
            review_id=None if raw.get("review_id") is None else str(raw["review_id"]),
            trace_id=None if raw.get("trace_id") is None else str(raw["trace_id"]),
            turn_id=None if raw.get("turn_id") is None else str(raw["turn_id"]),
        )


@dataclass(frozen=True, slots=True)
class CaseResult:
    """One case's scored outcome, kept case-level so regressions are local."""

    case: EvalCase
    retrieved: tuple[str, ...]
    recall: float | None
    citation_precision: float | None
    abstain_decision: bool
    abstain_correct: bool
    grounding_correct: bool | None
    cross_tenant_leaks: tuple[str, ...]
    resolved_query: str | None = None
    plan_mode: str | None = None

    def to_dict(self) -> dict[str, object]:
        row: dict[str, object] = {
            "case": self.case.id,
            "scenario": self.case.scenario,
            "retrieved": list(self.retrieved),
            "recall": self.recall,
            "citation_precision": self.citation_precision,
            "abstain_decision": self.abstain_decision,
            "abstain_correct": self.abstain_correct,
            "grounding_correct": self.grounding_correct,
            "cross_tenant_leaks": list(self.cross_tenant_leaks),
        }
        if self.case.prior_turns:
            row["prior_turns"] = list(self.case.prior_turns)
        if self.resolved_query is not None:
            row["resolved_query"] = self.resolved_query
        if self.plan_mode is not None:
            row["plan_mode"] = self.plan_mode
        for field in ("review_id", "trace_id", "turn_id"):
            value = getattr(self.case, field)
            if value is not None:
                row[field] = value
        return row


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """A fully deterministic run report: versions, scores, per-case rows."""

    retriever: RetrieverConfig
    embedding_model: str
    reranker: str | None
    prompt_template: dict[str, object] | None
    abstain_threshold: float
    min_recall: float
    min_citation_precision: float
    min_abstention: float
    min_grounding: float
    cases: tuple[CaseResult, ...]
    aggregate: dict[str, float]
    scored: dict[str, bool]
    components: dict[str, object]
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "components": dict(self.components),
            "scores": self.aggregate,
            "scored": self.scored,
            "passed": self.passed,
            "cases": [case.to_dict() for case in self.cases],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_text(self) -> str:
        """A compact, diffable summary: identical output for identical input."""
        lines = [
            f"retriever: {self.retriever.name}@{self.retriever.version} k={self.retriever.k}",
            f"retriever_params: {json.dumps(dict(self.retriever.parameters), sort_keys=True)}",
            f"embedding_model: {self.embedding_model}",
            f"reranker: {self.reranker or 'none'}",
            f"abstain_threshold: {self.abstain_threshold}",
            _score_line(self, f"recall@{self.retriever.k}", "recall_at_k"),
            _score_line(self, "citation_precision", "citation_precision"),
            f"abstention_correctness: {self.aggregate['abstention_correctness']:.4f}",
            _score_line(self, "grounding_correctness", "grounding_correctness"),
            f"cross_tenant_leaks: {self.aggregate['cross_tenant_leaks']:.0f}",
        ]
        for case in self.cases:
            recall = "  -" if case.recall is None else f"{case.recall:.2f}"
            citation = (
                "  -" if case.citation_precision is None else f"{case.citation_precision:.2f}"
            )
            grounding = "  -" if case.grounding_correct is None else f"{case.grounding_correct}"
            scenario = f" [{case.case.scenario}]" if case.case.scenario else ""
            lines.append(
                f"{case.case.id}: recall={recall} citation={citation} grounding={grounding} "
                f"abstain={'yes' if case.abstain_decision else 'no'} "
                f"({'correct' if case.abstain_correct else 'WRONG'}){scenario}"
            )
        return "\n".join(lines)


def _score_line(report: EvaluationReport, label: str, metric: str) -> str:
    """Render absent dimensions as such instead of a misleading numeric zero."""
    if not report.scored.get(metric, True):
        return f"{label}: n/a (unscored)"
    return f"{label}: {report.aggregate[metric]:.4f}"


def score_cases(
    *,
    corpus: FixtureCorpus,
    retriever_config: RetrieverConfig,
    cases: Sequence[EvalCase],
    retrieved_by_case: dict[str, Sequence[RetrievalResult]],
    abstain_threshold: float,
    min_recall: float,
    min_citation_precision: float,
    min_abstention: float,
    reranker: str | None = None,
    min_grounding: float = _GROUNDING_THRESHOLD,
    parser_chunker: str | None = None,
    tenant_policy: str | None = None,
    resolved_queries: Mapping[str, str] | None = None,
    plan_modes: Mapping[str, str] | None = None,
) -> EvaluationReport:
    """Score one run and decide whether it meets the configured thresholds."""
    case_results: list[CaseResult] = []
    for case in cases:
        results = tuple(retrieved_by_case[case.id])
        retrieved = tuple(result.chunk_id for result in results)
        gold = frozenset(case.gold_chunk_ids)
        recall = None
        if gold:
            recall = len(frozenset(retrieved) & gold) / len(gold)
        declared = case.citations
        citation_precision = None
        if declared:
            valid = sum(1 for citation in declared if citation in retrieved and citation in gold)
            citation_precision = valid / len(declared)
        leaks = tuple(
            chunk_id
            for chunk_id in retrieved
            if corpus.chunk_tenant(chunk_id) not in (None, case.tenant_id)
        )
        abstain_decision = _abstain(results, abstain_threshold)
        case_results.append(
            CaseResult(
                case=case,
                retrieved=retrieved,
                recall=recall,
                citation_precision=citation_precision,
                abstain_decision=abstain_decision,
                abstain_correct=abstain_decision == case.expect_abstain,
                grounding_correct=_grounding_correct(corpus, case),
                cross_tenant_leaks=leaks,
                resolved_query=None if resolved_queries is None else resolved_queries.get(case.id),
                plan_mode=None if plan_modes is None else plan_modes.get(case.id),
            )
        )
    recalls = [result.recall for result in case_results if result.recall is not None]
    citations = [
        result.citation_precision
        for result in case_results
        if result.citation_precision is not None
    ]
    groundings = [
        result.grounding_correct for result in case_results if result.grounding_correct is not None
    ]
    aggregate = {
        "recall_at_k": _mean(recalls),
        "citation_precision": _mean(citations),
        "abstention_correctness": _mean(
            [1.0 if result.abstain_correct else 0.0 for result in case_results]
        ),
        "grounding_correctness": _mean([1.0 if value else 0.0 for value in groundings]),
        "cross_tenant_leaks": float(sum(len(result.cross_tenant_leaks) for result in case_results)),
    }
    # An unscored dimension does not gate: a dataset that never exercises
    # grounding cannot regress it, and an empty aggregate must not read as 0.
    passed = (
        aggregate["cross_tenant_leaks"] == 0
        and (not recalls or aggregate["recall_at_k"] >= min_recall)
        and (not citations or aggregate["citation_precision"] >= min_citation_precision)
        and (not groundings or aggregate["grounding_correctness"] >= min_grounding)
        and aggregate["abstention_correctness"] >= min_abstention
    )
    return EvaluationReport(
        retriever=retriever_config,
        embedding_model=corpus.embedding_model,
        reranker=reranker,
        prompt_template=None,
        abstain_threshold=abstain_threshold,
        min_recall=min_recall,
        min_citation_precision=min_citation_precision,
        min_abstention=min_abstention,
        min_grounding=min_grounding,
        cases=tuple(case_results),
        aggregate=aggregate,
        scored={
            "recall_at_k": bool(recalls),
            "citation_precision": bool(citations),
            "abstention_correctness": True,
            "grounding_correctness": bool(groundings),
            "cross_tenant_leaks": True,
        },
        components=component_manifest(
            retriever=retriever_config,
            embedding_model=corpus.embedding_model,
            reranker=reranker,
            abstain_threshold=abstain_threshold,
            min_recall=min_recall,
            min_citation_precision=min_citation_precision,
            min_abstention=min_abstention,
            min_grounding=min_grounding,
            corpus_chunks=len(corpus.chunks),
            corpus_digest=corpus_digest(corpus),
            parser_chunker=parser_chunker,
            tenant_policy=tenant_policy,
        ),
        passed=passed,
    )


def _grounding_correct(corpus: FixtureCorpus, case: EvalCase) -> bool | None:
    """Score one grounded-answer case with the validator online runs (`RAG-005`).

    Evidence is the gold chunks' texts, the passages the case says should
    have been admitted; ``validate_sensitive_claims`` is the same function the
    request path calls, so a claim the online validator would refuse scores
    the same way here.
    """
    if case.answer is None or case.expect_grounded is None:
        return None
    evidence = tuple(
        text
        for chunk_id in case.gold_chunk_ids
        if (text := corpus.chunk_text(chunk_id)) is not None
    )
    verdict = validate_sensitive_claims(case.answer, evidence_texts=evidence)
    return (verdict.verdict is ClaimVerdict.SUPPORTED) == case.expect_grounded


def _abstain(results: Sequence[RetrievalResult], threshold: float) -> bool:
    """Abstain when nothing retrieved reaches the relevance threshold."""
    return all(result.score < threshold for result in results)


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)
