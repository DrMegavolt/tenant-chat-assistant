"""Scoring: recall@k, citation precision, abstention correctness.

Three scores make up the minimum scoreboard (`RAG-009`):

- **recall@k** — the fraction of a case's labelled gold chunks the retriever
  returned. Undefined for cases with no gold chunks; those are excluded from
  the aggregate.
- **citation precision** — of the citations a case declares, the fraction
  that reference a chunk the retriever actually returned *and* that is in the
  gold set. A fabricated citation therefore scores zero by construction.
- **abstention correctness** — the fraction of cases where the decision
  boundary (no retrieved chunk at or above the threshold) agrees with the
  case's ``expect_abstain`` label.

The cross-tenant check is a hard invariant, not a score: a retriever that
leaks another tenant's chunks fails the run regardless of thresholds.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from evals.corpus import FixtureCorpus
from evals.retriever import RetrievalResult, RetrieverConfig


@dataclass(frozen=True, slots=True)
class EvalCase:
    """One hand-labelled evaluation case from ``cases.json``."""

    id: str
    tenant_id: str
    query: str
    gold_chunk_ids: tuple[str, ...]
    expect_abstain: bool
    citations: tuple[str, ...]
    scenario: str | None

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
    cross_tenant_leaks: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "case": self.case.id,
            "scenario": self.case.scenario,
            "retrieved": list(self.retrieved),
            "recall": self.recall,
            "citation_precision": self.citation_precision,
            "abstain_decision": self.abstain_decision,
            "abstain_correct": self.abstain_correct,
            "cross_tenant_leaks": list(self.cross_tenant_leaks),
        }


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
    cases: tuple[CaseResult, ...]
    aggregate: dict[str, float]
    passed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "components": {
                "retriever": {
                    "name": self.retriever.name,
                    "version": self.retriever.version,
                    "k": self.retriever.k,
                },
                "embedding_model": self.embedding_model,
                "reranker": self.reranker,
                "prompt_template": self.prompt_template,
                "abstain_threshold": self.abstain_threshold,
                "min_recall": self.min_recall,
                "min_citation_precision": self.min_citation_precision,
                "min_abstention": self.min_abstention,
            },
            "scores": self.aggregate,
            "passed": self.passed,
            "cases": [case.to_dict() for case in self.cases],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_text(self) -> str:
        """A compact, diffable summary: identical output for identical input."""
        lines = [
            f"retriever: {self.retriever.name}@{self.retriever.version} k={self.retriever.k}",
            f"embedding_model: {self.embedding_model}",
            f"reranker: {self.reranker or 'none'}",
            f"abstain_threshold: {self.abstain_threshold}",
            f"recall@{self.retriever.k}: {self.aggregate['recall_at_k']:.4f}",
            f"citation_precision: {self.aggregate['citation_precision']:.4f}",
            f"abstention_correctness: {self.aggregate['abstention_correctness']:.4f}",
            f"cross_tenant_leaks: {self.aggregate['cross_tenant_leaks']:.0f}",
        ]
        for case in self.cases:
            recall = "  -" if case.recall is None else f"{case.recall:.2f}"
            citation = (
                "  -" if case.citation_precision is None else f"{case.citation_precision:.2f}"
            )
            scenario = f" [{case.case.scenario}]" if case.case.scenario else ""
            lines.append(
                f"{case.case.id}: recall={recall} citation={citation} "
                f"abstain={'yes' if case.abstain_decision else 'no'} "
                f"({'correct' if case.abstain_correct else 'WRONG'}){scenario}"
            )
        return "\n".join(lines)


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
                cross_tenant_leaks=leaks,
            )
        )
    recalls = [result.recall for result in case_results if result.recall is not None]
    citations = [
        result.citation_precision
        for result in case_results
        if result.citation_precision is not None
    ]
    aggregate = {
        "recall_at_k": _mean(recalls),
        "citation_precision": _mean(citations),
        "abstention_correctness": _mean(
            [1.0 if result.abstain_correct else 0.0 for result in case_results]
        ),
        "cross_tenant_leaks": float(sum(len(result.cross_tenant_leaks) for result in case_results)),
    }
    passed = (
        aggregate["cross_tenant_leaks"] == 0
        and aggregate["recall_at_k"] >= min_recall
        and aggregate["citation_precision"] >= min_citation_precision
        and aggregate["abstention_correctness"] >= min_abstention
    )
    return EvaluationReport(
        retriever=retriever_config,
        embedding_model=corpus.embedding_model,
        reranker=None,
        prompt_template=None,
        abstain_threshold=abstain_threshold,
        min_recall=min_recall,
        min_citation_precision=min_citation_precision,
        min_abstention=min_abstention,
        cases=tuple(case_results),
        aggregate=aggregate,
        passed=passed,
    )


def _abstain(results: Sequence[RetrievalResult], threshold: float) -> bool:
    """Abstain when nothing retrieved reaches the relevance threshold."""
    return all(result.score < threshold for result in results)


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)
