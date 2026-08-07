"""The `FEAT-004` staff handoff surface: the queue and every ownership action.

Every route here works a conversation that left the assistant, so all of them
require the operator's per-tenant role and the double-submit token — a staff
member who may view transcripts is not automatically one who may take a
conversation. The queue list is the ticket's own escalation state and summary;
the assignment fields are accountability data for the people working the
queue, never anything a visitor surface returns.

The ownership transaction lives in the store behind a conditional update, so
two operators accepting the same handoff have exactly one winner no matter
which consoles fired. A refused transition is a conflict the operator reloads
from, and every accept, release, and resolution is audited to a principal, a
tenant, a request ID, and a server timestamp (acceptance 2).
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, status

from tenantchat.api.dependencies import (
    Audit,
    Handoffs,
    Memberships,
    Registry,
    RequestId,
    get_settings,
)
from tenantchat.api.identity import (
    AdminIdentity,
    authorize_tenant_access,
    effective_role,
    require_role,
    tenant_scoped,
    verify_csrf,
)
from tenantchat.api.schemas import AdminHandoff, AdminHandoffsResponse, HandoffActionResponse
from tenantchat.api.store import AuditActorType, AuditEvent

router = APIRouter(tags=["admin-handoffs"])

TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]
# The queue's public identifier is the "HO-..." reference the assistant's
# committed actions already surface; the row UUID it names is the store's
# concern, not the caller's.
HandoffId = Annotated[str, Path(pattern=r"^HO-[0-9A-F]{32}$", alias="handoff_id")]
QueueLimit = Annotated[int, Query(ge=1, le=200)]

# The whole surface is a work surface: the queue is where staff pick up a
# conversation, so a viewer — who may read transcripts but never reply — has
# nothing to do here.
TenantWorker = Annotated[AdminIdentity, Depends(tenant_scoped("support_agent"))]


def _audit_event(
    *,
    tenant_id: str,
    principal_id: str,
    action: str,
    handoff_id: str,
    session_id: str,
    request_id: str,
    details: dict[str, object] | None = None,
) -> AuditEvent:
    """One accountability record; the store stamps the timestamp."""
    return AuditEvent(
        tenant_id=tenant_id,
        actor_type=AuditActorType.STAFF,
        principal_id=principal_id,
        action=action,
        resource_type="handoff",
        resource_id=uuid.UUID(handoff_id.removeprefix("HO-")),
        request_id=request_id,
        details={"session_id": session_id} | (details or {}),
    )


async def _worker_authorization(
    request: Request,
    identity: AdminIdentity,
    tenant_id: str,
    memberships: Memberships,
) -> tuple[bool, AdminIdentity]:
    """Admit an operator who may work this tenant's handoffs.

    Returns whether the operator's effective role is supervisory enough to
    release or resolve someone else's assignment — the staff-disconnect
    recovery path. Authorization is always the tighter of the directory role
    and the membership row (`SEC-001`).

    Raises:
        UnauthenticatedError: no usable operator identity.
        ForbiddenError: the operator may not work handoffs.
        TenantAccessDeniedError: no membership grants access to the tenant.
        CsrfValidationError: the double-submit token is absent or wrong.
    """
    verify_csrf(request, identity, get_settings(request))
    await authorize_tenant_access(
        identity,
        memberships,
        tenant_id,
        minimum="support_agent",
        path=request.url.path,
    )
    membership_role = await memberships.role_for(tenant_id, identity.subject)
    role = effective_role(identity, membership_role)
    return role in ("tenant_admin", "platform_admin"), identity


@router.get("/api/admin/handoffs", response_model=AdminHandoffsResponse)
async def list_handoffs(
    identity: TenantWorker,
    tenant_id: TenantIdQuery,
    registry: Registry,
    handoffs: Handoffs,
    limit: QueueLimit = 50,
) -> AdminHandoffsResponse:
    """The tenant's open handoff queue, oldest first.

    Resolved and cancelled rows are history, not work, and stay out of the
    queue. The list is the escalation ticket itself — reason, summary, and the
    assignment state that says whether it is takeable.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    rows = await handoffs.open_for_tenant(tenant_id, limit=limit)
    return AdminHandoffsResponse(
        handoffs=[AdminHandoff.of(record) for record in rows],
        operator_subject=identity.subject,
        limit=limit,
    )


@router.post(
    "/api/admin/handoffs/{handoff_id}/accept",
    response_model=HandoffActionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def accept_handoff(
    request: Request,
    identity: Annotated[AdminIdentity, Depends(require_role("support_agent"))],
    handoff_id: HandoffId,
    tenant_id: TenantIdQuery,
    registry: Registry,
    handoffs: Handoffs,
    memberships: Memberships,
    audit: Audit,
    request_id: RequestId,
) -> HandoffActionResponse:
    """Take ownership of an unowned handoff.

    The accept is the ownership transaction the acceptance criterion pins: the
    store's conditional update means a second console accepting the same
    handoff at the same moment reads the first one's committed assignment and
    is refused rather than overwriting it. The visitor is told a team member
    has joined, with no staff identity attached.

    Raises:
        NotFoundError: no such handoff, or it belongs to another tenant.
        HandoffTransitionError: the handoff already has an owner or closed.
    """
    registry.get(tenant_id)
    await _worker_authorization(request, identity, tenant_id, memberships)
    record = await handoffs.accept(tenant_id, str(handoff_id), principal_id=identity.subject)
    await audit.record(
        _audit_event(
            tenant_id=tenant_id,
            principal_id=identity.subject,
            action="handoff.accepted",
            handoff_id=handoff_id,
            session_id=record.session_id,
            request_id=request_id,
        )
    )
    return HandoffActionResponse(handoff=AdminHandoff.of(record))


@router.post(
    "/api/admin/handoffs/{handoff_id}/release",
    response_model=HandoffActionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def release_handoff(
    request: Request,
    identity: Annotated[AdminIdentity, Depends(require_role("support_agent"))],
    handoff_id: HandoffId,
    tenant_id: TenantIdQuery,
    registry: Registry,
    handoffs: Handoffs,
    memberships: Memberships,
    audit: Audit,
    request_id: RequestId,
) -> HandoffActionResponse:
    """Release an assigned handoff back to the queue and resume the assistant.

    The current owner releases it, or a supervisor releases a colleague's stale
    assignment after a disconnect. The graph's idempotent services keep the
    resumed conversation from committing a second time.

    Raises:
        NotFoundError: no such handoff, or it belongs to another tenant.
        HandoffTransitionError: not assigned, or held by another owner and the
            caller is not a supervisor.
    """
    registry.get(tenant_id)
    administrative, identity = await _worker_authorization(
        request, identity, tenant_id, memberships
    )
    record = await handoffs.release(
        tenant_id, str(handoff_id), principal_id=identity.subject, administrative=administrative
    )
    await audit.record(
        _audit_event(
            tenant_id=tenant_id,
            principal_id=identity.subject,
            action="handoff.released",
            handoff_id=handoff_id,
            session_id=record.session_id,
            request_id=request_id,
            details={"administrative": administrative},
        )
    )
    return HandoffActionResponse(handoff=AdminHandoff.of(record))


@router.post(
    "/api/admin/handoffs/{handoff_id}/resolve",
    response_model=HandoffActionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def resolve_handoff(
    request: Request,
    identity: Annotated[AdminIdentity, Depends(require_role("support_agent"))],
    handoff_id: HandoffId,
    tenant_id: TenantIdQuery,
    registry: Registry,
    handoffs: Handoffs,
    memberships: Memberships,
    audit: Audit,
    request_id: RequestId,
) -> HandoffActionResponse:
    """Close an open handoff and mark the conversation closed.

    The terminal state the OBS-005 vocabulary models: the conversation ends
    with its ``handoff`` outcome and a ``closed`` session status, and the
    visitor's next turn carries the closure notice rather than a model answer.

    Raises:
        NotFoundError: no such handoff, or it belongs to another tenant.
        HandoffTransitionError: the handoff is not open, or is held by another
            owner and the caller is not a supervisor.
    """
    registry.get(tenant_id)
    administrative, identity = await _worker_authorization(
        request, identity, tenant_id, memberships
    )
    record = await handoffs.resolve(
        tenant_id, str(handoff_id), principal_id=identity.subject, administrative=administrative
    )
    await audit.record(
        _audit_event(
            tenant_id=tenant_id,
            principal_id=identity.subject,
            action="handoff.resolved",
            handoff_id=handoff_id,
            session_id=record.session_id,
            request_id=request_id,
            details={"administrative": administrative},
        )
    )
    return HandoffActionResponse(handoff=AdminHandoff.of(record))
