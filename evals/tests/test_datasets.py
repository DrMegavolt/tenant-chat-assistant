"""The versioned-dataset contract (`RAG-008`): manifests, sources, the
PRIV-002 gate at load, and the multi-turn and adversarial slices.

A dataset is a manifest naming version, source (hand-labelled or promoted),
and its PII-check attestation; the loader refuses any case whose free text
carries contact data, account numbers, or unexpected ZIPs — the same patterns
the RAG-009 fixture test asserts, now for every dataset.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from evals.dataset import (
    DatasetError,
    known_datasets,
    load_dataset,
    validate_against_corpus,
)
from evals.runner import resolve_dataset


def _combined_text(name: str) -> str:
    spec = load_dataset(name)
    pieces = [case.query for case in spec.cases]
    pieces.extend(case.scenario or "" for case in spec.cases)
    pieces.extend(case.answer or "" for case in spec.cases)
    pieces.extend(turn for case in spec.cases for turn in case.prior_turns)
    return "\n".join(pieces)


class TestDatasetManifests(unittest.TestCase):
    """Every shipped dataset declares the versioned manifest contract."""

    def test_golden_v1_wraps_the_rag009_fixtures_without_copying(self) -> None:
        spec = load_dataset("golden-v1")
        self.assertEqual(spec.source, "hand-labelled")
        self.assertEqual(spec.version, 1)
        self.assertEqual(spec.pii_check["policy"], "PRIV-002")
        fixture_cases = json.loads((Path("evals/fixtures/cases.json")).read_text())["cases"]
        self.assertEqual(
            [case.id for case in spec.cases],
            [str(case["id"]) for case in fixture_cases],
            "golden-v1 must reference the fixture file, not diverge from it",
        )
        self.assertEqual(len(spec.cases), 25)

    def test_every_dataset_declares_source_pii_check_and_thresholds(self) -> None:
        for name in known_datasets():
            spec = load_dataset(name)
            self.assertIn(spec.source, {"hand-labelled", "promoted"}, name)
            self.assertTrue(spec.pii_check["policy"], name)
            self.assertTrue(spec.thresholds, name)
            self.assertGreater(len(spec.cases), 0, name)

    def test_unknown_dataset_is_refused(self) -> None:
        with self.assertRaises(DatasetError):
            load_dataset("no-such-dataset")


class TestPIIGate(unittest.TestCase):
    """Acceptance 5: no dataset case carries real customer PII (PRIV-002)."""

    def test_all_shipped_datasets_load_clean(self) -> None:
        for name in known_datasets():
            spec = load_dataset(name)
            self.assertTrue(spec.cases)

    def test_no_dataset_case_carries_phone_email_or_account_patterns(self) -> None:
        for name in known_datasets():
            combined = _combined_text(name)
            self.assertNotRegex(combined, r"\b\d{3}[-.)]\s?\d{3}[-.]\d{4}\b", f"{name} phone")
            self.assertNotRegex(
                combined, r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", f"{name} email"
            )
            self.assertNotRegex(combined, r"\b\d{9,}\b", f"{name} card or account")

    def test_no_dataset_case_carries_unexpected_zip_ranges(self) -> None:
        for name in known_datasets():
            combined = _combined_text(name)
            stripped = re.sub(r"9810[1-5]|97035|9720[1-5]", "", combined)
            self.assertNotRegex(stripped, r"\b\d{5}\b", f"{name} unexpected ZIP")

    def test_a_dataset_that_carries_a_phone_is_refused_at_load(self) -> None:
        path = Path("evals/datasets/__pii_probe.json")
        manifest = json.loads(Path("evals/datasets/adversarial-v1.json").read_text())
        manifest["name"] = "__pii_probe"
        manifest["cases"] = [
            {
                "id": "probe",
                "tenant_id": "clearview",
                "query": "Call me at 555-214-0800.",
                "gold_chunk_ids": [],
                "expect_abstain": True,
                "citations": [],
                "scenario": "probe",
            }
        ]
        path.write_text(json.dumps(manifest))
        try:
            with self.assertRaises(DatasetError):
                load_dataset("__pii_probe")
        finally:
            path.unlink()


class TestMultiTurnSlice(unittest.TestCase):
    """The turn-pair slice the planner resolves before scoring (RAG-006): the
    raw follow-up is the case query, the prior turns are real input, and the
    per-tenant vocabulary bounds what history may carry into the query."""

    def test_multi_turn_cases_carry_real_prior_turns_and_a_vocabulary(self) -> None:
        spec = load_dataset("multi-turn-v2")
        self.assertTrue(all(case.prior_turns for case in spec.cases))
        self.assertTrue({"apex", "clearview"} <= set(spec.vocabulary))
        for case in spec.cases:
            self.assertTrue(case.prior_turns, case.id)
            self.assertIn(
                case.scenario,
                {"pronoun", "correction", "topic_shift", "malicious_prior"},
                case.id,
            )

    def test_multi_turn_dataset_runs_through_the_shared_runner(self) -> None:
        spec, corpus = resolve_dataset("multi-turn-v2", 5)
        self.assertEqual(len(spec.cases), 13)
        self.assertEqual(
            validate_against_corpus(spec, [chunk.chunk_id for chunk in corpus.chunks]), ()
        )

    def test_scenarios_cover_the_four_required_kinds(self) -> None:
        spec = load_dataset("multi-turn-v2")
        self.assertTrue(
            {"pronoun", "correction", "topic_shift", "malicious_prior"}
            <= {case.scenario for case in spec.cases}
        )


class TestAdversarialSlice(unittest.TestCase):
    """Financing policy edge cases and the RAG-007 documents folded in."""

    def test_corpus_contains_the_financing_policy_and_adversarial_documents(self) -> None:
        spec, corpus = resolve_dataset("adversarial-v1", 5)
        chunk_ids = {chunk.chunk_id for chunk in corpus.chunks}
        self.assertTrue({"clearview-financing-1", "clearview-financing-5"} <= chunk_ids)
        # The five scanner-flagged documents are quarantined (inactive), like
        # RAG-007's ingestion quarantine; the three scanner-clean ones stay
        # active as defense-in-depth fixtures.
        inactive = {chunk.chunk_id for chunk in corpus.chunks if not chunk.active}
        for flagged in (
            "adv-discount-ultimatum",
            "adv-role-usurpation",
            "adv-prompt-extraction",
            "adv-active-content",
            "adv-permission-escalation",
        ):
            self.assertIn(flagged, inactive, flagged)
        for clean in ("adv-tool-demand-clean", "adv-price-fabrication", "adv-legitimate-terms"):
            self.assertNotIn(clean, inactive, clean)

    def test_financing_and_grounding_scenarios_are_present(self) -> None:
        spec = load_dataset("adversarial-v1")
        scenarios = {case.scenario for case in spec.cases}
        self.assertTrue(
            {"financing", "financing_grounding", "claim_grounding", "injection", "tenant_isolation"}
            <= scenarios
        )
        grounded = [case for case in spec.cases if case.answer is not None]
        self.assertGreaterEqual(len(grounded), 6)
        self.assertTrue(all(case.expect_grounded is not None for case in grounded))

    def test_adversarial_gold_chunks_resolve_in_its_corpus(self) -> None:
        spec, corpus = resolve_dataset("adversarial-v1", 5)
        self.assertEqual(
            validate_against_corpus(spec, [chunk.chunk_id for chunk in corpus.chunks]), ()
        )


if __name__ == "__main__":
    unittest.main()
