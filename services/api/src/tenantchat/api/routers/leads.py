"""Lead capture.

Only the write side. Reading captured leads exposes customer contact details, so
that endpoint waits for the admin authentication and tenant-scoped RBAC in
`SEC-001`; there is no unauthenticated way to list them in the meantime.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from tenantchat.api.dependencies import Leads, Registry
from tenantchat.api.schemas import LeadRequest, LeadResponse
from tenantchat.core.commands import LeadCommand

router = APIRouter(tags=["leads"])


@router.post(
    "/api/leads",
    response_model=LeadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_lead(
    payload: LeadRequest,
    registry: Registry,
    leads: Leads,
) -> LeadResponse:
    """Capture a request for a human callback.

    Raises:
        NotFoundError: no such tenant.
        LeadCaptureNotPermittedError: this tenant does not capture leads.
        MissingRequiredFieldsError: a required field was empty.
        InvalidContactError: the contact is not a valid email or NANP number.
    """
    record = registry.get(payload.tenant_id)

    command = LeadCommand.parse(
        record.policy,
        customer_name=payload.customer_name,
        contact=payload.contact,
        service=payload.service,
        summary=payload.summary,
        address_or_zip=payload.address_or_zip,
        urgency=payload.urgency,
    )

    return LeadResponse.of(await leads.record(command, session_id=payload.session_id))
