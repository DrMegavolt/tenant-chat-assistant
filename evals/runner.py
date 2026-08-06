"""The golden offline evaluation runner (`RAG-009`).

Runs the labelled cases through the configured retriever, scores recall@k,
citation precision, and abstention correctness, prints a diffable summary
with the pinned component versions, and exits non-zero when a score is below
its threshold. Hermetic: no network, database, LLM, or embedding service.

Usage::

    uv run --frozen python -m evals.runner [--k 5] [--retriever lexical-overlap|hybrid]

Thresholds come from ``RAG_EVAL_MIN_RECALL_AT_K``, ``RAG_EVAL_MIN_CITATION_PRECISION``,
and ``RAG_EVAL_MIN_ABSTENTION_CORRECTNESS`` (defaults: 0.6 / 0.8 / 0.9).
``RAG-004`` plugs the hybrid retriever in here; its abstention boundary is the
calibrated value, which the fixture's ``abstain_threshold`` documents.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from evals.corpus import FixtureCorpus
from evals.retriever import (
    HybridRetriever,
    LexicalOverlapRetriever,
    RetrievalResult,
    Retriever,
    RetrieverConfig,
    baseline_config,
    hybrid_config,
)
from evals.scorer import EvalCase, EvaluationReport, score_cases
from tenantchat.api.retrieval import (
    RERANKER_NAME,
    CalibrationRecord,
    HybridRetrieverConfig,
    RetrievalFilters,
    calibrate_min_evidence,
)
from tenantchat.api.search import ScriptedEmbedder

_FIXTURES = Path(__file__).parent / "fixtures"


@dataclass(frozen=True, slots=True)
class RetrieverEntry:
    """One runnable retriever plus the report it pins."""

    retriever: Retriever
    config: RetrieverConfig
    abstain_threshold: float
    reranker: str | None


def load_cases(path: Path | None = None) -> tuple[EvalCase, ...]:
    """Load the labelled cases; the threshold lives in the fixture header."""
    raw = json.loads((path or _FIXTURES / "cases.json").read_text())
    return tuple(EvalCase.from_json(item) for item in raw["cases"])


def abstain_threshold(path: Path | None = None) -> float:
    raw = json.loads((path or _FIXTURES / "cases.json").read_text())
    return float(raw.get("abstain_threshold", 0.25))


def _baseline_entry(corpus: FixtureCorpus, k: int) -> RetrieverEntry:
    return RetrieverEntry(
        retriever=LexicalOverlapRetriever(corpus),
        config=baseline_config(k=k),
        abstain_threshold=abstain_threshold(),
        reranker=None,
    )


def _hybrid_entry(corpus: FixtureCorpus, k: int) -> RetrieverEntry:
    """Build the hybrid with its threshold calibrated from the golden cases.

    Calibration reads only the cases' known-relevant pairs (never the
    ``expect_abstain`` labels), so abstention correctness stays an independent
    measurement of the derived boundary.
    """
    cases = load_cases()
    config = HybridRetrieverConfig(k=k)

    async def calibrate() -> tuple[HybridRetrieverConfig, CalibrationRecord]:
        embedder = ScriptedEmbedder(model=corpus.embedding_model)
        relevant_sets = tuple(
            (case.query, RetrievalFilters(tenant_id=case.tenant_id), case.gold_chunk_ids)
            for case in cases
            if case.gold_chunk_ids
        )
        record = await calibrate_min_evidence(
            embedder=embedder, chunks=corpus.chunks, relevant_sets=relevant_sets, config=config
        )
        return replace(config, min_evidence_score=record.min_evidence, calibration=record), record

    calibrated, record = asyncio.run(calibrate())
    return RetrieverEntry(
        retriever=HybridRetriever(corpus, calibrated),
        config=hybrid_config(k=k, config=calibrated),
        abstain_threshold=record.min_evidence,
        reranker=RERANKER_NAME if calibrated.rerank else None,
    )


_RETRIEVERS: dict[str, Callable[[FixtureCorpus, int], RetrieverEntry]] = {
    "lexical-overlap": _baseline_entry,
    "hybrid": _hybrid_entry,
}


def build_retriever_entry(name: str, corpus: FixtureCorpus, k: int) -> RetrieverEntry:
    """The runnable entry for a registered retriever, for the CLI and tests."""
    try:
        builder = _RETRIEVERS[name]
    except KeyError:
        raise ValueError(f"unknown retriever {name!r}; choose from {sorted(_RETRIEVERS)}") from None
    return builder(corpus, k)


async def run_evaluation(
    *,
    retriever: Retriever,
    retriever_config: RetrieverConfig,
    corpus: FixtureCorpus,
    cases: Sequence[EvalCase],
    abstain_threshold_value: float,
    min_recall: float,
    min_citation_precision: float,
    min_abstention: float,
    reranker: str | None = None,
) -> EvaluationReport:
    """Retrieve every case and score the run against the thresholds."""
    retrieved_by_case: dict[str, Sequence[RetrievalResult]] = {}
    for case in cases:
        retrieved_by_case[case.id] = await retriever.retrieve(
            case.query, tenant_id=case.tenant_id, k=retriever_config.k
        )
    return score_cases(
        corpus=corpus,
        retriever_config=retriever_config,
        cases=cases,
        retrieved_by_case=retrieved_by_case,
        abstain_threshold=abstain_threshold_value,
        min_recall=min_recall,
        min_citation_precision=min_citation_precision,
        min_abstention=min_abstention,
        reranker=reranker,
    )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--k", type=int, default=5, help="retrieval depth for recall@k")
    parser.add_argument(
        "--retriever",
        choices=sorted(_RETRIEVERS),
        default="lexical-overlap",
        help="retriever under test (RAG-004 plugs the hybrid retriever in here)",
    )
    args = parser.parse_args()

    corpus = asyncio.run(FixtureCorpus.load())
    entry = build_retriever_entry(args.retriever, corpus, args.k)
    report = asyncio.run(
        run_evaluation(
            retriever=entry.retriever,
            retriever_config=entry.config,
            corpus=corpus,
            cases=load_cases(),
            abstain_threshold_value=entry.abstain_threshold,
            min_recall=_env_float("RAG_EVAL_MIN_RECALL_AT_K", 0.6),
            min_citation_precision=_env_float("RAG_EVAL_MIN_CITATION_PRECISION", 0.8),
            min_abstention=_env_float("RAG_EVAL_MIN_ABSTENTION_CORRECTNESS", 0.9),
            reranker=entry.reranker,
        )
    )
    sys.stdout.write(report.to_text() + "\n\n")
    sys.stdout.write(report.to_json() + "\n")
    if not report.passed:
        sys.stderr.write("evaluation FAILED: a score is below its threshold\n")
        return 1
    sys.stdout.write("evaluation passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
