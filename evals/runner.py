"""The golden offline evaluation runner (`RAG-009`).

Runs the labelled cases through the configured retriever, scores recall@k,
citation precision, and abstention correctness, prints a diffable summary
with the pinned component versions, and exits non-zero when a score is below
its threshold. Hermetic: no network, database, LLM, or embedding service.

Usage::

    uv run --frozen python -m evals.runner [--k 5] [--retriever lexical-overlap]

Thresholds come from ``RAG_EVAL_MIN_RECALL_AT_K``, ``RAG_EVAL_MIN_CITATION_PRECISION``,
and ``RAG_EVAL_MIN_ABSTENTION_CORRECTNESS`` (defaults: 0.6 / 0.8 / 0.9).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from evals.corpus import FixtureCorpus
from evals.retriever import (
    LexicalOverlapRetriever,
    RetrievalResult,
    Retriever,
    RetrieverConfig,
    baseline_config,
)
from evals.scorer import EvalCase, EvaluationReport, score_cases
from evals.versions import prompt_template_manifest

_FIXTURES = Path(__file__).parent / "fixtures"
_RETRIEVERS: dict[str, Callable[[FixtureCorpus], Retriever]] = {
    "lexical-overlap": LexicalOverlapRetriever,
}


def load_cases(path: Path | None = None) -> tuple[EvalCase, ...]:
    """Load the labelled cases; the threshold lives in the fixture header."""
    raw = json.loads((path or _FIXTURES / "cases.json").read_text())
    return tuple(EvalCase.from_json(item) for item in raw["cases"])


def abstain_threshold(path: Path | None = None) -> float:
    raw = json.loads((path or _FIXTURES / "cases.json").read_text())
    return float(raw.get("abstain_threshold", 0.25))


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
    cases = load_cases()
    retriever = _RETRIEVERS[args.retriever](corpus)
    config = baseline_config(k=args.k)
    report = asyncio.run(
        run_evaluation(
            retriever=retriever,
            retriever_config=config,
            corpus=corpus,
            cases=cases,
            abstain_threshold_value=abstain_threshold(),
            min_recall=_env_float("RAG_EVAL_MIN_RECALL_AT_K", 0.6),
            min_citation_precision=_env_float("RAG_EVAL_MIN_CITATION_PRECISION", 0.8),
            min_abstention=_env_float("RAG_EVAL_MIN_ABSTENTION_CORRECTNESS", 0.9),
        )
    )
    sys.stdout.write(report.to_text() + "\n\n")
    sys.stdout.write(
        json.dumps(
            {"components": prompt_template_manifest(), "scores": report.aggregate},
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    if not report.passed:
        sys.stderr.write("evaluation FAILED: a score is below its threshold\n")
        return 1
    sys.stdout.write("evaluation passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
