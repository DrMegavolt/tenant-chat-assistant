"""The `FEAT-008` review workflow: enqueueing, review submission, safe
promotion, and the evaluation-closure gate.

This module owns the decisions that need both domain rules (`core.reviews`)
and store state: whether a turn qualifies for the queue, what priority its
case gets, whether a submission covers the automatic diagnoses, whether a
promoted case passes the privacy check, and which reviews an evaluation
report closes. It never touches the HTTP surface and never reads a prompt or
an output except through the turn record's opaque content — the content it
carries into the promoted case payload is itself governed by the projection
the store writes.

The evaluation-closure contract (acceptance 5) is :func:`apply_eval_report`:
the `RAG-008` gate will call it with the runner's report JSON and a server-
minted run id, and it closes every ``awaiting_fix`` review whose promoted case
passed — writing the first passing run's reference exactly once, by store
guard. Until a gate exists, a reviewed case simply stays visibly open.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from tenantchat.api.store import (
    ReviewCase,
    ReviewDiagnosis,
    ReviewQueueStore,
    TurnFeedback,
    TurnFeedbackStore,
    TurnRecord,
    TurnRecordStore,
)
from tenantchat.core.errors import PromotionPrivacyError, ValidationError
from tenantchat.core.reviews import (
    DiagnosisDecision,
    FeedbackRating,
    ReviewSource,
    ReviewSubmission,
    eval_case_payload,
    is_technical_failure,
    payload_contains_pii,
    priority_score,
    technical_severity,
    validate_decisions,
)

# The kind string the projection registry pins promoted cases with; the
# erasure lifecycle and the privacy tests already know it.
EVAL_DATASET_KIND: Final = "eval_dataset"

# Bounded inputs to the priority formula; see `core.reviews` for the formula.
_RECURRENCE_CAP: Final = 3


async def enqueue_automatic(
    reviews: ReviewQueueStore,
    tenant_id: str,
    turn: TurnRecord,
) -> ReviewCase | None:
    """Open a case for a turn the detector proved technical (acceptance 3).

    The trigger is the automatic diagnoses inside the recorded trace — the
    same content the attribution surface filters on — so a turn enters the
    queue with no thumbs-down and no reviewer in the loop. ``None`` when the
    detector did not prove a technical cause, or when the turn already has a
    case (the store's idempotency returns it and the queue keeps the first
    source).
    """
    diagnoses = _automatic_diagnoses(turn)
    if not is_technical_failure(diagnoses):
        return None
    return await _enqueue(reviews, tenant_id, turn, source=ReviewSource.AUTOMATIC)


async def record_feedback_and_enqueue(
    feedback_store: TurnFeedbackStore,
    reviews: ReviewQueueStore,
    turns: TurnRecordStore,
    tenant_id: str,
    turn_id: UUID,
    *,
    rating: str,
    reason: str | None,
) -> TurnFeedback:
    """Record one visitor's rating; a thumbs-down also opens a queue case.

    The caller has already proven the turn belongs to the credential's tenant
    and session, so the feedback row can name the turn without leaking another
    conversation (acceptance 1). A thumbs-up records without enqueueing; a
    thumbs-down enqueues with ``user_feedback`` source, idempotently.
    """
    feedback = await feedback_store.record(tenant_id, turn_id, rating=rating, reason=reason)
    if rating == FeedbackRating.DOWN.value:
        turn = await turns.get(tenant_id, turn_id)
        await _enqueue(reviews, tenant_id, turn, source=ReviewSource.USER_FEEDBACK)
    return feedback


async def submit_review(
    reviews: ReviewQueueStore,
    turns: TurnRecordStore,
    tenant_id: str,
    review_id: UUID,
    *,
    reviewer: str,
    submission: ReviewSubmission,
) -> ReviewCase:
    """Record a review decision, binding it to the automatic diagnosis set.

    The coverage rule is acceptance 4's teeth: the submission must confirm,
    reject, or amend *every* automatic diagnosis the detector recorded, and
    the reviewer's records are stored as separate rows that reference the
    originals by index — the detector's records inside the turn content are
    never mutated, so automatic and reviewer diagnoses can always be shown
    side by side.

    Raises:
        ValidationError: the decisions do not cover the automatic set.
        NotFoundError: the review or its turn record is absent.
    """
    review = await reviews.get(tenant_id, review_id)
    turn = await turns.get(tenant_id, review.turn_id)
    automatic = _automatic_diagnoses(turn)
    validate_decisions(automatic_count=len(automatic), submission=submission)
    rows = tuple(
        _as_review_diagnosis(tenant_id, review_id, decision) for decision in submission.decisions
    )
    return await reviews.submit(
        tenant_id,
        review_id,
        reviewer=reviewer,
        verdict=submission.verdict.value,
        note=submission.note,
        corrected_answer=submission.corrected_answer,
        proposed_fix=submission.proposed_fix,
        status=submission.status.value,
        diagnoses=rows,
    )


async def promote_case(
    reviews: ReviewQueueStore,
    turns: TurnRecordStore,
    tenant_id: str,
    review_id: UUID,
) -> ReviewCase:
    """Promote a reviewed, anonymized turn into an evaluation case.

    The privacy check (acceptance 6) refuses a payload that would still carry
    contact data — promotion never silently redacts what a reviewer approved.
    The case is the projection the turn's erasure cascades away, and its id
    is derived from the review so the fix-closure gate can find it again.

    Raises:
        ValidationError: the review is not ``awaiting_fix`` or the turn has
            no retrievable query to promote.
        PromotionPrivacyError: the anonymized payload would carry contact data.
    """
    review = await reviews.get(tenant_id, review_id)
    if review.status != "awaiting_fix":
        raise ValidationError(detail="only a reviewed case awaiting its fix may be promoted")
    if review.case_id is not None:
        return review
    turn = await turns.get(tenant_id, review.turn_id)
    payload = _case_payload(review, turn)
    if payload_contains_pii(payload):
        raise PromotionPrivacyError(detail="promoted case carries a phone or email address")
    case_id = str(payload["id"])
    await turns.create_projection(
        tenant_id,
        turn.turn_id,
        kind=EVAL_DATASET_KIND,
        payload=payload,
    )
    return await reviews.set_case_id(tenant_id, review_id, case_id=case_id)


def case_passes(report: Mapping[str, object], case_id: str) -> bool:
    """Whether one evaluation report row shows a case passing every threshold.

    This is the per-case reading of the harness's report: a case passes when
    its abstention decision was correct, nothing retrieved leaked across
    tenants, and every score it declared meets the run's thresholds. A case
    with no gold chunks (``recall``/``citation_precision`` are ``null``) is
    judged on abstention alone. ``RAG-008`` will use exactly this predicate
    when it gates releases on the report.
    """
    components = report.get("components")
    min_recall = _component_float(components, "min_recall")
    min_citation = _component_float(components, "min_citation_precision")
    rows = report.get("cases")
    if not isinstance(rows, list):
        return False
    for row in rows:
        if not isinstance(row, Mapping) or str(row.get("case", "")) != case_id:
            continue
        if not bool(row.get("abstain_correct")):
            return False
        leaks = row.get("cross_tenant_leaks")
        if isinstance(leaks, list) and leaks:
            return False
        recall = row.get("recall")
        if (
            isinstance(recall, int | float)
            and not isinstance(recall, bool)
            and float(recall) < min_recall
        ):
            return False
        citation = row.get("citation_precision")
        citation_too_low = (
            isinstance(citation, int | float)
            and not isinstance(citation, bool)
            and float(citation) < min_citation
        )
        return not citation_too_low
    return False


async def apply_eval_report(
    reviews: ReviewQueueStore,
    tenant_id: str,
    *,
    run_id: str,
    report: Mapping[str, object],
    passed_at: datetime | None = None,
) -> tuple[str, ...]:
    """Close every open review whose promoted case the report passes.

    This is the acceptance-5 linkage the `RAG-008` gate will populate: given
    the runner's report JSON and a server-minted run id, each ``awaiting_fix``
    review whose case appears in the report's passing rows receives the first
    passing run's reference and moves to ``resolved`` — the store's guard
    keeps that reference immutable, so re-applying the report never rewrites
    history. Reviews the report does not cover stay visibly open. Returns the
    review ids that closed.
    """
    rows = report.get("cases")
    if not isinstance(rows, list):
        return ()
    passing = tuple(
        str(row["case"])
        for row in rows
        if isinstance(row, Mapping) and case_passes(report, str(row.get("case", "")))
    )
    if not passing:
        return ()
    candidates = await reviews.for_case_ids(tenant_id, passing)
    closed: list[str] = []
    moment = passed_at or datetime.now(UTC)
    for case in candidates:
        if case.status != "awaiting_fix":
            continue
        updated = await reviews.record_eval_pass(
            tenant_id,
            case.review_id,
            run_id=run_id,
            case_id=str(case.case_id or ""),
            passed_at=moment,
        )
        if updated.status == "resolved":
            closed.append(str(case.review_id))
    return tuple(closed)


async def _enqueue(
    reviews: ReviewQueueStore,
    tenant_id: str,
    turn: TurnRecord,
    *,
    source: ReviewSource,
) -> ReviewCase:
    """Compute the deterministic priority inputs and open the case.

    The inputs are frozen at enqueue time: severity from the automatic
    diagnoses, recurrence and novelty from the tenant's existing queue state,
    and the business-outcome bit from the committed-actions record. All four
    are content-free, so the stored priority never needs content re-derived.
    """
    causes = tuple(
        str(diagnosis.get("cause", ""))
        for diagnosis in _automatic_diagnoses(turn)
        if diagnosis.get("cause")
    )
    severity = technical_severity(causes)
    prior = await reviews.count_for_manifest(tenant_id, turn.component_manifest_hash)
    committed = bool(_committed_actions(turn))
    score = priority_score(
        severity=severity,
        recurrence=min(prior, _RECURRENCE_CAP),
        committed=committed,
        novel=prior == 0,
    )
    return await reviews.enqueue(
        tenant_id,
        turn.turn_id,
        source=source.value,
        priority=score,
        recurrence=prior + 1,
        manifest_hash=turn.component_manifest_hash,
        committed_actions=committed,
        novel_manifest=prior == 0,
    )


def _committed_actions(turn: TurnRecord) -> tuple[str, ...]:
    tools = turn.content.get("tools")
    if not isinstance(tools, Mapping):
        return ()
    committed = tools.get("committed")
    if not isinstance(committed, list):
        return ()
    return tuple(
        str(action.get("action", ""))
        for action in committed
        if isinstance(action, Mapping) and action.get("action")
    )


def _case_payload(review: ReviewCase, turn: TurnRecord) -> dict[str, object]:
    """The promoted case: the turn's query as the query, its evidence as gold.

    ``query`` and ``scenario`` are the only free-text fields, which is exactly
    what :func:`payload_contains_pii` scans before promotion is allowed; the
    gold set is the retrieval evidence the turn actually grounded on, and the
    citation set is the claims the validator saw.
    """
    content = turn.content
    retrieval = content.get("retrieval")
    if not isinstance(retrieval, Mapping):
        raise ValidationError(detail="turn has no retrieval section to promote")
    query = str(retrieval.get("query", ""))
    if not query.strip():
        raise ValidationError(detail="turn has no retrievable query to promote")
    gold = tuple(
        str(item.get("source_id", ""))
        for item in _list_of_dicts(retrieval.get("evidence"))
        if item.get("source_id")
    )
    output = content.get("output")
    citations = (
        tuple(str(claim) for claim in _list_of_str(output.get("claims")))
        if isinstance(output, Mapping)
        else ()
    )
    return eval_case_payload(
        case_id=f"review-{review.review_id}",
        tenant_id=review.tenant_id,
        query=query,
        gold_chunk_ids=gold,
        citations=citations,
        scenario="reviewed-turn",
        expect_abstain=False,
    )


def _automatic_diagnoses(turn: TurnRecord) -> list[dict[str, object]]:
    diagnoses = turn.content.get("diagnoses")
    return _list_of_dicts(diagnoses)


def _as_review_diagnosis(
    tenant_id: str, review_id: UUID, decision: DiagnosisDecision
) -> ReviewDiagnosis:
    return ReviewDiagnosis(
        diagnosis_id=uuid.uuid4(),
        tenant_id=tenant_id,
        review_id=review_id,
        relationship=decision.relationship.value,
        automatic_index=decision.automatic_index,
        cause=decision.cause,
        stage=decision.stage,
        role=decision.role,
        status=decision.status,
        confidence=decision.confidence,
        evidence=decision.evidence,
        note=decision.note,
        created_at=datetime.now(UTC),
    )


def _component_float(components: object, name: str) -> float:
    if not isinstance(components, Mapping):
        return 0.0
    value = components.get(name)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return 0.0


def _list_of_dicts(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _list_of_str(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str)]
