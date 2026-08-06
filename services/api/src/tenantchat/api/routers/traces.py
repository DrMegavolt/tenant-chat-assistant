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
its governance surface, not its content model.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from tenantchat.api.dependencies import (
    Audit,
    Registry,
    RequestId,
    TraceAccess,
    TurnRecords,
    get_settings,
)
from tenantchat.api.identity import (
    AdminIdentity,
    require_role,
    require_trace_read,
    verify_csrf,
)
from tenantchat.api.schemas import (
    TraceAccessesResponse,
    TraceAccessRequest,
    TraceAccessResponse,
    TraceReadResponse,
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
OutcomeQuery = Annotated[
    str | None,
    Query(
        min_length=1,
        max_length=32,
        pattern=r"^[a-z]+$",
        description="How the turn ended: answered, paused, escalated, abstained, clarified.",
    ),
]
TraceLimitQuery = Annotated[int, Query(ge=1, le=200)]


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


@router.get(
    "/api/admin/trace-access",
    response_model=TraceAccessesResponse,
)
async def list_trace_access(
    identity: GrantsAdmin,
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
    outcome: OutcomeQuery = None,
    limit: TraceLimitQuery = 50,
) -> TraceSearchResponsePage:
    """The `OBS-004` attribution surface: records matching content-free filters.

    Filters are the content-free projection only — component-manifest hash,
    diagnosis cause, outcome — so an operator can ask "which build answered
    these turns" or "which turns attributed a citation error" without the
    query touching the opaque content object. Every search is audited with
    the filter that ran, and results carry no content: the record itself is
    fetched through the single-read route.

    Raises:
        ForbiddenError: the operator holds no trace-read grant for the tenant.
    """
    registry.get(tenant_id)
    records = await turns.search(
        tenant_id,
        manifest_hash=manifest_hash,
        causes=(cause,) if cause else (),
        outcome=outcome,
        limit=limit,
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
                "outcome": outcome,
                "limit": limit,
                "matches": len(records),
            },
        )
    )
    return TraceSearchResponsePage.of(records)


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
