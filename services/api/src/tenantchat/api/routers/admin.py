"""The operator console surface (SEC-001 tenant-scoped RBAC).

Every route here reads or writes another person's conversation, so all of them
require an identity the gateway established and a role this service re-checks —
see :mod:`tenantchat.api.identity`. None of them is reachable through CORS: the
allowlist in the composition root covers the widget origins, and an operator's
browser talks to this API same-origin through the gateway.

Tenant scoping resolves one operator's per-tenant role from the membership
store before any tenant record is touched. A refused operator gets the same
document whether the tenant exists or not, so the surface cannot be used to
enumerate tenants.

Administrative mutations — staff replies and membership assignment — write an
audit row carrying the principal, tenant, request ID, and timestamp.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from tenantchat.api.dependencies import (
    Audit,
    Bookings,
    Configuration,
    Conversations,
    Handoffs,
    Leads,
    Memberships,
    Registry,
    RequestId,
    get_settings,
)
from tenantchat.api.faults import ForbiddenError
from tenantchat.api.identity import (
    AdminIdentity,
    authorize_tenant_access,
    csrf_token,
    require_role,
    tenant_scoped,
    verify_csrf,
)
from tenantchat.api.schemas import (
    AdminBooking,
    AdminBookingsResponse,
    AdminLead,
    AdminLeadsResponse,
    AdminSessionsResponse,
    AdminTenantsResponse,
    AdminTenantSummary,
    ChatSessionResponse,
    ChatSessionSummary,
    CsrfTokenResponse,
    MembershipRequest,
    MembershipResponse,
    StaffMessageRequest,
    StaffMessageResponse,
    TranscriptMessage,
)
from tenantchat.api.store import AuditActorType, AuditEvent, MessageRole
from tenantchat.core.errors import NotFoundError

router = APIRouter(tags=["admin"])

logger = logging.getLogger(__name__)

_read_access = require_role("viewer")
_reply_access = require_role("support_agent")
_admin_mutation_access = require_role("platform_admin")
_tenant_read = tenant_scoped("viewer")

TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]
PageSize = Annotated[int, Query(ge=1, le=200)]


def _audit_event(
    *,
    tenant_id: str,
    principal_id: str,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None,
    request_id: str,
    details: dict[str, object],
) -> AuditEvent:
    """One accountability record; the store stamps the timestamp."""
    return AuditEvent(
        tenant_id=tenant_id,
        actor_type=AuditActorType.STAFF,
        principal_id=principal_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        details=details,
    )


async def _authorized_reply(
    request: Request,
    payload: StaffMessageRequest,
    memberships: Memberships,
) -> AdminIdentity:
    """Admit an operator who may reply to this tenant, same-origin.

    The role, the tenant, and the CSRF token answer different questions. The
    role says this operator is allowed to speak to customers at all; the
    membership says they may speak to *this tenant's* customers; the token says
    this particular request came from the console rather than from a page that
    merely knows the operator's browser holds a session.

    Raises:
        UnauthenticatedError: no usable operator identity.
        ForbiddenError: the operator may not send staff replies.
        TenantAccessDeniedError: no membership grants access to the tenant.
        CsrfValidationError: the double-submit token is absent or wrong.
    """
    identity = _reply_access(request)
    verify_csrf(request, identity, get_settings(request))
    await authorize_tenant_access(
        identity,
        memberships,
        payload.tenant_id,
        minimum="support_agent",
        path=request.url.path,
    )
    return identity


async def _authorized_admin_mutation(request: Request) -> AdminIdentity:
    """Admit a platform administrator performing a state-changing operation.

    Raises:
        UnauthenticatedError: no usable operator identity.
        ForbiddenError: the operator is not a platform administrator.
        CsrfValidationError: the double-submit token is absent or wrong.
    """
    identity = _admin_mutation_access(request)
    verify_csrf(request, identity, get_settings(request))
    return identity


Reader = Annotated[AdminIdentity, Depends(_read_access)]
TenantReader = Annotated[AdminIdentity, Depends(_tenant_read)]
Replier = Annotated[AdminIdentity, Depends(_authorized_reply)]
AdminMutation = Annotated[AdminIdentity, Depends(_authorized_admin_mutation)]


@router.get("/api/admin/csrf-token", response_model=CsrfTokenResponse)
def issue_csrf_token(identity: Reader, settings: Configuration) -> CsrfTokenResponse:
    """Mint the token the console must echo on state-changing requests.

    Readable by any authenticated operator: the token authorizes nothing on its
    own, and one derived for a viewer is useless without the role a write also
    requires.

    Raises:
        CsrfValidationError: no CSRF secret is configured, so no acceptable
            token exists.
    """
    return CsrfTokenResponse(csrf_token=csrf_token(identity, settings))


@router.get("/api/admin/tenants", response_model=AdminTenantsResponse)
async def list_accessible_tenants(
    identity: Reader,
    memberships: Memberships,
    registry: Registry,
) -> AdminTenantsResponse:
    """The tenants this operator may work, and the role they hold in each.

    A platform administrator spans every tenant by definition; any other
    operator sees exactly their membership rows. This is the surface the
    console's tenant picker reads, so an operator is never offered a tenant
    they cannot open.
    """
    if identity.role == "platform_admin":
        tenants = [
            AdminTenantSummary(
                tenant_id=record.policy.tenant_id,
                name=record.policy.name,
                role="platform_admin",
            )
            for record in registry.all().values()
        ]
    else:
        tenants = []
        for membership in await memberships.for_principal(identity.subject):
            try:
                record = registry.get(membership.tenant_id)
            except NotFoundError:
                continue
            tenants.append(
                AdminTenantSummary(
                    tenant_id=record.policy.tenant_id,
                    name=record.policy.name,
                    role=membership.role,
                )
            )
    tenants.sort(key=lambda tenant: tenant.tenant_id)
    return AdminTenantsResponse(tenants=tenants)


@router.get("/api/admin/chats", response_model=AdminSessionsResponse)
async def list_chats(
    identity: TenantReader,
    tenant_id: TenantIdQuery,
    registry: Registry,
    conversations: Conversations,
    limit: PageSize = 50,
) -> AdminSessionsResponse:
    """Conversations for one tenant, most recently active first.

    Summaries only. Listing is how an operator finds work, and a list endpoint
    that returned transcripts would put every customer's words into a response
    nobody asked for.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    records = await conversations.for_tenant(tenant_id, limit=limit)
    return AdminSessionsResponse(
        sessions=[ChatSessionSummary.of(record) for record in records], limit=limit
    )


@router.get("/api/admin/chats/{session_id}", response_model=ChatSessionResponse)
async def read_chat(
    identity: TenantReader,
    session_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    conversations: Conversations,
) -> ChatSessionResponse:
    """One conversation and its full transcript.

    Raises:
        NotFoundError: no such conversation, or it belongs to another tenant.
    """
    record = await conversations.get(tenant_id, session_id)
    messages = await conversations.transcript(tenant_id, session_id)
    return ChatSessionResponse(
        session=ChatSessionSummary.of(record),
        messages=[TranscriptMessage.of(message) for message in messages],
    )


@router.get("/api/admin/leads", response_model=AdminLeadsResponse)
async def list_leads(
    identity: TenantReader,
    tenant_id: TenantIdQuery,
    registry: Registry,
    leads: Leads,
) -> AdminLeadsResponse:
    """Captured leads for one tenant, oldest first.

    The visitor write side has no read counterpart: contact values are PII, and
    this is the only surface that publishes them. It is the reason the backlog
    kept the prototype's authenticated lead read out of the cutover — it needs
    the tenant membership check this route now enforces.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    return AdminLeadsResponse(
        leads=[AdminLead.of(record) for record in await leads.for_tenant(tenant_id)]
    )


@router.get("/api/admin/bookings", response_model=AdminBookingsResponse)
async def list_bookings(
    identity: TenantReader,
    tenant_id: TenantIdQuery,
    registry: Registry,
    bookings: Bookings,
) -> AdminBookingsResponse:
    """Confirmed bookings for one tenant, oldest first.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    return AdminBookingsResponse(
        bookings=[AdminBooking.of(record) for record in await bookings.for_tenant(tenant_id)]
    )


@router.post(
    "/api/admin/chats/{session_id}/messages",
    response_model=StaffMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_staff_message(
    identity: Replier,
    session_id: uuid.UUID,
    payload: StaffMessageRequest,
    conversations: Conversations,
    handoffs: Handoffs,
    audit: Audit,
    request_id: RequestId,
) -> StaffMessageResponse:
    """Say something to the customer as a person.

    Stored with the ``staff`` role, distinct from ``assistant``: a reply a human
    wrote and one a model produced carry different weight for the customer
    reading them and for anyone auditing what was promised.

    Single ownership (`FEAT-004`) is enforced here, not just by the queue UI:
    a conversation another staff member currently holds accepts replies only
    from that owner, so two operators cannot talk over each other into one
    transcript. A conversation no one holds — no handoff, or one still waiting
    in the queue — stays replyable by any staff member, exactly as before.

    The message does not enter the model's view of the conversation. Feeding
    staff replies back into the agent's transcript is `FEAT-004`, which owns the
    handoff lifecycle and the question of whether the assistant should resume at
    all once a person has taken over.

    The audit row is what makes the reply an accountability record rather than a
    row in a table: principal, tenant, request ID, and a server timestamp are
    stored with it. `PRIV-001` owns retention and erasure of these rows.

    Raises:
        NotFoundError: no such conversation, or it belongs to another tenant.
        ForbiddenError: a different staff member currently holds this
            conversation.
    """
    handoff = await handoffs.for_session(payload.tenant_id, str(session_id))
    if (
        handoff is not None
        and handoff.status == "assigned"
        and handoff.assigned_principal_id != identity.subject
    ):
        raise ForbiddenError
    record = await conversations.append(
        payload.tenant_id,
        session_id,
        role=MessageRole.STAFF,
        content=payload.content,
        metadata={"operator_subject": identity.subject},
    )
    await audit.record(
        _audit_event(
            tenant_id=payload.tenant_id,
            principal_id=identity.subject,
            action="staff_reply_sent",
            resource_type="chat_session",
            resource_id=session_id,
            request_id=request_id,
            details={"message_id": str(record.message_id)},
        )
    )
    logger.info(
        "staff reply recorded",
        extra={
            "subject": identity.subject,
            "tenant_id": payload.tenant_id,
            "session_id": str(session_id),
        },
    )
    return StaffMessageResponse(message=TranscriptMessage.of(record))


@router.post(
    "/api/admin/memberships",
    response_model=MembershipResponse,
    status_code=status.HTTP_201_CREATED,
)
async def assign_membership(
    identity: AdminMutation,
    payload: MembershipRequest,
    memberships: Memberships,
    registry: Registry,
    audit: Audit,
    request_id: RequestId,
) -> MembershipResponse:
    """Grant or update one operator's role inside one tenant.

    Platform administrators only: the power to grant roles is the highest role
    there is. Re-assigning a role replaces the previous one (upsert), and every
    change is audited with the assigning principal.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(payload.tenant_id)
    record = await memberships.assign(
        tenant_id=payload.tenant_id, subject=payload.subject, role=payload.role
    )
    await audit.record(
        _audit_event(
            tenant_id=payload.tenant_id,
            principal_id=identity.subject,
            action="membership_assigned",
            resource_type="tenant_membership",
            resource_id=None,
            request_id=request_id,
            details={"subject": payload.subject, "role": payload.role},
        )
    )
    return MembershipResponse.of(record)


@router.delete("/api/admin/memberships", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_membership(
    identity: AdminMutation,
    tenant_id: TenantIdQuery,
    subject: Annotated[str, Query(min_length=1, max_length=200)],
    memberships: Memberships,
    registry: Registry,
    audit: Audit,
    request_id: RequestId,
) -> None:
    """Remove an operator's access to one tenant.

    Revoking an assignment that never existed is not an error: the operator
    ends up without access either way, and the audit row records that a
    platform administrator asked for it.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    await memberships.revoke(tenant_id=tenant_id, subject=subject)
    await audit.record(
        _audit_event(
            tenant_id=tenant_id,
            principal_id=identity.subject,
            action="membership_revoked",
            resource_type="tenant_membership",
            resource_id=None,
            request_id=request_id,
            details={"subject": subject},
        )
    )
