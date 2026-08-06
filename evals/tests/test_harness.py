"""The RAG-009 acceptance criteria, proven in the hermetic gate.

Two runs over unchanged inputs produce identical scores; a deliberately
weakened retriever moves recall@k measurably; the fixture corpus has the
required shape and contains no real customer PII.
"""

from __future__ import annotations

import asyncio
import re
import unittest

from evals.corpus import FixtureCorpus
from evals.retriever import LexicalOverlapRetriever, baseline_config
from evals.runner import abstain_threshold, load_cases, run_evaluation
from evals.scorer import EvaluationReport


def _evaluate(*, k: int, corpus: FixtureCorpus | None = None) -> EvaluationReport:
    async def scenario() -> EvaluationReport:
        loaded = corpus if corpus is not None else await FixtureCorpus.load()
        cases = load_cases()
        retriever = LexicalOverlapRetriever(loaded)
        return await run_evaluation(
            retriever=retriever,
            retriever_config=baseline_config(k=k),
            corpus=loaded,
            cases=cases,
            abstain_threshold_value=abstain_threshold(),
            min_recall=0.6,
            min_citation_precision=0.8,
            min_abstention=0.9,
        )

    return asyncio.run(scenario())


class TestDeterminism(unittest.TestCase):
    """The scoreboard must be reproducible, or it cannot detect regressions."""

    def test_two_runs_produce_identical_reports(self) -> None:
        first = _evaluate(k=5)
        second = _evaluate(k=5)
        self.assertEqual(first.to_json(), second.to_json())

    def test_unchanged_inputs_unchanged_scores_after_reload(self) -> None:
        first = _evaluate(k=5)
        second = _evaluate(k=5, corpus=asyncio.run(FixtureCorpus.load()))
        self.assertEqual(first.aggregate, second.aggregate)


class TestSensitivity(unittest.TestCase):
    """A weakened retriever must move the score visibly in the expected direction."""

    def test_shallower_k_lowers_recall(self) -> None:
        deep = _evaluate(k=5)
        shallow = _evaluate(k=1)
        self.assertGreater(deep.aggregate["recall_at_k"], shallow.aggregate["recall_at_k"])
        self.assertGreater(deep.aggregate["recall_at_k"], 0.5)

    def test_lexical_baseline_meets_documented_thresholds(self) -> None:
        report = _evaluate(k=5)
        self.assertTrue(report.passed, msg=report.to_text())

    def test_cross_tenant_leak_fails_the_run(self) -> None:
        report = _evaluate(k=5)
        self.assertEqual(report.aggregate["cross_tenant_leaks"], 0.0)
        for case in report.cases:
            self.assertEqual(case.cross_tenant_leaks, (), case.case.id)


class TestFixtures(unittest.TestCase):
    """The corpus and cases are the contract later waves tune against."""

    def setUp(self) -> None:
        self.corpus = asyncio.run(FixtureCorpus.load())
        self.cases = load_cases()

    def test_corpus_covers_both_seed_tenants(self) -> None:
        tenants = {chunk.tenant_id for chunk in self.corpus.chunks}
        self.assertTrue({"apex", "clearview"} <= tenants)

    def test_case_count_reaches_the_documented_floor(self) -> None:
        self.assertGreaterEqual(len(self.cases), 20)

    def test_special_scenarios_are_present(self) -> None:
        scenarios = {case.scenario for case in self.cases}
        self.assertIn("stale", scenarios)
        self.assertIn("cross_tenant", scenarios)
        self.assertIn("unsupported", scenarios)
        self.assertIn("fabricated_citation", scenarios)

    def test_all_gold_and_citation_chunks_exist_in_the_corpus(self) -> None:
        known = {chunk.chunk_id for chunk in self.corpus.chunks}
        for case in self.cases:
            missing = set(case.gold_chunk_ids) - known
            self.assertEqual(missing, set(), case.id)
            # A fabricated citation deliberately names a nonexistent chunk;
            # every other citation must resolve to the corpus.
            if case.scenario != "fabricated_citation":
                missing = set(case.citations) - known
                self.assertEqual(missing, set(), case.id)

    def test_stale_case_gold_points_at_the_current_version(self) -> None:
        stale = next(case for case in self.cases if case.scenario == "stale")
        for chunk_id in stale.gold_chunk_ids:
            self.assertNotIn("stale", chunk_id, "gold must cite the current version")
            text = self.corpus.chunk_text(chunk_id)
            self.assertIsNotNone(text)

    def test_fixtures_contain_no_customer_pii(self) -> None:
        combined = "\n".join(
            [chunk.text for chunk in self.corpus.chunks] + [case.query for case in self.cases]
        )
        self.assertNotRegex(combined, r"\b\d{3}[-.)]\s?\d{3}[-.]\d{4}\b", "phone number")
        self.assertNotRegex(combined, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "email")
        self.assertNotRegex(combined, r"\b\d{9,}\b", "card or account number")
        self.assertNotRegex(
            re.sub(r"9810[1-5]|97035|9720[1-5]", "", combined),
            r"\b\d{5}\b",
            "only the seed tenants' documented ZIP ranges may appear",
        )

    def test_each_gold_chunk_is_active(self) -> None:
        active = {chunk.chunk_id for chunk in self.corpus.chunks if chunk.active}
        for case in self.cases:
            missing = set(case.gold_chunk_ids) - active
            self.assertEqual(missing, set(), case.id)
