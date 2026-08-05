"""The operator-facing privacy surface: export and the erasure queue.

Everything here is about another person's data, so every route requires an
identity the gateway established and a role this service re-checks — see
:mod:`tenantchat.api.identity`. Export is a read of everything the platform
holds about one subject; deletion requests are *requests*: the erasure worker
fulfills them with the erasure role's credentials, because the application
role holds no ``DELETE`` on sessions or transcripts.

The response models deliberately carry the contact in canonical form: an
operator who files a request against a number needs to see it echoed back.
The queue list redacts completed requests — the store anonymizes their contact
value when the worker finishes, so a page of history does not hoard details.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from tenantchat.api.dependencies import Audit, Jobs, Privacy, Registry, RequestId, get_settings
from tenantchat.api.identity import AdminIdentity, require_role, verify_csrf
from tenantchat.api.jobs import JobKind
from tenantchat.api.schemas import (
    BookingExportItem,
    ConsentExportItem,
    ContactQuery,
    DeletionRequestResponse,
    DeletionRequestsResponse,
    HandoffExportItem,
    LeadExportItem,
    PrivacyExportResponse,
    SessionExportItem,
    TranscriptMessage,
    TurnRecordExportItem,
    TurnRecordProjectionExportItem,
)
from tenantchat.api.store import AuditActorType, AuditEvent
from tenantchat.core.contact import Contact

router = APIRouter(tags=["admin-privacy"])

_read_access = require_role("tenant_admin")
_queue_access = require_role("platform_admin")


def _authorized(request: Request, role: AdminIdentity) -> AdminIdentity:
    """Admit an operator and require a same-origin double-submit token.

    Raises:
        UnauthenticatedError: no usable operator identity.
        ForbiddenError: the operator lacks the role this route needs.
        CsrfValidationError: the double-submit token is absent or wrong.
    """
    verify_csrf(request, role, get_settings(request))
    return role


Exporter = Annotated[AdminIdentity, Depends(_read_access)]
Eraser = Annotated[AdminIdentity, Depends(_queue_access)]
TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]


@router.post("/api/admin/privacy/export", response_model=PrivacyExportResponse)
async def export_subject(
    identity: Exporter,
    payload: ContactQuery,
    registry: Registry,
    privacy: Privacy,
    audit: Audit,
    request_id: RequestId,
) -> PrivacyExportResponse:
    """Everything the platform holds about one subject, for one tenant.

    The subject is a contact value, matched against leads, bookings, and
    transcript content. The response is complete on purpose: a partial export
    would be taken for the whole truth of a rights request.

    Raises:
        NotFoundError: no such tenant, or the contact is not well-formed.
    """
    registry.get(payload.tenant_id)
    contact = Contact.parse(payload.contact)
    session_ids = await privacy.sessions_for_contact(payload.tenant_id, contact)
    records = await privacy.subject_records(payload.tenant_id, session_ids)
    await audit.record(
        AuditEvent(
            tenant_id=payload.tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="privacy.export",
            resource_type="privacy_export",
            resource_id=None,
            request_id=request_id,
            details={"sessions": len(session_ids), "contact_kind": contact.kind.value},
        )
    )
    return PrivacyExportResponse(
        tenant_id=payload.tenant_id,
        contact_kind=contact.kind.value,
        contact_value=contact.value,
        requested_by=identity.subject,
        generated_at=datetime.now(UTC),
        sessions=[SessionExportItem.of(record) for record in records.sessions],
        messages=[TranscriptMessage.of(record) for record in records.messages],
        leads=[LeadExportItem.of(record) for record in records.leads],
        bookings=[BookingExportItem.of(record) for record in records.bookings],
        handoffs=[HandoffExportItem.of(record) for record in records.handoffs],
        consent=[ConsentExportItem.of(record) for record in records.consent],
        turn_records=[TurnRecordExportItem.of(record) for record in records.turn_records],
        projections=[TurnRecordProjectionExportItem.of(record) for record in records.projections],
    )


@router.post(
    "/api/admin/privacy/deletion-requests",
    response_model=DeletionRequestResponse,
    status_code=status.HTTP_201_CREATED,
)
async def request_deletion(
    identity: Eraser,
    payload: ContactQuery,
    registry: Registry,
    privacy: Privacy,
    jobs: Jobs,
    audit: Audit,
    request_id: RequestId,
) -> DeletionRequestResponse:
    """Queue a deletion request for the erasure worker.

    Filing does not erase anything: the worker processes the queue under the
    erasure role, and the request stays visible in the queue until it does.
    An audit row records the filing, and the worker's own audit rows record
    the erasure, so the two events can be matched by the request id.

    Raises:
        NotFoundError: no such tenant, or the contact is not well-formed.
    """
    registry.get(payload.tenant_id)
    contact = Contact.parse(payload.contact)
    record = await privacy.create_privacy_request(
        payload.tenant_id, contact=contact, requested_by=identity.subject
    )
    # The job key is the durable domain request ID. A retry of this HTTP path or
    # a process crash after enqueue can only resolve the same work item.
    await jobs.enqueue(
        payload.tenant_id,
        kind=JobKind.PRIVACY_DELETION,
        payload={"request_id": str(record.request_id)},
        idempotency_key=str(record.request_id),
    )
    await audit.record(
        AuditEvent(
            tenant_id=payload.tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="privacy.deletion_requested",
            resource_type="privacy_request",
            resource_id=record.request_id,
            request_id=request_id,
            details={},
        )
    )
    return DeletionRequestResponse.of(record)


@router.get(
    "/api/admin/privacy/deletion-requests",
    response_model=DeletionRequestsResponse,
)
async def list_deletion_requests(
    identity: Eraser,
    tenant_id: TenantIdQuery,
    registry: Registry,
    privacy: Privacy,
) -> DeletionRequestsResponse:
    """The tenant's erasure queue, newest first.

    Completed requests echo an anonymized contact (the store erases the value
    when the worker finishes), so the queue is not a page of hoarded numbers.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    records = await privacy.requests_for_tenant(tenant_id)
    return DeletionRequestsResponse(
        requests=[DeletionRequestResponse.of(record) for record in records]
    )
