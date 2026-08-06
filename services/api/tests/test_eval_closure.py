"""The `RAG-008` closure wiring: the release gate's real reports close
``awaiting_fix`` reviews through the `FEAT-008` contract.

``apply_eval_report`` (and its store guard) is already tested against
report-shaped fixtures in ``test_feedback_review.py``; these tests prove the
*runner's actual report JSON* satisfies that contract — the report the gate
would publish is fed to the closure unchanged, and a passing promoted case
closes its review with the gate's run id exactly once.
"""

from __future__ import annotations

import asyncio
import uuid

from evals.corpus import FixtureCorpus
from evals.gate import close_passing_reviews
from evals.promotion import materialize_promoted_case
from evals.runner import build_retriever_entry_async, run_evaluation
from evals.scorer import EvalCase
from tenantchat.api.store import InMemoryReviewQueueStore, InMemoryTurnRecordStore

_TENANT = "apex"


def _review_store() -> tuple[InMemoryReviewQueueStore, InMemoryTurnRecordStore]:
    return InMemoryReviewQueueStore(), InMemoryTurnRecordStore()


async def _open_awaiting_fix(
    reviews: InMemoryReviewQueueStore,
    turns: InMemoryTurnRecordStore,
    *,
    case_id: str,
) -> uuid.UUID:
    await turns.record(
        _TENANT,
        uuid.uuid4(),
        content={
            "retrieval": {"query": "What are your hours?", "evidence": []},
            "output": {"answer": "We are open daily from 7 AM to 7 PM.", "claims": []},
            "diagnoses": [],
        },
        trace_id="trace-closure-test",
        outcome="answered",
        component_manifest_hash="a" * 64,
        diagnosis_causes=(),
        diagnosis_statuses=(),
        turn_index=1,
    )
    review = await reviews.enqueue(
        _TENANT,
        uuid.uuid4(),
        source="user_feedback",
        priority=5,
        recurrence=0,
        manifest_hash="a" * 64,
        committed_actions=False,
        novel_manifest=True,
    )
    await reviews.submit(
        _TENANT,
        review.review_id,
        reviewer="operator-7",
        verdict="confirmed",
        note="documented the fix",
        corrected_answer=None,
        proposed_fix="reindex the document",
        status="awaiting_fix",
        diagnoses=(),
    )
    await reviews.set_case_id(_TENANT, review.review_id, case_id=case_id)
    return review.review_id


def _promoted_case(case_id: str, corpus: FixtureCorpus, **overrides: object) -> dict[str, object]:
    payload = {
        "id": case_id,
        "tenant_id": _TENANT,
        "query": "What are your hours?",
        "gold_chunk_ids": ["apex-hvac-2"],
        "citations": ["apex-hvac-2"],
        "scenario": "reviewed-turn",
        "expect_abstain": False,
    }
    payload.update(overrides)
    texts = {chunk.chunk_id: chunk.text for chunk in corpus.chunks}
    return materialize_promoted_case(payload, chunk_texts=texts)


async def _real_report(case_payload: dict[str, object], *, retriever: str) -> dict[str, object]:
    """A real runner report over one promoted case, exactly as the gate runs."""
    corpus = await FixtureCorpus.load()
    case = EvalCase.from_json(case_payload)
    entry = await build_retriever_entry_async(
        retriever, corpus, 5, cases=(case,), abstain_threshold_value=0.5
    )
    report = await run_evaluation(
        retriever=entry.retriever,
        retriever_config=entry.config,
        corpus=corpus,
        cases=(case,),
        abstain_threshold_value=entry.abstain_threshold,
        min_recall=0.6,
        min_citation_precision=0.8,
        min_abstention=0.9,
        reranker=entry.reranker,
    )
    return report.to_dict()


def test_a_passing_gate_report_closes_the_awaiting_fix_review() -> None:
    corpus = asyncio.run(FixtureCorpus.load())
    case_id = f"review-{uuid.uuid4()}"
    reviews, turns = _review_store()
    review_id = asyncio.run(_open_awaiting_fix(reviews, turns, case_id=case_id))
    report = asyncio.run(_real_report(_promoted_case(case_id, corpus), retriever="lexical-overlap"))

    closed = asyncio.run(
        close_passing_reviews(reviews, _TENANT, run_id="gate-run-2026-08-06-1", report=report)
    )
    assert closed == (str(review_id),)
    case = asyncio.run(reviews.get(_TENANT, review_id))
    assert case.status == "resolved"
    assert case.closing_eval_run_id == "gate-run-2026-08-06-1"
    assert case.closing_eval_case_id == case_id
    assert case.closing_eval_passed_at is not None


def test_a_regressed_case_in_a_real_report_stays_open() -> None:
    corpus = asyncio.run(FixtureCorpus.load())
    case_id = f"review-{uuid.uuid4()}"
    reviews, turns = _review_store()
    review_id = asyncio.run(_open_awaiting_fix(reviews, turns, case_id=case_id))
    payload = _promoted_case(
        case_id,
        corpus,
        query="Can you repair my furnace heating unit?",
        gold_chunk_ids=["apex-hvac-3", "apex-hvac-8"],
        citations=["apex-hvac-3"],
    )
    # The hybrid retriever regresses this case's recall to 0.5 (below the 0.6
    # threshold): the same report that trips the gate must leave the review
    # open, because the closure reads the identical predicate.
    report = asyncio.run(_real_report(payload, retriever="hybrid"))
    closed = asyncio.run(
        close_passing_reviews(reviews, _TENANT, run_id="gate-run-2026-08-06-2", report=report)
    )
    assert closed == ()
    case = asyncio.run(reviews.get(_TENANT, review_id))
    assert case.status == "awaiting_fix"
    assert case.closing_eval_run_id is None


def test_a_report_for_another_tenant_leaves_the_review_untouched() -> None:
    corpus = asyncio.run(FixtureCorpus.load())
    case_id = f"review-{uuid.uuid4()}"
    reviews, turns = _review_store()
    review_id = asyncio.run(_open_awaiting_fix(reviews, turns, case_id=case_id))
    report = asyncio.run(_real_report(_promoted_case(case_id, corpus), retriever="lexical-overlap"))
    asyncio.run(close_passing_reviews(reviews, "clearview", run_id="gate-run-other", report=report))
    case = asyncio.run(reviews.get(_TENANT, review_id))
    assert case.status == "awaiting_fix"
