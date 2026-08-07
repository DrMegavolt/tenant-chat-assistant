"""The golden offline evaluation runner (`RAG-009`, grown by `RAG-008`).

Runs the labelled cases of one versioned dataset through the configured
retriever, scores recall@k, citation precision, abstention correctness, and
claim grounding, prints a diffable summary with the pinned component
versions, and exits non-zero when a score is below its threshold. Hermetic:
no network, database, LLM, or embedding service.

Usage::

    uv run --frozen python -m evals.runner [--dataset golden-v1] [--k 5]
        [--retriever lexical-overlap|hybrid]

Thresholds come from ``RAG_EVAL_MIN_RECALL_AT_K``,
``RAG_EVAL_MIN_CITATION_PRECISION``, ``RAG_EVAL_MIN_ABSTENTION_CORRECTNESS``,
and ``RAG_EVAL_MIN_GROUNDING_CORRECTNESS`` (defaults: 0.6 / 0.8 / 0.9 / 0.9),
overriding the dataset manifest's own defaults. ``RAG-004`` plugs the hybrid
retriever in here; its abstention boundary is the calibrated value, which the
fixture's ``abstain_threshold`` documents. The release gate that compares two
runs lives in ``evals.gate``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from evals.corpus import FixtureCorpus
from evals.dataset import DatasetSpec, load_dataset
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
    HybridRetrieverConfig,
    RetrievalFilters,
    calibrate_min_evidence,
)
from tenantchat.api.search import ScriptedEmbedder
from tenantchat.core.planning import ConversationTurn, RetrievalPlan, plan_query

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


def _baseline_entry(
    corpus: FixtureCorpus, k: int, *, cases: Sequence[EvalCase], abstain_threshold_value: float
) -> RetrieverEntry:
    return RetrieverEntry(
        retriever=LexicalOverlapRetriever(corpus),
        config=baseline_config(k=k),
        abstain_threshold=abstain_threshold_value,
        reranker=None,
    )


async def _hybrid_entry(
    corpus: FixtureCorpus,
    k: int,
    *,
    cases: Sequence[EvalCase],
    abstain_threshold_value: float,
    vocabulary: Mapping[str, Sequence[str]] | None = None,
) -> RetrieverEntry:
    """Build the hybrid with its threshold calibrated from the dataset's cases.

    Calibration reads only the dataset's known-relevant pairs (never the
    ``expect_abstain`` labels), so abstention correctness stays an independent
    measurement of the derived boundary. Multi-turn cases calibrate on the
    *resolved* queries (`RAG-006`): a raw pronoun scores nothing against its
    gold chunk, and calibrating on it would set the boundary to zero. ``cases``
    travels from the selected dataset — calibrating against the golden fixtures
    while scoring another dataset would measure the wrong boundary.
    """
    config = HybridRetrieverConfig(k=k)
    embedder = ScriptedEmbedder(model=corpus.embedding_model)
    resolved = resolve_multiturn(cases, vocabulary)
    relevant_sets = tuple(
        (
            resolved[case.id].query if case.id in resolved else case.query,
            RetrievalFilters(tenant_id=case.tenant_id),
            case.gold_chunk_ids,
        )
        for case in cases
        if case.gold_chunk_ids
    )
    record = await calibrate_min_evidence(
        embedder=embedder, chunks=corpus.chunks, relevant_sets=relevant_sets, config=config
    )
    calibrated = replace(config, min_evidence_score=record.min_evidence, calibration=record)
    return RetrieverEntry(
        retriever=HybridRetriever(corpus, calibrated),
        config=hybrid_config(k=k, config=calibrated),
        abstain_threshold=record.min_evidence,
        reranker=RERANKER_NAME if calibrated.rerank else None,
    )


def resolve_multiturn(
    cases: Sequence[EvalCase],
    vocabulary: Mapping[str, Sequence[str]] | None,
) -> dict[str, RetrievalPlan]:
    """Plan a standalone query for every multi-turn case (`RAG-006`).

    A case with prior turns is resolved against them with the deterministic
    planner; a single-turn case has no plan and is scored as written. The
    resolved queries feed both retrieval and hybrid calibration, so the
    abstention boundary is calibrated on what actually gets retrieved.
    """
    plans: dict[str, RetrievalPlan] = {}
    for case in cases:
        if not case.prior_turns:
            continue
        plans[case.id] = plan_query(
            case.query,
            tenant_id=case.tenant_id,
            history=_prior_turns(case.prior_turns),
            known_terms=tuple(vocabulary.get(case.tenant_id, ()) if vocabulary else ()),
        )
    return plans


def _prior_turns(prior_turns: Sequence[str]) -> tuple[ConversationTurn, ...]:
    """Parse ``Customer:``/``Assistant:``-prefixed prior turns for the planner."""
    turns: list[ConversationTurn] = []
    for raw in prior_turns:
        role, content = _split_turn(raw)
        turns.append(ConversationTurn(role=role, content=content))
    return tuple(turns)


def _split_turn(raw: str) -> tuple[str, str]:
    """The role prefix of one prior turn, if it carries one; user otherwise."""
    text = raw.strip()
    lowered = text.casefold()
    for prefix, role in (("customer:", "user"), ("user:", "user"), ("assistant:", "assistant")):
        if lowered.startswith(prefix):
            return role, text[len(prefix) :].strip()
    return "user", text


async def build_retriever_entry_async(
    name: str,
    corpus: FixtureCorpus,
    k: int,
    *,
    cases: Sequence[EvalCase] | None = None,
    abstain_threshold_value: float = 0.5,
    vocabulary: Mapping[str, Sequence[str]] | None = None,
) -> RetrieverEntry:
    """The async entry builder, for callers already inside an event loop.

    ``cases``, ``abstain_threshold_value``, and ``vocabulary`` feed the hybrid
    calibration and the lexical boundary respectively. ``None`` keeps the
    RAG-009 contract: the hybrid calibrates against the golden fixtures, which
    is what the original harness tests assert.
    """
    resolved = load_cases() if cases is None else cases
    if name == "hybrid":
        return await _hybrid_entry(
            corpus,
            k,
            cases=resolved,
            abstain_threshold_value=abstain_threshold_value,
            vocabulary=vocabulary,
        )
    if name == "lexical-overlap":
        return _baseline_entry(
            corpus, k, cases=resolved, abstain_threshold_value=abstain_threshold_value
        )
    raise ValueError(f"unknown retriever {name!r}; choose from lexical-overlap, hybrid")


def build_retriever_entry(
    name: str,
    corpus: FixtureCorpus,
    k: int,
    *,
    cases: Sequence[EvalCase] | None = None,
    abstain_threshold_value: float = 0.5,
    vocabulary: Mapping[str, Sequence[str]] | None = None,
) -> RetrieverEntry:
    """The synchronous entry builder for the CLI and sync tests."""
    return asyncio.run(
        build_retriever_entry_async(
            name,
            corpus,
            k,
            cases=cases,
            abstain_threshold_value=abstain_threshold_value,
            vocabulary=vocabulary,
        )
    )


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
    min_grounding: float = 0.9,
    parser_chunker: str | None = None,
    tenant_policy: str | None = None,
    vocabulary: Mapping[str, Sequence[str]] | None = None,
) -> EvaluationReport:
    """Retrieve every case and score the run against the thresholds.

    Multi-turn cases are resolved into standalone queries before retrieval, and
    the report rows carry the resolved query and plan mode so a reviewer can
    see what was actually scored (`RAG-006`).
    """
    plans = resolve_multiturn(cases, vocabulary)
    retrieved_by_case: dict[str, Sequence[RetrievalResult]] = {}
    for case in cases:
        plan = plans.get(case.id)
        query = plan.query if plan is not None else case.query
        retrieved_by_case[case.id] = await retriever.retrieve(
            query, tenant_id=case.tenant_id, k=retriever_config.k
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
        min_grounding=min_grounding,
        parser_chunker=parser_chunker,
        tenant_policy=tenant_policy,
        resolved_queries={case_id: plan.query for case_id, plan in plans.items()},
        plan_modes={case_id: plan.mode.value for case_id, plan in plans.items()},
    )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def resolve_dataset(dataset: str, k: int) -> tuple[DatasetSpec, FixtureCorpus]:
    """Load a dataset and its corpus; the shared setup of runner and gate."""
    spec = load_dataset(dataset)
    corpus_path = None
    if spec.corpus_file is not None:
        corpus_path = Path(__file__).parent / "datasets" / spec.corpus_file
    corpus = asyncio.run(FixtureCorpus.load(corpus_path))
    return spec, corpus


def dataset_thresholds(
    spec: DatasetSpec,
    *,
    recall: float | None = None,
    citation: float | None = None,
    abstention: float | None = None,
    grounding: float | None = None,
) -> tuple[float, float, float, float]:
    """The run's thresholds: dataset-manifest defaults overridden by env.

    Environment wins over the manifest so a release can tighten a threshold
    without editing a dataset.
    """
    defaults = spec.thresholds
    return (
        recall
        if recall is not None
        else _env_float("RAG_EVAL_MIN_RECALL_AT_K", float(defaults.get("recall_at_k", 0.6))),
        citation
        if citation is not None
        else _env_float(
            "RAG_EVAL_MIN_CITATION_PRECISION", float(defaults.get("citation_precision", 0.8))
        ),
        abstention
        if abstention is not None
        else _env_float(
            "RAG_EVAL_MIN_ABSTENTION_CORRECTNESS",
            float(defaults.get("abstention_correctness", 0.9)),
        ),
        grounding
        if grounding is not None
        else _env_float(
            "RAG_EVAL_MIN_GROUNDING_CORRECTNESS", float(defaults.get("grounding_correctness", 0.9))
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dataset",
        default="golden-v1",
        help="the versioned dataset to score (evals/datasets/*.json)",
    )
    parser.add_argument("--k", type=int, default=5, help="retrieval depth for recall@k")
    parser.add_argument(
        "--retriever",
        choices=("lexical-overlap", "hybrid"),
        default="lexical-overlap",
        help="retriever under test (RAG-004 plugs the hybrid retriever in here)",
    )
    args = parser.parse_args()

    spec, corpus = resolve_dataset(args.dataset, args.k)
    entry = build_retriever_entry(
        args.retriever,
        corpus,
        args.k,
        cases=spec.cases,
        abstain_threshold_value=spec.abstain_threshold,
        vocabulary=spec.vocabulary,
    )
    min_recall, min_citation, min_abstention, min_grounding = dataset_thresholds(spec)
    report = asyncio.run(
        run_evaluation(
            retriever=entry.retriever,
            retriever_config=entry.config,
            corpus=corpus,
            cases=spec.cases,
            abstain_threshold_value=entry.abstain_threshold,
            min_recall=min_recall,
            min_citation_precision=min_citation,
            min_abstention=min_abstention,
            reranker=entry.reranker,
            min_grounding=min_grounding,
            parser_chunker=spec.parser_chunker,
            tenant_policy=spec.tenant_policy,
            vocabulary=spec.vocabulary,
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
