"""The multi-turn runner: resolution, calibration, and scoring (`RAG-006`).

These pin the harness contract that RAG-006 adds: a case with prior turns is
resolved into a standalone query with the deterministic planner before the
retriever scores it, the resolved query and plan mode ride the report row, and
the hybrid's abstention boundary is calibrated on the resolved queries (a raw
pronoun scores nothing against its gold chunk). The four case kinds — pronouns,
corrections, topic shifts, and malicious prior turns — are each asserted to
resolve to a query that retrieves the gold chunk and to abstain when the
dataset says so.
"""

from __future__ import annotations

import asyncio
import unittest

from evals.dataset import DatasetSpec
from evals.runner import (
    build_retriever_entry_async,
    resolve_dataset,
    resolve_multiturn,
    run_evaluation,
)
from evals.scorer import EvalCase


def _case(spec: DatasetSpec, case_id: str) -> EvalCase:
    return next(case for case in spec.cases if case.id == case_id)


class TestResolution(unittest.TestCase):
    def setUp(self) -> None:
        self.spec, self.corpus = resolve_dataset("multi-turn-v2", 5)

    def test_pronoun_carryover_resolves_the_referent(self) -> None:
        plans = resolve_multiturn(self.spec.cases, self.spec.vocabulary)
        plan = plans["mt-clearview-other-plan"]
        self.assertEqual(plan.mode.value, "carryover")
        self.assertIn("care plan", plan.entities)
        self.assertNotEqual(plan.query, _case(self.spec, "mt-clearview-other-plan").query)

    def test_a_correction_drops_the_carried_context(self) -> None:
        plans = resolve_multiturn(self.spec.cases, self.spec.vocabulary)
        plan = plans["mt-apex-furnace-correction"]
        self.assertEqual(plan.mode.value, "correction")
        self.assertTrue(plan.reset)
        self.assertEqual(plan.query, "I meant my furnace.")

    def test_a_topic_switch_resets_the_context(self) -> None:
        plans = resolve_multiturn(self.spec.cases, self.spec.vocabulary)
        plan = plans["mt-apex-hours-switch"]
        self.assertEqual(plan.mode.value, "topic_switch")
        self.assertTrue(plan.reset)
        self.assertEqual(plan.query, "What are your hours?")

    def test_a_malicious_prior_turn_is_never_laundered(self) -> None:
        plans = resolve_multiturn(self.spec.cases, self.spec.vocabulary)
        for case_id in (
            "mt-apex-malicious-hours",
            "mt-clearview-malicious-estimate",
            "mt-apex-malicious-warranty",
        ):
            query = plans[case_id].query.casefold()
            for forbidden in ("ignore", "reveal", "price list", "instructions", "everything"):
                self.assertNotIn(forbidden, query, case_id)

    def test_the_report_carries_the_resolved_query_and_mode(self) -> None:
        entry = asyncio.run(
            build_retriever_entry_async(
                "lexical-overlap",
                self.corpus,
                5,
                cases=self.spec.cases,
                abstain_threshold_value=self.spec.abstain_threshold,
            )
        )
        report = asyncio.run(
            run_evaluation(
                retriever=entry.retriever,
                retriever_config=entry.config,
                corpus=self.corpus,
                cases=self.spec.cases,
                abstain_threshold_value=self.spec.abstain_threshold,
                min_recall=0.0,
                min_citation_precision=0.0,
                min_abstention=0.0,
                vocabulary=self.spec.vocabulary,
            )
        )
        row = next(r for r in report.cases if r.case.id == "mt-clearview-other-plan")
        self.assertIsNotNone(row.resolved_query)
        self.assertEqual(row.plan_mode, "carryover")
        self.assertIn("care plan", row.resolved_query or "")

    def test_every_case_retrieves_its_gold_chunk(self) -> None:
        entry = asyncio.run(
            build_retriever_entry_async(
                "hybrid",
                self.corpus,
                5,
                cases=self.spec.cases,
                abstain_threshold_value=self.spec.abstain_threshold,
                vocabulary=self.spec.vocabulary,
            )
        )
        report = asyncio.run(
            run_evaluation(
                retriever=entry.retriever,
                retriever_config=entry.config,
                corpus=self.corpus,
                cases=self.spec.cases,
                abstain_threshold_value=entry.abstain_threshold,
                min_recall=0.0,
                min_citation_precision=0.0,
                min_abstention=0.0,
                reranker=entry.reranker,
                vocabulary=self.spec.vocabulary,
            )
        )
        for row in report.cases:
            if row.case.gold_chunk_ids:
                self.assertEqual(
                    row.recall,
                    1.0,
                    f"{row.case.id} resolved to {row.resolved_query!r}",
                )
            self.assertTrue(row.abstain_correct, row.case.id)


if __name__ == "__main__":
    unittest.main()
