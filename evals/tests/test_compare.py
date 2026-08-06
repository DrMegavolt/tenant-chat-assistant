"""The baseline-versus-candidate comparison, the release gate, the reviewed-
exception registry, and the judge policy (`RAG-008` acceptance 1-4).

A comparison is fully deterministic; a regression carries its trace linkage;
the gate blocks a material regression unless a reviewed exception bound to
the same manifests waives it; and an unvalidated LLM judge can inform the
report but never gate it.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast
from unittest import mock

from evals.compare import ComparisonReport, compare_reports, metric_passes, row_passes
from evals.corpus import FixtureCorpus
from evals.dataset import load_dataset
from evals.exceptions import ExceptionRegistry, ReviewException, mint_waiver
from evals.judges import JudgeProfile, register_judge
from evals.retriever import RetrievalResult, baseline_config
from evals.runner import build_retriever_entry, dataset_thresholds, run_evaluation
from evals.scorer import EvalCase, EvaluationReport
from tenantchat.api.review import case_passes

_METRICS = (
    "recall_at_k",
    "citation_precision",
    "abstention_correctness",
    "grounding_correctness",
    "cross_tenant_leaks",
)


def _evaluate(
    cases: Sequence[EvalCase], *, retriever_name: str, corpus: FixtureCorpus, k: int = 5
) -> EvaluationReport:
    entry = build_retriever_entry(
        retriever_name,
        corpus,
        k,
        cases=cases,
        abstain_threshold_value=0.5,
    )
    thresholds = dataset_thresholds(load_dataset("golden-v1"))
    return asyncio.run(
        run_evaluation(
            retriever=entry.retriever,
            retriever_config=entry.config,
            corpus=corpus,
            cases=cases,
            abstain_threshold_value=entry.abstain_threshold,
            min_recall=thresholds[0],
            min_citation_precision=thresholds[1],
            min_abstention=thresholds[2],
            min_grounding=thresholds[3],
            reranker=entry.reranker,
        )
    )


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = asyncio.run(FixtureCorpus.load())
        self.dataset = load_dataset("golden-v1")
        self.baseline = _evaluate(
            self.dataset.cases, retriever_name="lexical-overlap", corpus=self.corpus
        )
        self.candidate = _evaluate(self.dataset.cases, retriever_name="hybrid", corpus=self.corpus)

    def compare(self, exceptions: ExceptionRegistry) -> ComparisonReport:
        return compare_reports(self.baseline, self.candidate, self.dataset, exceptions=exceptions)


class TestPredicateEquivalence(_Base):
    """The gate's per-case predicate and the FEAT-008 closure predicate must
    agree on every row, or CI and the closure would diverge."""

    def test_row_passes_matches_case_passes_on_every_row(self) -> None:
        for report in (self.baseline, self.candidate):
            serialized = report.to_dict()
            for row in report.cases:
                self.assertEqual(
                    row_passes(row, report),
                    case_passes(serialized, row.case.id),
                    row.case.id,
                )

    def test_metric_passes_mirrors_the_row_predicate(self) -> None:
        for report in (self.baseline, self.candidate):
            for row in report.cases:
                metrics = {m: metric_passes(row, report, m) for m in _METRICS}
                self.assertEqual(row_passes(row, report), all(metrics.values()), row.case.id)


class TestComparisonReport(_Base):
    """Acceptance 2: one report carries manifest diff, deltas, and regressions
    with their trace linkage — no hand correlation between separate reports."""

    def test_manifest_diff_names_the_changed_components(self) -> None:
        comparison = self.compare(ExceptionRegistry(()))
        diff = {item["component"]: item["changed"] for item in comparison.manifest_diff}
        self.assertTrue(diff["retriever"])
        self.assertTrue(diff["reranker"])
        self.assertFalse(diff["embedding"])
        self.assertFalse(diff["prompt_template"])

    def test_aggregate_deltas_are_reported(self) -> None:
        comparison = self.compare(ExceptionRegistry(()))
        self.assertLess(comparison.aggregate_deltas["recall_at_k"], 0.0)

    def test_regressions_carry_case_metric_and_provenance(self) -> None:
        comparison = self.compare(ExceptionRegistry(()))
        by_case = {delta.case: delta for delta in comparison.regressions}
        self.assertEqual(
            set(by_case),
            {"apex-hvac-heating-repair", "clearview-hvac-current-pricing"},
        )
        for delta in by_case.values():
            self.assertEqual(delta.metric, "recall_at_k")
            self.assertEqual(delta.baseline, 1.0)
            self.assertEqual(delta.candidate, 0.5)
            self.assertEqual(delta.trace, {"review_id": None, "trace_id": None, "turn_id": None})
            self.assertTrue(delta.regressed)

    def test_promoted_case_regression_carries_its_review_and_trace(self) -> None:
        dataset = load_dataset("golden-v1")
        case = next(case for case in dataset.cases if case.id == "apex-hvac-heating-repair")
        promoted = EvalCase(
            id=case.id,
            tenant_id=case.tenant_id,
            query=case.query,
            gold_chunk_ids=case.gold_chunk_ids,
            expect_abstain=case.expect_abstain,
            citations=case.citations,
            scenario=case.scenario,
            review_id=f"review-{case.id}",
            trace_id="trace-42",
            turn_id="turn-42",
        )
        baseline = _evaluate((promoted,), retriever_name="lexical-overlap", corpus=self.corpus)
        candidate = _evaluate((promoted,), retriever_name="hybrid", corpus=self.corpus)
        comparison = compare_reports(baseline, candidate, dataset, exceptions=ExceptionRegistry(()))
        self.assertEqual(len(comparison.regressions), 1)
        delta = comparison.regressions[0]
        self.assertEqual(delta.trace["review_id"], f"review-{case.id}")
        self.assertEqual(delta.trace["trace_id"], "trace-42")
        self.assertEqual(delta.trace["turn_id"], "turn-42")

    def test_deterministic_reports_are_byte_identical(self) -> None:
        first = self.compare(ExceptionRegistry(())).to_json()
        second = self.compare(ExceptionRegistry(())).to_json()
        self.assertEqual(first, second)

    def test_run_ids_are_content_bound(self) -> None:
        comparison = self.compare(ExceptionRegistry(()))
        self.assertTrue(str(comparison.baseline["run_id"]).startswith("eval-"))
        self.assertNotEqual(str(comparison.baseline["run_id"]), str(comparison.candidate["run_id"]))


class TestGate(_Base):
    """Acceptance 3: CI blocks material regressions below thresholds."""

    def test_unwaived_regressions_block(self) -> None:
        comparison = self.compare(ExceptionRegistry(()))
        self.assertFalse(comparison.gate.passed)
        blockers = {(blocker.kind, blocker.case) for blocker in comparison.gate.blockers}
        self.assertIn(("case_regression", "apex-hvac-heating-repair"), blockers)
        self.assertNotIn(("cross_tenant_leak", None), blockers)

    def test_shipped_exceptions_waive_the_documented_baseline_regressions(self) -> None:
        comparison = self.compare(ExceptionRegistry.load())
        self.assertTrue(comparison.gate.passed, comparison.to_text())
        self.assertEqual(len(comparison.gate.exceptions_applied), 2)
        for blocker in comparison.gate.blockers:
            self.assertTrue(blocker.waived, blocker.case)
            self.assertTrue(blocker.waived_by)

    def test_a_waiver_for_a_different_manifest_does_not_apply(self) -> None:
        wrong = ExceptionRegistry(
            (
                ReviewException(
                    case_id="apex-hvac-heating-repair",
                    metric="recall_at_k",
                    baseline_manifest_hash="0" * 64,
                    candidate_manifest_hash="1" * 64,
                    waived_by="reviewer",
                    waived_at="2026-08-06",
                    reason="reviewed against a different report",
                ),
            )
        )
        comparison = self.compare(wrong)
        self.assertFalse(comparison.gate.passed)
        self.assertEqual(comparison.gate.exceptions_applied, ())

    def test_a_matching_waiver_waives_only_its_own_case(self) -> None:
        components = self.baseline.to_dict()["components"], self.candidate.to_dict()["components"]
        waiver = mint_waiver(
            case_id="apex-hvac-heating-repair",
            metric="recall_at_k",
            baseline_components=cast(Mapping[str, object], components[0]),
            candidate_components=cast(Mapping[str, object], components[1]),
            waived_by="reviewer",
            reason="accepted regression",
        )
        comparison = self.compare(ExceptionRegistry((waiver,)))
        self.assertFalse(comparison.gate.passed, "the other case must still block")
        blockers = {(blocker.case, blocker.waived) for blocker in comparison.gate.blockers}
        self.assertIn(("apex-hvac-heating-repair", True), blockers)
        self.assertIn(("clearview-hvac-current-pricing", False), blockers)

    def test_a_cross_tenant_leak_is_never_waivable(self) -> None:
        leaker = _LeakyRetriever()
        leaking = asyncio.run(
            run_evaluation(
                retriever=leaker,
                retriever_config=baseline_config(k=5),
                corpus=self.corpus,
                cases=self.dataset.cases,
                abstain_threshold_value=0.5,
                min_recall=0.6,
                min_citation_precision=0.8,
                min_abstention=0.9,
            )
        )
        waiver = mint_waiver(
            case_id=None,
            metric="cross_tenant_leaks",
            baseline_components=cast(Mapping[str, object], self.baseline.to_dict()["components"]),
            candidate_components=cast(Mapping[str, object], leaking.to_dict()["components"]),
            waived_by="reviewer",
            reason="attempted waiver",
        )
        comparison = compare_reports(
            self.baseline, leaking, self.dataset, exceptions=ExceptionRegistry((waiver,))
        )
        self.assertFalse(comparison.gate.passed)
        leak_blockers = [
            blocker for blocker in comparison.gate.blockers if blocker.kind == "cross_tenant_leak"
        ]
        self.assertEqual(len(leak_blockers), 1)
        self.assertFalse(leak_blockers[0].waived, "leaks are a hard invariant, not waivable")

    def test_environment_thresholds_override_the_manifest(self) -> None:
        with mock.patch.dict(os.environ, {"RAG_EVAL_MIN_RECALL_AT_K": "0.99"}, clear=False):
            min_recall, _, _, _ = dataset_thresholds(self.dataset)
        self.assertEqual(min_recall, 0.99)


class _LeakyRetriever:
    """Returns a chunk that does not belong to the tenant, so the run's leak
    invariant trips regardless of thresholds or waivers."""

    def __init__(self) -> None:
        self._corpus = asyncio.run(FixtureCorpus.load())

    async def retrieve(self, query: str, *, tenant_id: str, k: int) -> tuple[RetrievalResult, ...]:
        other = next(chunk for chunk in self._corpus.chunks if chunk.tenant_id != tenant_id)
        return (RetrievalResult(chunk_id=other.chunk_id, score=0.99),)


class TestJudgePolicy(unittest.TestCase):
    """Acceptance 4: only a validated judge may gate; an unvalidated judge
    informs review but never blocks."""

    def setUp(self) -> None:
        self.corpus = asyncio.run(FixtureCorpus.load())
        self.dataset = load_dataset("golden-v1")
        self.baseline = _evaluate(
            self.dataset.cases, retriever_name="lexical-overlap", corpus=self.corpus
        )
        self.candidate = _evaluate(self.dataset.cases, retriever_name="hybrid", corpus=self.corpus)

    def _compare(self, judge_name: str) -> ComparisonReport:
        return compare_reports(
            self.baseline,
            self.candidate,
            self.dataset,
            exceptions=ExceptionRegistry(()),
            judge_regressions=((judge_name, "apex-hvac-heating-repair"),),
        )

    def test_unvalidated_judge_cannot_gate(self) -> None:
        register_judge(JudgeProfile(name="unvalidated-judge", agreement=None, held_out_size=0))
        comparison = self._compare("unvalidated-judge")
        informational = {item["judge"]: item for item in comparison.gate.informational}
        self.assertEqual(informational["unvalidated-judge"]["state"], "unvalidated")
        judge_blockers = [
            blocker for blocker in comparison.gate.blockers if blocker.kind == "judge_regression"
        ]
        self.assertEqual(judge_blockers, [])
        profile = next(item for item in comparison.judges if item["name"] == "unvalidated-judge")
        self.assertFalse(profile["gates"])

    def test_validated_judge_with_measured_agreement_can_gate(self) -> None:
        register_judge(
            JudgeProfile(
                name="validated-judge",
                agreement=0.9,
                held_out_size=40,
                validated_at="review-2026-08-06",
            )
        )
        comparison = self._compare("validated-judge")
        judge_blockers = [
            blocker for blocker in comparison.gate.blockers if blocker.kind == "judge_regression"
        ]
        self.assertEqual(len(judge_blockers), 1)

    def test_judge_agreement_below_floor_never_gates(self) -> None:
        register_judge(JudgeProfile(name="weak-judge", agreement=0.6, held_out_size=40))
        comparison = self._compare("weak-judge")
        judge_blockers = [
            blocker for blocker in comparison.gate.blockers if blocker.kind == "judge_regression"
        ]
        self.assertEqual(judge_blockers, [])

    def test_unregistered_judge_is_reported_informational(self) -> None:
        comparison = self._compare("ghost-judge")
        self.assertEqual(comparison.gate.informational[0]["state"], "unregistered")


class TestRegistryFile(unittest.TestCase):
    def test_shipped_registry_waives_the_documented_golden_pair(self) -> None:
        registry = ExceptionRegistry.load()
        self.assertEqual(len(registry.waivers), 2)
        for waiver in registry.waivers:
            self.assertIn(
                waiver.case_id, {"apex-hvac-heating-repair", "clearview-hvac-current-pricing"}
            )
            self.assertEqual(waiver.metric, "recall_at_k")
            self.assertTrue(waiver.waived_by)
            self.assertTrue(waiver.reason)
            self.assertEqual(len(waiver.baseline_manifest_hash), 64)
            self.assertEqual(len(waiver.candidate_manifest_hash), 64)

    def test_registry_path_is_checked_in(self) -> None:
        path = Path("evals/exceptions.json")
        self.assertTrue(path.is_file())
        raw = json.loads(path.read_text())
        self.assertEqual(raw["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()
