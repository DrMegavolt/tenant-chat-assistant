"""Booking submission.

The handler is deliberately thin. It resolves the tenant, hands the raw strings
to ``BookingCommand.parse``, and asks the idempotent :class:`BookingService` to
reserve and confirm the slot. Every rule that decides whether the booking is
allowed lives in the command, so the model-driven tool call added in `ARCH-001`
gets the identical checks without this module.

The `Idempotency-Key` header is required: `DATA-003` makes a direct API booking
replay-safe the same way a graph replay is, so a form double-submit cannot book
twice. A repeated key returns the original confirmation (with the same
reference) rather than a second booking.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Response, status
from fastapi.responses import JSONResponse

from tenantchat.api.dependencies import Availability, BookingActions, Registry
from tenantchat.api.schemas import BookingRequest, BookingResponse
from tenantchat.core.commands import BookingCommand
from tenantchat.core.ports import IdempotencyKey

router = APIRouter(tags=["bookings"])


@router.post("/api/book")
async def create_booking(
    payload: BookingRequest,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    registry: Registry,
    availability: Availability,
    book: BookingActions,
) -> Response:
    """Reserve and confirm an offered slot, exactly once per idempotency key.

    Returns ``201`` for the first attempt and ``200`` for a replay of the same
    key, whose body names the original booking.

    Storing the customer's contact is gated on the session having granted the
    purposes the booking requires (`PRIV-001`).

    Raises:
        NotFoundError: no such tenant.
        BookingNotPermittedError: this tenant does not book through chat.
        MissingRequiredFieldsError: a required field was empty.
        InvalidContactError: the contact is not a valid email or NANP number.
        UnknownServiceError: the service did not resolve against the catalog.
        SlotUnavailableError: the slot is not offered, is past, or was just
            reserved by another customer; carries the current offers.
        ConsentRequiredError: the session has not granted every purpose the
            booking requires. Enforced by the booking service, so the model-
            driven tool call is gated identically.
    """
    key = IdempotencyKey.parse(idempotency_key)
    replay = await book.find_replay(payload.tenant_id, key)
    if replay is not None:
        body = BookingResponse.build(replay).model_dump(mode="json")
        return JSONResponse(status_code=status.HTTP_200_OK, content=body)

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
        offered_slots=(
            await availability.offered_slots(payload.tenant_id, resolved.slug)
            if resolved is not None
            else ()
        ),
    )
    confirmation = await book.confirm(
        command,
        session_id=payload.session_id,
        idempotency_key=key,
    )
    body = BookingResponse.build(confirmation).model_dump(mode="json")
    return JSONResponse(
        status_code=status.HTTP_200_OK if confirmation.replayed else status.HTTP_201_CREATED,
        content=body,
    )
