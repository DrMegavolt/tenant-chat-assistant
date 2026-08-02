"""Booking submission.

The handler is deliberately thin. It resolves the tenant, hands the raw strings
to ``BookingCommand.parse``, and records what comes back. Every rule that decides
whether the booking is allowed lives in the command, so the model-driven tool
call added in `ARCH-001` gets the identical checks without this module.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from tenantchat.api.dependencies import Bookings, Registry
from tenantchat.api.schemas import BookingRequest, BookingResponse
from tenantchat.core.commands import BookingCommand

router = APIRouter(tags=["bookings"])


@router.post(
    "/api/book",
    response_model=BookingResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_booking(
    payload: BookingRequest,
    registry: Registry,
    bookings: Bookings,
) -> BookingResponse:
    """Book an offered slot.

    Not yet idempotent: a retried request books twice. `DATA-003` adds the
    idempotency key and the uniqueness constraint that close that gap, which
    needs the database schema from `DATA-001` to exist first.

    Raises:
        NotFoundError: no such tenant.
        BookingNotPermittedError: this tenant does not book through chat.
        MissingRequiredFieldsError: a required field was empty.
        InvalidContactError: the contact is not a valid email or NANP number.
        UnknownServiceError: the service did not resolve against the catalog.
        SlotUnavailableError: the slot is not currently offered.
    """
    record = registry.get(payload.tenant_id)
    resolved = record.policy.catalog.resolve(payload.service)

    command = BookingCommand.parse(
        record.policy,
        customer_name=payload.customer_name,
        contact=payload.contact,
        address=payload.address,
        service=payload.service,
        slot=payload.slot,
        # An unresolvable service has no slot list; passing an empty tuple lets
        # the command report the service problem, which is the more useful of
        # the two errors, rather than a confusing "slot unavailable".
        offered_slots=record.offered_slots(resolved.slug) if resolved else (),
    )

    return BookingResponse.of(bookings.record(command, session_id=payload.session_id))
