"""The FEAT-008 promotion consumption path (`RAG-008` deliverable 2): a
reviewed projection materializes into a dataset case — gold chunk texts
resolved from the knowledge base, PRIV-002 re-checked at ingestion, and
provenance attached for the comparison report's regression links.
"""

from __future__ import annotations

import asyncio
import unittest
from collections.abc import Sequence
from typing import cast

from evals.corpus import FixtureCorpus
from evals.dataset import load_dataset
from evals.promotion import (
    PromotedCaseError,
    materialize_promoted_case,
    projection_dataset,
)

_TURN = {"trace_id": "trace-promoted-1", "turn_id": "turn-promoted-1"}


def _projection_payload(
    *, query: str = "What are your hours?", gold: Sequence[str] = ("apex-hvac-2",)
) -> dict[str, object]:
    return {
        "id": "review-123e4567-e89b-12d3-a456-426614174000",
        "tenant_id": "apex",
        "query": query,
        "gold_chunk_ids": list(gold),
        "citations": ["apex-hvac-2"],
        "scenario": "reviewed-turn",
        "expect_abstain": False,
    }


class _CorpusTexts(unittest.TestCase):
    corpus: FixtureCorpus
    texts: dict[str, str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.corpus = asyncio.run(FixtureCorpus.load())
        cls.texts = {chunk.chunk_id: chunk.text for chunk in cls.corpus.chunks}


class TestMaterialize(_CorpusTexts):
    def test_a_promoted_projection_becomes_a_scoreable_case(self) -> None:
        case = materialize_promoted_case(
            _projection_payload(), chunk_texts=self.texts, turn_record=_TURN
        )
        self.assertEqual(case["id"], "review-123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual(case["source"], "promoted")
        self.assertEqual(case["review_id"], "review-123e4567-e89b-12d3-a456-426614174000")
        self.assertEqual(case["trace_id"], "trace-promoted-1")
        self.assertEqual(case["turn_id"], "turn-promoted-1")
        self.assertEqual(case["gold_chunk_ids"], ["apex-hvac-2"])

    def test_the_materialized_case_runs_through_the_shared_scorer(self) -> None:
        case = materialize_promoted_case(
            _projection_payload(), chunk_texts=self.texts, turn_record=_TURN
        )
        spec = load_dataset("golden-v1")
        cases = (*spec.cases, type(spec.cases[0]).from_json(case))
        known = {c.id for c in cases}
        self.assertIn("review-123e4567-e89b-12d3-a456-426614174000", known)

    def test_gold_chunks_must_resolve_in_the_knowledge_base(self) -> None:
        payload = _projection_payload(gold=("no-such-chunk",))
        with self.assertRaises(PromotedCaseError):
            materialize_promoted_case(payload, chunk_texts=self.texts)

    def test_pii_is_rechecked_at_ingestion(self) -> None:
        payload = _projection_payload(query="What are your hours? Call 555-214-0800.")
        with self.assertRaises(PromotedCaseError):
            materialize_promoted_case(payload, chunk_texts=self.texts)

    def test_non_review_ids_are_refused(self) -> None:
        payload = _projection_payload()
        payload["id"] = "not-a-review"
        with self.assertRaises(PromotedCaseError):
            materialize_promoted_case(payload, chunk_texts=self.texts)

    def test_a_corrected_answer_rides_into_the_grounding_dimension(self) -> None:
        payload = _projection_payload()
        payload["answer"] = "We are open daily from 7 AM to 7 PM."
        payload["expect_grounded"] = True
        case = materialize_promoted_case(payload, chunk_texts=self.texts, turn_record=_TURN)
        self.assertEqual(case["answer"], "We are open daily from 7 AM to 7 PM.")
        self.assertTrue(case["expect_grounded"])

    def test_the_review_scenario_is_preserved(self) -> None:
        case = materialize_promoted_case(
            _projection_payload(), chunk_texts=self.texts, turn_record=_TURN
        )
        self.assertEqual(case["scenario"], "reviewed-turn")
        self.assertFalse(case["expect_abstain"])


class TestProjectionDataset(_CorpusTexts):
    def test_a_projection_collection_becomes_a_promoted_dataset(self) -> None:
        manifest = projection_dataset(
            (_projection_payload(),),
            tenant_id="apex",
            chunk_texts=self.texts,
            turn_records={
                "review-123e4567-e89b-12d3-a456-426614174000": _TURN,
            },
        )
        self.assertEqual(manifest["name"], "promoted-apex-v1")
        self.assertEqual(manifest["source"], "promoted")
        pii_check = manifest["pii_check"]
        assert isinstance(pii_check, dict)
        self.assertEqual(pii_check["enforced_at"], "promotion-ingest-and-load")
        cases = cast(list[dict[str, object]], manifest["cases"])
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["trace_id"], "trace-promoted-1")

    def test_a_promoted_dataset_carries_the_loader_contract(self) -> None:
        manifest = projection_dataset(
            (_projection_payload(),),
            tenant_id="apex",
            chunk_texts=self.texts,
        )
        self.assertEqual(manifest["source"], "promoted")
        cases = cast(list[dict[str, object]], manifest["cases"])
        self.assertIn("review-", str(cases[0]["id"]))
        self.assertEqual(cases[0]["tenant_id"], "apex")
        self.assertEqual(cases[0]["gold_chunk_ids"], ["apex-hvac-2"])
        self.assertEqual(cases[0]["scenario"], "reviewed-turn")


if __name__ == "__main__":
    unittest.main()
