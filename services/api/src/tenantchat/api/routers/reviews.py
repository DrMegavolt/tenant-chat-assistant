"""The `FEAT-008` admin surface: the review queue and every review decision.

The queue list is content-free — a row per case with the content-free priority
inputs and the turn's derived columns, no prompt, evidence, or feedback text —
so scanning the queue costs no content read. Everything content-bearing (the
review detail, which carries the feedback reason and the reviewer's overlay,
and every mutation, which decides about that content) sits under the same
dedicated trace-read role and CSRF token as the rest of the inference plane,
and every decision, correction, and promotion is audited to an actor, a
tenant, and a request id (acceptance 2).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from tenantchat.api.dependencies import (
    Audit,
    Feedback,
    Registry,
    RequestId,
    Reviews,
    TurnRecords,
    get_settings,
)
from tenantchat.api.identity import (
    AdminIdentity,
    require_trace_read,
    verify_csrf,
)
from tenantchat.api.review import promote_case, submit_review
from tenantchat.api.schemas import (
    ReviewDecisionResponse,
    ReviewDetailResponse,
    ReviewDiagnosisDecisionRequest,
    ReviewPageResponse,
    ReviewSubmitRequest,
)
from tenantchat.api.store import AuditActorType, AuditEvent
from tenantchat.core.privacy import TurnRecordReadReason
from tenantchat.core.reviews import (
    DiagnosisDecision,
    DiagnosisRelationship,
    ReviewStatus,
    ReviewSubmission,
    ReviewVerdict,
)

router = APIRouter(tags=["admin-reviews"])

TraceReader = Annotated[AdminIdentity, Depends(require_trace_read())]
TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]
ReviewStatusQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=16,
        pattern=r"^[a-z_]+$",
        description="Only cases in this state (open, in_review, awaiting_fix, rejected, resolved).",
    ),
]
ReviewLimitQuery = Annotated[int, Query(ge=1, le=200)]


def _with_csrf(request: Request, identity: AdminIdentity) -> None:
    verify_csrf(request, identity, get_settings(request))


@router.get("/api/admin/reviews", response_model=ReviewPageResponse)
async def list_reviews(
    identity: TraceReader,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    registry: Registry,
    reviews: Reviews,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
    review_status: ReviewStatusQuery = None,
    limit: ReviewLimitQuery = 50,
) -> ReviewPageResponse:
    """The tenant's review queue, highest priority first.

    The list is content-free: every field is the review's own column or the
    turn record's derived projection, so an operator can triage the queue
    without a single content-bearing read. The search is audited with the
    filter that ran, exactly like the trace search surface.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
    """
    registry.get(tenant_id)
    cases = await reviews.search(
        tenant_id, statuses=(review_status,) if review_status else (), limit=limit
    )
    records = {case.turn_id: await turns.get(tenant_id, case.turn_id) for case in cases}
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="review.search",
            resource_type="review_queue",
            resource_id=None,
            request_id=request_id,
            details={
                "reason": reason.value,
                "status": review_status,
                "limit": limit,
                "matches": len(cases),
            },
        )
    )
    return ReviewPageResponse.of(cases, records)


@router.get("/api/admin/reviews/{review_id}", response_model=ReviewDetailResponse)
async def review_detail(
    identity: TraceReader,
    review_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    registry: Registry,
    reviews: Reviews,
    turns: TurnRecords,
    feedback: Feedback,
    audit: Audit,
    request_id: RequestId,
) -> ReviewDetailResponse:
    """One queue entry with its feedback and the reviewer's diagnosis overlay.

    Content-bearing: the feedback reason is the visitor's words, and the
    overlay rows name what the reviewer decided about each automatic
    diagnosis, so this read carries the same dedicated role and audit trail as
    the turn record itself.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such case, or it belongs to another tenant.
    """
    registry.get(tenant_id)
    case = await reviews.get(tenant_id, review_id)
    turn = await turns.get(tenant_id, case.turn_id)
    feedback_record = await feedback.for_turn(tenant_id, case.turn_id)
    diagnoses = await reviews.diagnoses(tenant_id, review_id)
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="review.read",
            resource_type="review_queue",
            resource_id=case.review_id,
            request_id=request_id,
            details={"reason": reason.value, "turn_id": str(case.turn_id)},
        )
    )
    return ReviewDetailResponse.of(case, turn, feedback=feedback_record, diagnoses=diagnoses)


@router.post(
    "/api/admin/reviews/{review_id}/take",
    response_model=ReviewDecisionResponse,
)
async def take_review(
    identity: TraceReader,
    review_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    registry: Registry,
    reviews: Reviews,
    audit: Audit,
    request_id: RequestId,
    request: Request,
) -> ReviewDecisionResponse:
    """Mark an open case as in review by one operator.

    The double-submit token protects the mutation; the audit row names the
    operator who took the case, so two reviewers cannot silently work the same
    row.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such case, or it belongs to another tenant.
        ReviewTransitionError: the case is not ``open``.
    """
    registry.get(tenant_id)
    _with_csrf(request, identity)
    case = await reviews.take(tenant_id, review_id, reviewer=identity.subject)
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="review.taken",
            resource_type="review_queue",
            resource_id=case.review_id,
            request_id=request_id,
            details={"turn_id": str(case.turn_id)},
        )
    )
    return ReviewDecisionResponse.of(case)


@router.post(
    "/api/admin/reviews/{review_id}/review",
    response_model=ReviewDecisionResponse,
)
async def submit_review_decision(
    identity: TraceReader,
    review_id: uuid.UUID,
    payload: ReviewSubmitRequest,
    registry: Registry,
    reviews: Reviews,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
    request: Request,
) -> ReviewDecisionResponse:
    """Record the reviewer's decision, verdict, correction, and fix.

    The submission is validated against the turn's actual automatic diagnosis
    set (every record confirmed, rejected, or amended — acceptance 4), the
    corrected answer is stored beside the immutable trace, and the decision is
    audited with the verdict and destination state.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such case, or it belongs to another tenant.
        ValidationError: the diagnosis decisions do not cover the automatic set.
        ReviewTransitionError: the case is not ``open`` or ``in_review``.
    """
    registry.get(payload.tenant_id)
    _with_csrf(request, identity)
    submission = ReviewSubmission(
        verdict=ReviewVerdict(payload.verdict),
        status=ReviewStatus(payload.status),
        decisions=tuple(_decision(item) for item in payload.diagnoses),
        note=payload.note,
        corrected_answer=payload.corrected_answer,
        proposed_fix=payload.proposed_fix,
    )
    case = await submit_review(
        reviews,
        turns,
        payload.tenant_id,
        review_id,
        reviewer=identity.subject,
        submission=submission,
    )
    await audit.record(
        AuditEvent(
            tenant_id=payload.tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="review.decided",
            resource_type="review_queue",
            resource_id=case.review_id,
            request_id=request_id,
            details={
                "turn_id": str(case.turn_id),
                "verdict": payload.verdict,
                "status": payload.status,
                "has_corrected_answer": payload.corrected_answer is not None,
                "has_proposed_fix": payload.proposed_fix is not None,
            },
        )
    )
    return ReviewDecisionResponse.of(case)


@router.post(
    "/api/admin/reviews/{review_id}/promote",
    response_model=ReviewDecisionResponse,
)
async def promote_review(
    identity: TraceReader,
    review_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    registry: Registry,
    reviews: Reviews,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
    request: Request,
) -> ReviewDecisionResponse:
    """Promote a reviewed, anonymized turn into an evaluation case.

    The promotion runs the privacy check (acceptance 6) and pins the case as a
    projection of the turn, so erasure of the turn erases the dataset with it.
    The audit row records the promotion and the case id it produced.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such case, or it belongs to another tenant.
        ValidationError: the case is not ``awaiting_fix`` or has no query.
        PromotionPrivacyError: the anonymized payload would carry contact data.
    """
    registry.get(tenant_id)
    _with_csrf(request, identity)
    case = await promote_case(reviews, turns, tenant_id, review_id)
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="review.promoted",
            resource_type="review_queue",
            resource_id=case.review_id,
            request_id=request_id,
            details={"turn_id": str(case.turn_id), "case_id": case.case_id},
        )
    )
    return ReviewDecisionResponse.of(case)


def _decision(item: ReviewDiagnosisDecisionRequest) -> DiagnosisDecision:
    return DiagnosisDecision(
        automatic_index=item.automatic_index,
        relationship=DiagnosisRelationship(item.relationship),
        cause=item.cause,
        stage=item.stage,
        role=item.role,
        status=item.status,
        confidence=item.confidence,
        evidence=tuple(item.evidence),
        note=item.note,
    )
