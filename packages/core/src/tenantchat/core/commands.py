"""Customer-initiated business actions, parsed into executable commands.

A command is the boundary between "something asked for this" and "the system may
act on it". Constructing one runs every deterministic check the action depends on —
tenant policy, required fields, contact parsing, service resolution — so an
instance is proof that those checks passed. Nothing downstream re-validates.

Parsing lives here rather than at the API edge because the same action arrives by
three routes: a direct API call, a tool call from the model, and an operator
action. Only one of those is HTTP-shaped, and the model's arguments are the least
trustworthy of the three. A rule enforced in a request handler is a rule the other
two callers skip.

Field bounds are enforced here for the same reason. The API edge bounds request
bodies too, and that is not redundant: a graph node calling with model-generated
text never passes through it.

What is deliberately absent: persistence, idempotency keys, and the calendar
reservation. A command says the request is well-formed and permitted, not that
it is unique or durable — the adapter behind :class:`~tenantchat.core.ports.BookingService`
reserves the slot and makes retries replay-safe in one transaction (`DATA-003`).
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

from tenantchat.core.catalog import ServiceDefinition
from tenantchat.core.contact import Contact
from tenantchat.core.errors import (
    BookingNotPermittedError,
    LeadCaptureNotPermittedError,
    MissingRequiredFieldsError,
    SlotUnavailableError,
    UnknownServiceError,
    ValidationError,
)
from tenantchat.core.fields import RequiredField
from tenantchat.core.slots import OfferedSlot
from tenantchat.core.tenant import TenantPolicy

# Generous enough for any real value, tight enough that a runaway model response
# cannot push megabytes into a lead record or a log line.
_MAX_LENGTHS: Final[dict[RequiredField, int]] = {
    RequiredField.CUSTOMER_NAME: 120,
    RequiredField.ADDRESS: 240,
    RequiredField.SERVICE: 120,
    RequiredField.SLOT: 120,
    RequiredField.SUMMARY: 2000,
}


class HandoffReason(StrEnum):
    """Why the assistant stopped and asked for a person.

    A closed set because this value is the natural grouping for "how often does
    the assistant give up, and why" — and a metric label built from model-written
    prose is both unbounded and capable of carrying a customer's words into the
    operational plane, which `ADR-0010` forbids.
    """

    CUSTOMER_REQUEST = "customer_request"
    OUTSIDE_POLICY = "outside_policy"
    TOOL_FAILURE = "tool_failure"
    UNRESOLVED = "unresolved"

    @classmethod
    def parse(cls, raw: str) -> HandoffReason:
        """Map free text onto the taxonomy, defaulting to :attr:`UNRESOLVED`.

        Unrecognized input never refuses the handoff: this action exists because
        something already went wrong, and rejecting it over an unparseable label
        strands the customer with no route to a human.
        """
        try:
            return cls(raw.strip().casefold())
        except ValueError:
            return cls.UNRESOLVED


class LeadUrgency(StrEnum):
    """How soon the customer needs work done.

    A routing hint, never a gate. Unrecognized input becomes :attr:`UNKNOWN`
    rather than rejecting the lead: losing a real customer's callback request
    over an unparseable urgency word is the worse failure.
    """

    EMERGENCY = "emergency"
    TODAY = "today"
    THIS_WEEK = "this_week"
    FLEXIBLE = "flexible"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: str) -> LeadUrgency:
        try:
            return cls(raw.strip().casefold())
        except ValueError:
            return cls.UNKNOWN


def _clean(field: RequiredField, raw: str) -> str:
    """Collapse whitespace and enforce the field's length bound.

    Raises:
        ValidationError: if the value exceeds the bound for this field.
    """
    value = " ".join(raw.split())
    limit = _MAX_LENGTHS[field]
    if len(value) > limit:
        raise ValidationError(detail=f"{field.value} is {len(value)} characters, limit {limit}")
    return value


def _missing(**supplied: str) -> tuple[RequiredField, ...]:
    """Every named field that arrived empty, in declaration order.

    Reported together so a caller can ask for everything outstanding in one turn
    instead of one field per round trip.
    """
    return tuple(RequiredField(name) for name, value in supplied.items() if not value)


@dataclass(frozen=True, slots=True)
class BookingCommand:
    """A booking the tenant permits and the domain can act on.

    ``slot`` is the label the customer picked, checked against what the tenant
    was offering. ``slot_id`` is the provider's stable identity for that slot
    and ``slot_start``/``slot_end`` are its timezone-aware bounds, so the
    reservation later enforces "not in the past" and "one booking per slot"
    against values the caller cannot choose — only the label is caller-supplied.
    """

    tenant_id: str
    customer_name: str
    contact: Contact
    address: str
    service: ServiceDefinition
    slot: str
    slot_id: str
    slot_start: datetime
    slot_end: datetime

    @classmethod
    def parse(
        cls,
        policy: TenantPolicy,
        *,
        customer_name: str,
        contact: str,
        address: str,
        service: str,
        slot: str,
        offered_slots: Collection[OfferedSlot],
    ) -> BookingCommand:
        """Check policy, completeness, contact, service, and slot in that order.

        The order is deliberate: a tenant that does not book through chat is told
        so before the customer is asked to hand over a phone number and a home
        address, so a refused action collects no PII it has no use for.

        ``offered_slots`` is what the tenant is offering for this service, and a
        slot outside it is refused — as is a slot whose window has already
        passed. Passing the structured offers in rather than reading them off
        the policy is what keeps these rules intact when the source swaps from
        static configuration to the live calendar: the rules stay here, only the
        caller changes.

        Raises:
            BookingNotPermittedError: this tenant does not book through chat.
            MissingRequiredFieldsError: one or more required fields were empty.
            InvalidContactError: the contact is not a valid email or NANP number.
            UnknownServiceError: the service did not resolve against the catalog.
            SlotUnavailableError: the slot is not offered, or is already past.
            ValidationError: a field exceeded its length bound.
        """
        if not policy.booking_enabled:
            raise BookingNotPermittedError(detail=f"tenant {policy.tenant_id} has booking disabled")

        cleaned_name = _clean(RequiredField.CUSTOMER_NAME, customer_name)
        cleaned_address = _clean(RequiredField.ADDRESS, address)
        cleaned_service = _clean(RequiredField.SERVICE, service)
        cleaned_slot = _clean(RequiredField.SLOT, slot)

        missing = _missing(
            customer_name=cleaned_name,
            contact=contact.strip(),
            address=cleaned_address,
            service=cleaned_service,
            slot=cleaned_slot,
        )
        if missing:
            raise MissingRequiredFieldsError(missing)

        parsed_contact = Contact.parse(contact)

        resolved = policy.catalog.resolve(cleaned_service)
        if resolved is None:
            raise UnknownServiceError(
                offered=policy.catalog.offered_names(),
                detail=f"{cleaned_service!r} did not resolve for tenant {policy.tenant_id}",
            )

        offers = tuple(slot.label for slot in offered_slots)
        chosen = next((slot for slot in offered_slots if slot.label == cleaned_slot), None)
        if chosen is None:
            raise SlotUnavailableError(
                offered=offers,
                detail=f"{cleaned_slot!r} not offered for {resolved.slug}",
            )
        if chosen.start <= datetime.now(UTC):
            raise SlotUnavailableError(
                offered=offers,
                detail=f"{cleaned_slot!r} has already passed",
            )

        return cls(
            tenant_id=policy.tenant_id,
            customer_name=cleaned_name,
            contact=parsed_contact,
            address=cleaned_address,
            service=resolved,
            slot=chosen.label,
            slot_id=chosen.id,
            slot_start=chosen.start,
            slot_end=chosen.end,
        )


@dataclass(frozen=True, slots=True)
class HandoffCommand:
    """A request for a person to take over the conversation.

    No policy gate: a tenant cannot switch off the escape hatch, because every
    other action in this module can refuse and the customer needs somewhere to
    go when one does.
    """

    tenant_id: str
    reason: HandoffReason
    summary: str

    @classmethod
    def parse(cls, policy: TenantPolicy, *, reason: str, summary: str) -> HandoffCommand:
        """Build a handoff request.

        ``summary`` is required. A handoff with no context makes the staff member
        read the whole transcript before they can say hello, and the caller can
        always supply one — the assistant that raised the handoff has the
        conversation in front of it, and the runtime's own give-up path writes
        the summary itself.

        Raises:
            MissingRequiredFieldsError: no summary was supplied.
            ValidationError: the summary exceeded its length bound.
        """
        cleaned_summary = _clean(RequiredField.SUMMARY, summary)
        missing = _missing(summary=cleaned_summary)
        if missing:
            raise MissingRequiredFieldsError(missing)

        return cls(
            tenant_id=policy.tenant_id,
            reason=HandoffReason.parse(reason),
            summary=cleaned_summary,
        )


@dataclass(frozen=True, slots=True)
class LeadCommand:
    """A follow-up request the tenant permits and the domain can act on.

    ``service`` stays free text, unlike :class:`BookingCommand`. A lead is a
    request for a human to call back, so an unrecognized service is information
    for that human rather than grounds for refusal; ``service_slug`` carries the
    catalog match when there is one, for routing and metrics.
    """

    tenant_id: str
    customer_name: str
    contact: Contact
    service: str
    summary: str
    address_or_zip: str
    urgency: LeadUrgency
    service_slug: str | None

    @classmethod
    def parse(
        cls,
        policy: TenantPolicy,
        *,
        customer_name: str,
        contact: str,
        service: str,
        summary: str,
        address_or_zip: str = "",
        urgency: str = "",
    ) -> LeadCommand:
        """Check policy, completeness, and contact.

        Raises:
            LeadCaptureNotPermittedError: this tenant does not capture leads.
            MissingRequiredFieldsError: one or more required fields were empty.
            InvalidContactError: the contact is not a valid email or NANP number.
            ValidationError: a field exceeded its length bound.
        """
        if not policy.lead_capture_enabled:
            raise LeadCaptureNotPermittedError(
                detail=f"tenant {policy.tenant_id} has lead capture disabled"
            )

        cleaned_name = _clean(RequiredField.CUSTOMER_NAME, customer_name)
        cleaned_service = _clean(RequiredField.SERVICE, service)
        cleaned_summary = _clean(RequiredField.SUMMARY, summary)

        missing = _missing(
            customer_name=cleaned_name,
            contact=contact.strip(),
            service=cleaned_service,
            summary=cleaned_summary,
        )
        if missing:
            raise MissingRequiredFieldsError(missing)

        parsed_contact = Contact.parse(contact)
        resolved = policy.catalog.resolve(cleaned_service)
        return cls(
            tenant_id=policy.tenant_id,
            customer_name=cleaned_name,
            contact=parsed_contact,
            service=cleaned_service,
            summary=cleaned_summary,
            address_or_zip=_clean(RequiredField.ADDRESS, address_or_zip),
            urgency=LeadUrgency.parse(urgency),
            service_slug=resolved.slug if resolved else None,
        )
