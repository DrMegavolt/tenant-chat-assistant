"""The PRIV-002 inference-plane surface: the dedicated read role and the turn
record under it.

Everything here reads or changes another person's conversational data, so every
route requires an identity the gateway established. The read is gated by the
dedicated trace-read grant (:func:`tenantchat.api.identity.require_trace_read`)
— deliberately not by any transcript role — and every read is audited to an
actor, turn, and reason. Granting and revoking the role are platform-admin
mutations, audited like membership assignment and protected by the same
double-submit token.

The turn record itself is the envelope `OBS-004` will populate; this router is
its governance surface, not its content model. `FEAT-015` adds the explorer
surface on top: the six Gate B filters, the audited single-read, safe replay,
and the gold-evidence overlay.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from tenantchat.api.dependencies import (
    Audit,
    Registry,
    RequestId,
    SearchIndexes,
    TraceAccess,
    TurnRecords,
    get_settings,
)
from tenantchat.api.faults import ChatUnavailableError
from tenantchat.api.gold import load_gold_cases
from tenantchat.api.identity import (
    AdminIdentity,
    require_role,
    require_trace_read,
    verify_csrf,
)
from tenantchat.api.replay import (
    replay_trials,
    replay_turn,
    replay_with_retrieval,
    replay_with_template,
)
from tenantchat.api.schemas import (
    GoldCaseResponse,
    GoldCasesResponse,
    GoldEvidenceItem,
    ReplayRetrievalRequest,
    ReplayTemplateRequest,
    ReplayTrialsRequest,
    TraceAccessesResponse,
    TraceAccessRequest,
    TraceAccessResponse,
    TraceReadResponse,
    TraceReplayResponse,
    TraceReplayRetrievalResponse,
    TraceReplayTemplateResponse,
    TraceReplayTrialsResponse,
    TraceSearchResponsePage,
)
from tenantchat.api.store import AuditActorType, AuditEvent
from tenantchat.core.privacy import TurnRecordReadReason

router = APIRouter(tags=["admin-traces"])

logger = logging.getLogger(__name__)

_grants_access = require_role("platform_admin")
TraceReader = Annotated[AdminIdentity, Depends(require_trace_read())]
TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]
SubjectQuery = Annotated[str, Query(min_length=1, max_length=200)]
ManifestHashQuery = Annotated[
    str | None,
    Query(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
        description="The exact component-manifest SHA-256 a turn's record pins.",
    ),
]
CauseQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="A Gate B diagnosis cause; only records carrying it match.",
    ),
]
DiagnosisStatusQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*$",
        description="A diagnosis status (detected, suspected, confirmed, "
        "inconclusive); only records carrying it match.",
    ),
]
OutcomeQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z]+$",
        description="How the turn ended: answered, paused, escalated, abstained, clarified.",
    ),
]
RecordedSinceQuery = Annotated[
    datetime | None,
    Query(
        description="Only turns recorded at or after this instant, in the "
        "`OBS-001` clock's timezone.",
    ),
]
RecordedUntilQuery = Annotated[
    datetime | None,
    Query(
        description="Only turns recorded at or before this instant.",
    ),
]
TraceLimitQuery = Annotated[int, Query(ge=1, le=200)]
GenerationIdQuery = Annotated[
    uuid.UUID | None,
    Query(
        description="An ingestion index generation; only turns whose retrieval "
        "cited it match. `FEAT-001` links index-integrity findings to the turns "
        "grounded in the affected generation through this filter.",
    ),
]


def _authorized_grants(request: Request) -> AdminIdentity:
    """Admit a platform administrator and require the same-origin token.

    Raises:
        UnauthenticatedError: no usable operator identity.
        ForbiddenError: the operator is not a platform administrator.
        CsrfValidationError: the double-submit token is absent or wrong.
    """
    identity = _grants_access(request)
    verify_csrf(request, identity, get_settings(request))
    return identity


GrantsAdmin = Annotated[AdminIdentity, Depends(_authorized_grants)]
# Reading the grant list is a GET like every other trace read: the double-submit
# token defends state-changing requests, and sibling trace GETs carry no CSRF
# requirement, so this one aligns with them.
GrantsReader = Annotated[AdminIdentity, Depends(_grants_access)]


async def _audit_replay(
    audit: Audit,
    *,
    identity: AdminIdentity,
    tenant_id: str,
    action: str,
    turn_id: uuid.UUID,
    request_id: str,
    details: dict[str, object],
) -> None:
    """One accountability row for a replay attempt, success or failure (R-40).

    A replay that blew up must leave the same trail as one that answered, so
    the caller records twice on the failure path: once with ``outcome:
    "failed"``, once — after success — with the result's content-free fields.
    """
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action=action,
            resource_type="turn_record",
            resource_id=turn_id,
            request_id=request_id,
            details=details,
        )
    )


@router.get(
    "/api/admin/trace-access",
    response_model=TraceAccessesResponse,
)
async def list_trace_access(
    identity: GrantsReader,
    tenant_id: TenantIdQuery,
    registry: Registry,
    grants: TraceAccess,
) -> TraceAccessesResponse:
    """The tenant's current trace-read grants, for the operator console.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    return TraceAccessesResponse(
        grants=[TraceAccessResponse.of(grant) for grant in await grants.for_tenant(tenant_id)]
    )


@router.post(
    "/api/admin/trace-access",
    response_model=TraceAccessResponse,
    status_code=status.HTTP_201_CREATED,
)
async def grant_trace_access(
    identity: GrantsAdmin,
    payload: TraceAccessRequest,
    registry: Registry,
    grants: TraceAccess,
    audit: Audit,
    request_id: RequestId,
) -> TraceAccessResponse:
    """Grant one operator the dedicated turn-record read role for one tenant.

    The grant is tenant-qualified and separate from transcript memberships: a
    platform administrator decides who may read the inference plane, and the
    decision is audited with the granting principal. Re-granting is an
    idempotent upsert, exactly like membership assignment.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(payload.tenant_id)
    grant = await grants.grant(payload.tenant_id, payload.subject, granted_by=identity.subject)
    await audit.record(
        AuditEvent(
            tenant_id=payload.tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="trace_access.granted",
            resource_type="trace_access",
            resource_id=None,
            request_id=request_id,
            details={"subject": payload.subject},
        )
    )
    return TraceAccessResponse.of(grant)


@router.delete("/api/admin/trace-access", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_trace_access(
    identity: GrantsAdmin,
    tenant_id: TenantIdQuery,
    subject: SubjectQuery,
    registry: Registry,
    grants: TraceAccess,
    audit: Audit,
    request_id: RequestId,
) -> None:
    """Revoke an operator's trace-read role for one tenant.

    Revoking a grant that never existed is not an error — the operator ends up
    without access either way, and the audit row records that a platform
    administrator asked for it, matching the membership-revocation contract.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    await grants.revoke(tenant_id, subject)
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="trace_access.revoked",
            resource_type="trace_access",
            resource_id=None,
            request_id=request_id,
            details={"subject": subject},
        )
    )


@router.get("/api/admin/traces/gold-cases", response_model=GoldCasesResponse)
async def gold_cases(
    identity: TraceReader,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    registry: Registry,
    audit: Audit,
    request_id: RequestId,
) -> GoldCasesResponse:
    """The reviewer-labelled gold evidence the explorer overlays on a turn.

    Gold chunks are synthetic evaluation content, not visitor data, but they
    are evidence-like text, so this surface sits under the same dedicated role
    and audit rules as the inference plane itself: an operator who may not read
    a turn may not read the passages a turn is graded against.

    Registered before ``/api/admin/traces/{turn_id}``: the literal path would
    otherwise be shadowed by the id route's single segment.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
    """
    registry.get(tenant_id)
    cases = tuple(case for case in load_gold_cases() if case.tenant_id == tenant_id)
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="trace.gold_read",
            resource_type="eval_case",
            resource_id=None,
            request_id=request_id,
            details={"reason": reason.value, "matches": len(cases)},
        )
    )
    return GoldCasesResponse(
        cases=[
            GoldCaseResponse(
                case_id=case.case_id,
                tenant_id=case.tenant_id,
                scenario=case.scenario,
                query=case.query,
                gold_chunks=[
                    GoldEvidenceItem(source_id=chunk.source_id, text=chunk.text)
                    for chunk in case.gold_chunks
                ],
            )
            for case in cases
        ]
    )


@router.get(
    "/api/admin/traces/{turn_id}",
    response_model=TraceReadResponse,
)
async def read_turn_record(
    identity: TraceReader,
    turn_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    registry: Registry,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
) -> TraceReadResponse:
    """One turn record: the governed read of the inference plane.

    The dedicated role was checked before this route ran; the reason travels
    into the audit row, so every read is answerable as actor, turn, and reason.
    The record is served even when its content is empty — the envelope may
    outlive the purge of an unpopulated payload — and a record that belongs to
    another tenant is indistinguishable from one that never existed.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such turn record, or it belongs to another tenant.
    """
    registry.get(tenant_id)
    record = await turns.get(tenant_id, turn_id)
    projections = await turns.projections_for_turn(tenant_id, turn_id)
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="trace.read",
            resource_type="turn_record",
            resource_id=record.turn_id,
            request_id=request_id,
            details={"reason": reason.value},
        )
    )
    return TraceReadResponse.of(record, projections)


@router.get("/api/admin/traces", response_model=TraceSearchResponsePage)
async def search_turn_records(
    identity: TraceReader,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    registry: Registry,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
    manifest_hash: ManifestHashQuery = None,
    cause: CauseQuery = None,
    diagnosis_status: DiagnosisStatusQuery = None,
    outcome: OutcomeQuery = None,
    since: RecordedSinceQuery = None,
    until: RecordedUntilQuery = None,
    limit: TraceLimitQuery = 50,
    generation_id: GenerationIdQuery = None,
) -> TraceSearchResponsePage:
    """The `OBS-004` attribution surface: records matching content-free filters.

    Filters are the content-free projection only — component-manifest hash,
    diagnosis cause, diagnosis status, outcome, recorded time, and the cited
    index generation — so an operator can ask "which build answered these
    turns", "which turns are merely suspected of a citation error", "what
    failed this morning", or "which turns this index generation grounded"
    without the query touching the opaque content object. Every search is
    audited with the filter that ran, and results carry no content: the record
    itself is fetched through the single-read route.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
    """
    registry.get(tenant_id)
    records = await turns.search(
        tenant_id,
        manifest_hash=manifest_hash,
        causes=(cause,) if cause else (),
        statuses=(diagnosis_status,) if diagnosis_status else (),
        outcome=outcome,
        since=since,
        until=until,
        limit=limit,
        generation_ids=(generation_id,) if generation_id is not None else (),
    )
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="trace.search",
            resource_type="turn_record",
            resource_id=None,
            request_id=request_id,
            details={
                "reason": reason.value,
                "manifest_hash": manifest_hash,
                "cause": cause,
                "diagnosis_status": diagnosis_status,
                "outcome": outcome,
                "since": since.isoformat() if since else None,
                "until": until.isoformat() if until else None,
                "limit": limit,
                "generation_id": str(generation_id) if generation_id is not None else None,
                "matches": len(records),
            },
        )
    )
    return TraceSearchResponsePage.of(records)


@router.post(
    "/api/admin/traces/{turn_id}/replay",
    response_model=TraceReplayResponse,
)
async def replay_turn_record(
    identity: TraceReader,
    turn_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    registry: Registry,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
    request: Request,
) -> TraceReplayResponse:
    """One safe replay of one stored turn through the current model.

    Replaying is a state-changing surface — it launches a model call — so it
    carries the same double-submit token as every other admin mutation, on top
    of the dedicated trace-read role (which reads the ``tenant_id`` query
    parameter, like every other trace surface). The replay rebuilds the stored
    prompt (`reconstruct_prompt`), re-hashes it against the stored content
    hash, and sends it through the *current* model with no tools: no booking,
    lead, or handoff can be touched. The audit row records the manifest
    comparison, content-free, so "was this replayed against the same
    components" is answerable without reading the output.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such turn record, or it belongs to another tenant.
        ChatUnavailableError: this deployment composed no model to replay with.
        TraceReplayError: the stored record names no reconstructible prompt.
    """
    registry.get(tenant_id)
    verify_csrf(request, identity, get_settings(request))
    record = await turns.get(tenant_id, turn_id)
    model = request.app.state.chat_model
    if model is None:
        raise ChatUnavailableError
    evidence = request.app.state.evidence_source
    retriever = evidence.retriever_manifest if hasattr(evidence, "retriever_manifest") else None
    settings = get_settings(request)
    # Every replay attempt is audited, not only the ones that land: the
    # operator probing a replay that fails must leave the same trail as one
    # that succeeds.
    details: dict[str, object] = {"reason": reason.value, "outcome": "failed"}
    try:
        result = await replay_turn(
            record=record,
            model=model,
            retriever=retriever,
            replay_timeout_seconds=settings.replay_timeout_seconds,
        )
    except Exception:
        await _audit_replay(
            audit,
            identity=identity,
            tenant_id=tenant_id,
            action="trace.replay",
            turn_id=record.turn_id,
            request_id=request_id,
            details=details,
        )
        raise
    details |= {
        "outcome": "replayed",
        "manifest_changed": result.manifest_changed,
        "changed_components": [
            component.name for component in result.components if component.changed
        ],
    }
    await _audit_replay(
        audit,
        identity=identity,
        tenant_id=tenant_id,
        action="trace.replay",
        turn_id=record.turn_id,
        request_id=request_id,
        details=details,
    )
    return result


@router.post(
    "/api/admin/traces/{turn_id}/replay/trials",
    response_model=TraceReplayTrialsResponse,
)
async def replay_turn_trials(
    identity: TraceReader,
    turn_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    payload: ReplayTrialsRequest,
    registry: Registry,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
    request: Request,
) -> TraceReplayTrialsResponse:
    """Bounded repeated trials of one stored turn through the current model.

    Each trial sends the same stored prompt through the *current* model with no
    tools. The response is an aggregate — all trials listed, with an explicit
    stochastic label. ``trials`` is bounded at 5: an unbounded replay loop
    against a live model is a footgun.

    Each trial is audited like a single replay: actor, turn, reason, and the
    trial count.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such turn record, or it belongs to another tenant.
        ChatUnavailableError: this deployment composed no model to replay with.
        TraceReplayError: the stored record names no reconstructible prompt.
    """
    registry.get(tenant_id)
    verify_csrf(request, identity, get_settings(request))
    record = await turns.get(tenant_id, turn_id)
    model = request.app.state.chat_model
    if model is None:
        raise ChatUnavailableError
    evidence = request.app.state.evidence_source
    retriever = evidence.retriever_manifest if hasattr(evidence, "retriever_manifest") else None
    settings = get_settings(request)
    details: dict[str, object] = {
        "reason": reason.value,
        "outcome": "failed",
        "trials": payload.trials,
    }
    try:
        result = await replay_trials(
            record=record,
            model=model,
            retriever=retriever,
            trials=payload.trials,
            replay_timeout_seconds=settings.replay_timeout_seconds,
        )
    except Exception:
        await _audit_replay(
            audit,
            identity=identity,
            tenant_id=tenant_id,
            action="trace.replay_trials",
            turn_id=record.turn_id,
            request_id=request_id,
            details=details,
        )
        raise
    details |= {
        "outcome": "replayed",
        "manifest_changed": result.manifest_changed,
        "changed_components": [
            component.name for component in result.components if component.changed
        ],
        "trials": result.trial_count,
    }
    await _audit_replay(
        audit,
        identity=identity,
        tenant_id=tenant_id,
        action="trace.replay_trials",
        turn_id=record.turn_id,
        request_id=request_id,
        details=details,
    )
    return result


@router.post(
    "/api/admin/traces/{turn_id}/replay/retrieval",
    response_model=TraceReplayRetrievalResponse,
)
async def replay_turn_retrieval(
    identity: TraceReader,
    turn_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    payload: ReplayRetrievalRequest,
    registry: Registry,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
    request: Request,
    search_index: SearchIndexes,
) -> TraceReplayRetrievalResponse:
    """Generation-pinned retrieval replay, optionally with gold substitution.

    Checks that the stored index generation still has retained chunks. When it
    does not, the route refuses (400 ``generation_unavailable``) rather than
    silently replaying against current data — a replay that quietly changes its
    evidence is worse than no replay. When ``gold_evidence`` is provided, those
    passages replace the stored evidence before the prompt reaches the model.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such turn record, or it belongs to another tenant.
        ChatUnavailableError: this deployment composed no model to replay with.
        TraceReplayError: the stored record names no reconstructible prompt.
        GenerationUnavailableError: the index generation is gone.
    """
    registry.get(tenant_id)
    verify_csrf(request, identity, get_settings(request))
    record = await turns.get(tenant_id, turn_id)
    model = request.app.state.chat_model
    if model is None:
        raise ChatUnavailableError
    evidence = request.app.state.evidence_source
    retriever = evidence.retriever_manifest if hasattr(evidence, "retriever_manifest") else None
    settings = get_settings(request)

    content = record.content
    retrieval = content.get("retrieval")
    generation_id_str = None
    gen_uuid: uuid.UUID | None = None
    generation_exists = False
    if isinstance(retrieval, dict):
        gid = retrieval.get("generation_id")
        if gid is not None:
            try:
                gen_uuid = uuid.UUID(str(gid))
                generation_id_str = str(gid)
                if search_index is not None:
                    generation_exists = bool(
                        await search_index.generation_chunks(
                            tenant_id=tenant_id, generation_id=gen_uuid
                        )
                    )
            except ValueError:
                pass

    gold_evidence: list[dict[str, str]] | None = None
    if payload.gold_evidence:
        gold_evidence = [
            {"source_id": item.source_id, "text": item.text} for item in payload.gold_evidence
        ]

    details: dict[str, object] = {
        "reason": reason.value,
        "generation_id": generation_id_str,
        "generation_exists": generation_exists,
        "gold_evidence_count": len(gold_evidence) if gold_evidence else 0,
        "outcome": "failed",
    }
    try:
        retrieved_evidence: list[dict[str, str]] | None = None
        if generation_exists and gold_evidence is None and gen_uuid is not None:
            replay_generation = getattr(evidence, "replay_generation", None)
            if replay_generation is not None:
                query = ""
                if isinstance(retrieval, dict):
                    query = str(retrieval.get("resolved_query") or retrieval.get("query") or "")
                retrieved_evidence = await replay_generation(
                    tenant_id=tenant_id,
                    query=query,
                    generation_id=gen_uuid,
                )

        result = await replay_with_retrieval(
            record=record,
            model=model,
            retriever=retriever,
            generation_exists=generation_exists,
            retrieved_evidence=retrieved_evidence,
            gold_evidence=gold_evidence,
            replay_timeout_seconds=settings.replay_timeout_seconds,
        )
    except Exception:
        await _audit_replay(
            audit,
            identity=identity,
            tenant_id=tenant_id,
            action="trace.replay_retrieval",
            turn_id=record.turn_id,
            request_id=request_id,
            details=details,
        )
        raise
    details["outcome"] = "replayed"
    await _audit_replay(
        audit,
        identity=identity,
        tenant_id=tenant_id,
        action="trace.replay_retrieval",
        turn_id=record.turn_id,
        request_id=request_id,
        details=details,
    )
    return result


@router.post(
    "/api/admin/traces/{turn_id}/replay/template",
    response_model=TraceReplayTemplateResponse,
)
async def replay_turn_template(
    identity: TraceReader,
    turn_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    payload: ReplayTemplateRequest,
    registry: Registry,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
    request: Request,
) -> TraceReplayTemplateResponse:
    """Template-version-pinned replay with model, evidence, and history constant.

    Renders the selected retained template using the stored binding values and
    sends the counterfactual prompt through the current model with no tools.

    The response carries ``template_ref`` (the version actually used) and
    ``template_matches_current`` (``True`` when the pinned version is the
    same as the deployment's current version).

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such turn record, or it belongs to another tenant.
        ChatUnavailableError: this deployment composed no model to replay with.
        TraceReplayError: the stored record names no reconstructible prompt.
    """
    registry.get(tenant_id)
    verify_csrf(request, identity, get_settings(request))
    record = await turns.get(tenant_id, turn_id)
    model = request.app.state.chat_model
    if model is None:
        raise ChatUnavailableError
    evidence = request.app.state.evidence_source
    retriever = evidence.retriever_manifest if hasattr(evidence, "retriever_manifest") else None
    settings = get_settings(request)
    details: dict[str, object] = {
        "reason": reason.value,
        "outcome": "failed",
        "template_version": payload.template_version,
    }
    try:
        result = await replay_with_template(
            record=record,
            model=model,
            retriever=retriever,
            template_version=payload.template_version,
            replay_timeout_seconds=settings.replay_timeout_seconds,
        )
    except Exception:
        await _audit_replay(
            audit,
            identity=identity,
            tenant_id=tenant_id,
            action="trace.replay_template",
            turn_id=record.turn_id,
            request_id=request_id,
            details=details,
        )
        raise
    details |= {
        "outcome": "replayed",
        "manifest_changed": result.manifest_changed,
        "changed_components": [
            component.name for component in result.components if component.changed
        ],
        "template_ref": result.template_ref,
        "template_matches_current": result.template_matches_current,
    }
    await _audit_replay(
        audit,
        identity=identity,
        tenant_id=tenant_id,
        action="trace.replay_template",
        turn_id=record.turn_id,
        request_id=request_id,
        details=details,
    )
    return result


@router.get("/api/admin/traces/by-trace-id/{trace_id}", response_model=TraceReadResponse)
async def read_turn_record_by_trace_id(
    identity: TraceReader,
    trace_id: str,
    tenant_id: TenantIdQuery,
    reason: TurnRecordReadReason,
    registry: Registry,
    turns: TurnRecords,
    audit: Audit,
    request_id: RequestId,
) -> TraceReadResponse:
    """The record the `OBS-001` correlation id names, under the same gates.

    This is the lookup a distributed trace answers with: given the request's
    trace id, the full inference record of the turn it produced — audited like
    any other read.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
        NotFoundError: no such record, or it belongs to another tenant.
    """
    registry.get(tenant_id)
    record = await turns.for_trace_id(tenant_id, trace_id)
    projections = await turns.projections_for_turn(tenant_id, record.turn_id)
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="trace.read",
            resource_type="turn_record",
            resource_id=record.turn_id,
            request_id=request_id,
            details={"reason": reason.value, "trace_id": trace_id},
        )
    )
    return TraceReadResponse.of(record, projections)
