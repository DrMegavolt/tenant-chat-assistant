"""Idempotent application services: the only way the graph changes anything.

`ADR-0001` puts authorization, validation, transactions, and idempotency here
rather than in a graph node, because a node is replayed and these operations
must not be. Each service claims the caller's idempotency key, performs the
write once, and returns the original result to every later attempt carrying the
same key.

The fingerprint guards the other direction. A key identifies an *attempt*, so
the same key arriving with materially different content is a caller bug — a
recycled key, a mutated command — and committing the second version under the
first one's identity would silently lose one of them. It is a hash rather than
the content because the fingerprint is stored, and the content includes a
customer's name, address, and phone number.

Nothing in this module imports the agent framework, and nothing in it is
specific to one. A worker or an HTTP handler calls the same services with a
key of its own.
"""

from __future__ import annotations

import hashlib
from typing import Final

from tenantchat.api.store import (
    BookingAttempt,
    BookingRecord,
    BookingStore,
    ConsentStore,
    HandoffStore,
    IdempotencyStore,
    LeadStore,
)
from tenantchat.core.commands import (
    BookingCommand,
    ConsentGatedCommand,
    HandoffCommand,
    LeadCommand,
)
from tenantchat.core.errors import SlotUnavailableError
from tenantchat.core.ports import (
    AvailabilityProvider,
    BookingConfirmation,
    ConsentSource,
    HandoffTicket,
    IdempotencyKey,
    LeadReceipt,
)

BOOKING_SCOPE: Final = "booking"
LEAD_SCOPE: Final = "lead"
HANDOFF_SCOPE: Final = "handoff"

_SEPARATOR: Final = "\x00"


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256(_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


async def _require_consent(
    consent: ConsentSource, command: ConsentGatedCommand, session_id: str
) -> None:
    """Refuse the action unless the session holds the consent the command needs.

    Checked before the idempotency claim, so a refusal leaves no claimed key
    behind and a retry after the visitor consents starts clean. The command
    carries the required purposes from the policy it was parsed against.
    """
    grant = await consent.consent_grant(command.tenant_id, session_id)
    grant.require(*command.require_consent)


class RecordedBookingService:
    """Commits a booking once per idempotency key.

    The claim, the slot reservation, and the booking write all happen inside the
    store's single transaction (`DATA-003`), so there is no window where a
    crashed attempt makes a retry wait. This service supplies the attempt's
    fingerprint and, when the reservation fails because a slot was just taken,
    refreshes the offered alternatives so the caller can re-prompt in one turn.

    Consent is checked before the claim, so a refusal leaves no reservation and
    no claimed key behind (`PRIV-001`).
    """

    def __init__(
        self,
        bookings: BookingStore,
        availability: AvailabilityProvider,
        consent: ConsentStore,
    ) -> None:
        self._bookings = bookings
        self._availability = availability
        self._consent = consent

    async def confirm(
        self,
        command: BookingCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> BookingConfirmation:
        """Book the slot, or return the booking this key already made.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
            ConflictError: the key was used for a materially different booking.
            ConsentRequiredError: the session has not granted every purpose the
                booking requires.
            SlotUnavailableError: the slot is past, reserved, or wrong tenant,
                carrying the current offers.
        """
        await _require_consent(self._consent, command, session_id)
        attempt = BookingAttempt(
            tenant_id=command.tenant_id,
            scope=BOOKING_SCOPE,
            key=idempotency_key,
            request_hash=_fingerprint(
                session_id,
                command.customer_name,
                command.contact.value,
                command.address,
                command.service.slug,
                command.slot_id,
            ),
        )
        try:
            outcome = await self._bookings.confirm(command, session_id=session_id, attempt=attempt)
        except SlotUnavailableError:
            refreshed = tuple(
                slot.label
                for slot in await self._availability.offered_slots(
                    command.tenant_id, command.service.slug
                )
            )
            raise SlotUnavailableError(offered=refreshed) from None
        return _confirmation(outcome.record, replayed=outcome.replayed)

    async def find_replay(
        self, tenant_id: str, idempotency_key: IdempotencyKey
    ) -> BookingConfirmation | None:
        """Return the original confirmation if this key already booked, else ``None``.

        A caller checks this before re-validating the request: a replay names the
        slot the same attempt already took, so that slot is no longer offered and
        would otherwise be refused as if it were a brand-new booking.
        """
        record = await self._bookings.replay(tenant_id, BOOKING_SCOPE, idempotency_key.value)
        if record is None:
            return None
        return _confirmation(record, replayed=True)


def _confirmation(record: BookingRecord, *, replayed: bool) -> BookingConfirmation:
    """Project a stored booking onto the domain confirmation a replay needs."""
    return BookingConfirmation(
        reference=record.booking_id,
        service_slug=record.service_slug,
        service_name=record.service_name,
        slot=record.slot,
        customer_name=record.customer_name,
        contact=record.contact.display,
        address=record.address,
        created_at=record.created_at,
        replayed=replayed,
    )


class RecordedLeadService:
    """Captures a lead once per idempotency key."""

    def __init__(
        self, leads: LeadStore, idempotency: IdempotencyStore, consent: ConsentStore
    ) -> None:
        self._leads = leads
        self._idempotency = idempotency
        self._consent = consent

    async def capture(
        self,
        command: LeadCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> LeadReceipt:
        """Record the lead, or return the one this key already recorded.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
            ConsentRequiredError: the session has not granted every purpose the
                lead requires.
            ConflictError: an attempt with this key is in flight, or the key was
                used for a different lead.
        """
        await _require_consent(self._consent, command, session_id)
        replay = await self._idempotency.begin(
            command.tenant_id,
            scope=LEAD_SCOPE,
            key=idempotency_key,
            fingerprint=_fingerprint(
                session_id,
                command.customer_name,
                command.contact.value,
                command.service,
                command.summary,
            ),
        )
        if replay is not None:
            return LeadReceipt(reference=str(replay["reference"]), replayed=True)

        record = await self._leads.record(command, session_id=session_id)
        await self._idempotency.complete(
            command.tenant_id,
            scope=LEAD_SCOPE,
            key=idempotency_key,
            response={"reference": record.lead_id},
        )
        return LeadReceipt(reference=record.lead_id, replayed=False)


class RecordedHandoffService:
    """Opens a handoff once per idempotency key.

    Duplicate handoffs are cheaper than duplicate bookings, but not free: each
    one lands in a staff queue, and a resumed run that opens a second ticket for
    the same stuck conversation is how that queue stops being trustworthy.
    """

    def __init__(self, handoffs: HandoffStore, idempotency: IdempotencyStore) -> None:
        self._handoffs = handoffs
        self._idempotency = idempotency

    async def request(
        self,
        command: HandoffCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> HandoffTicket:
        """Open the handoff, or return the one this key already opened.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
            ConflictError: an attempt with this key is in flight, or the key was
                used for a different handoff.
        """
        replay = await self._idempotency.begin(
            command.tenant_id,
            scope=HANDOFF_SCOPE,
            key=idempotency_key,
            fingerprint=_fingerprint(session_id, command.reason.value, command.summary),
        )
        if replay is not None:
            return HandoffTicket(
                reference=str(replay["reference"]), reason=command.reason, replayed=True
            )

        record = await self._handoffs.record(command, session_id=session_id)
        await self._idempotency.complete(
            command.tenant_id,
            scope=HANDOFF_SCOPE,
            key=idempotency_key,
            response={"reference": record.handoff_id},
        )
        return HandoffTicket(reference=record.handoff_id, reason=record.reason, replayed=False)
