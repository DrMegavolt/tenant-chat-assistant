"""Claim-grounding scoring reuses the validator the online path runs (`RAG-005`).

The scorer calls ``tenantchat.core.claims.validate_sensitive_claims`` — the
exact function the request-time answer path calls — so the property gated in
CI and the property enforced at request time cannot drift: a fabricated price
fails the dataset and the release gate exactly when it fails a customer
request.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from evals.corpus import FixtureCorpus
from evals.dataset import load_dataset
from evals.retriever import LexicalOverlapRetriever, baseline_config
from evals.runner import run_evaluation
from evals.scorer import EvalCase, EvaluationReport, validate_sensitive_claims
from tenantchat.core import claims as core_claims


def _run(
    cases: tuple[EvalCase, ...], corpus_file: str | None = "evals/datasets/adversarial-corpus.json"
) -> EvaluationReport:
    async def scenario() -> EvaluationReport:
        corpus = await FixtureCorpus.load(Path(corpus_file) if corpus_file else None)
        return await run_evaluation(
            retriever=LexicalOverlapRetriever(corpus),
            retriever_config=baseline_config(k=5),
            corpus=corpus,
            cases=cases,
            abstain_threshold_value=0.5,
            min_recall=0.6,
            min_citation_precision=0.8,
            min_abstention=0.9,
        )

    return asyncio.run(scenario())


class TestValidatorIdentity(unittest.TestCase):
    """One validator, one contract: the scorer must not reimplement RAG-005."""

    def test_the_scorer_uses_the_online_validator_function(self) -> None:
        self.assertIs(
            validate_sensitive_claims,
            core_claims.validate_sensitive_claims,
            "scorer must import, not copy, the online claim validator",
        )


class TestGroundingScores(unittest.TestCase):
    """The grounded-answer dimension on the adversarial dataset."""

    def setUp(self) -> None:
        self.cases = tuple(
            case for case in load_dataset("adversarial-v1").cases if case.answer is not None
        )

    def test_grounding_cases_are_scored(self) -> None:
        report = _run(self.cases)
        scored = [row for row in report.cases if row.grounding_correct is not None]
        self.assertEqual(len(scored), len(self.cases))
        for row in scored:
            self.assertTrue(row.grounding_correct, row.case.id)

    def test_aggregate_grounding_correctness_is_perfect_on_the_fixture(self) -> None:
        report = _run(self.cases)
        self.assertEqual(report.aggregate["grounding_correctness"], 1.0)
        self.assertTrue(report.passed)

    def test_the_verdict_pair_moves_with_the_answer_not_the_query(self) -> None:
        corpus = asyncio.run(FixtureCorpus.load(Path("evals/datasets/adversarial-corpus.json")))
        evidence = (corpus.chunk_text("adv-legitimate-terms") or "",)
        self.assertEqual(
            core_claims.validate_sensitive_claims(
                "HVAC diagnostic visits are $120 per visit.", evidence_texts=evidence
            ).verdict,
            core_claims.ClaimVerdict.SUPPORTED,
        )
        self.assertEqual(
            core_claims.validate_sensitive_claims(
                "An HVAC diagnostic visit costs $89.", evidence_texts=evidence
            ).verdict,
            core_claims.ClaimVerdict.UNSUPPORTED,
        )

    def test_a_dataset_without_grounding_cases_is_not_failed_by_the_dimension(self) -> None:
        golden = tuple(case for case in load_dataset("golden-v1").cases)
        report = _run(golden, corpus_file=None)
        self.assertEqual(report.aggregate["grounding_correctness"], 0.0)
        self.assertTrue(report.passed, "an unscored dimension must not fail the run")

    def test_grounding_scoring_is_deterministic(self) -> None:
        self.assertEqual(_run(self.cases).to_json(), _run(self.cases).to_json())


if __name__ == "__main__":
    unittest.main()
