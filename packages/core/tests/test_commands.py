"""Booking, lead, and handoff command parsing: policy gates, completeness, field checks."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tenantchat.core.commands import (
    BookingCommand,
    HandoffCommand,
    HandoffReason,
    LeadCommand,
    LeadUrgency,
)
from tenantchat.core.errors import (
    BookingNotPermittedError,
    InvalidContactError,
    LeadCaptureNotPermittedError,
    MissingRequiredFieldsError,
    SlotUnavailableError,
    UnknownServiceError,
    ValidationError,
)
from tenantchat.core.fields import RequiredField
from tenantchat.core.slots import OfferedSlot
from tenantchat.core.tenant import TenantPolicy

# The `build_tenant` fixture, named for use in a signature. Spelled out here
# rather than imported from conftest so no test module depends on another's
# import path.
TenantBuilder = Callable[..., TenantPolicy]


def future_offers() -> tuple[OfferedSlot, ...]:
    """Two bookable slots, always in the future no matter when the suite runs."""
    base = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return (
        OfferedSlot(
            id="slot-1",
            service_slug="hvac",
            start=base + timedelta(hours=14),
            end=base + timedelta(hours=15),
        ),
        OfferedSlot(
            id="slot-2",
            service_slug="hvac",
            start=base + timedelta(days=2, hours=11),
            end=base + timedelta(days=2, hours=12),
        ),
    )


OFFERED_SLOTS = future_offers()
OFFERED_LABELS = tuple(slot.label for slot in OFFERED_SLOTS)


def booking_args(**overrides: Any) -> dict[str, Any]:
    """A complete, valid booking, overridable one argument at a time."""
    return {
        "customer_name": "Dana Ruiz",
        "contact": "555-222-1919",
        "address": "12 Alder Court, Portland, OR 97205",
        "service": "HVAC",
        "slot": OFFERED_LABELS[0],
        "offered_slots": OFFERED_SLOTS,
    } | overrides


def lead_args(**overrides: Any) -> dict[str, Any]:
    """A complete, valid lead, overridable one argument at a time."""
    return {
        "customer_name": "Dana Ruiz",
        "contact": "dana@example.com",
        "service": "HVAC",
        "summary": "Furnace is making a grinding noise.",
    } | overrides


class TestBookingPolicyGate:
    def test_booking_disabled_tenant_refuses_before_reading_customer_details(
        self, build_tenant: TenantBuilder
    ) -> None:
        """A refused action must not collect PII it has no use for.

        Policy is checked ahead of field validation, so a tenant that does not
        book through chat rejects the request without the customer's phone
        number or home address ever being parsed.
        """
        tenant = build_tenant(booking_enabled=False)

        with pytest.raises(BookingNotPermittedError):
            BookingCommand.parse(tenant, **booking_args())

    def test_policy_refusal_message_is_safe_to_show_a_visitor(
        self, build_tenant: TenantBuilder
    ) -> None:
        tenant = build_tenant(booking_enabled=False)

        with pytest.raises(BookingNotPermittedError) as caught:
            BookingCommand.parse(tenant, **booking_args())

        assert str(caught.value) == "This company does not take bookings through chat."
        assert caught.value.code == "booking_not_permitted"


class TestBookingContact:
    def test_filler_phone_number_is_rejected(self, build_tenant: TenantBuilder) -> None:
        """`0001234567` is ten digits but not dialable.

        A digit count alone is not a phone number: area code and exchange both
        begin 2-9. Accepting filler produces a booking nobody can be called
        about, which surfaces as a no-show rather than as an error.
        """
        with pytest.raises(InvalidContactError):
            BookingCommand.parse(build_tenant(), **booking_args(contact="0001234567"))

    def test_accepted_contact_is_stored_canonically(self, build_tenant: TenantBuilder) -> None:
        command = BookingCommand.parse(build_tenant(), **booking_args(contact="(555) 222-1919"))

        assert command.contact.value == "+15552221919"


class TestBookingCompleteness:
    def test_every_empty_field_is_reported_at_once(self, build_tenant: TenantBuilder) -> None:
        """One round trip per missing field would be a miserable conversation."""
        with pytest.raises(MissingRequiredFieldsError) as caught:
            BookingCommand.parse(build_tenant(), **booking_args(customer_name="", address="   "))

        assert set(caught.value.fields) == {RequiredField.CUSTOMER_NAME, RequiredField.ADDRESS}

    def test_whitespace_only_field_counts_as_missing(self, build_tenant: TenantBuilder) -> None:
        with pytest.raises(MissingRequiredFieldsError):
            BookingCommand.parse(build_tenant(), **booking_args(slot="\t\n "))

    def test_oversized_field_is_rejected_rather_than_truncated(
        self, build_tenant: TenantBuilder
    ) -> None:
        """Bounds hold here too: a graph node never passes the API edge's limits."""
        with pytest.raises(ValidationError):
            BookingCommand.parse(build_tenant(), **booking_args(customer_name="x" * 200))


class TestBookingService:
    def test_unresolvable_service_reports_what_the_tenant_offers(
        self, build_tenant: TenantBuilder
    ) -> None:
        with pytest.raises(UnknownServiceError) as caught:
            BookingCommand.parse(build_tenant(), **booking_args(service="roof repair"))

        assert caught.value.offered == ("HVAC", "Window Cleaning")

    def test_resolved_service_carries_the_stable_slug(self, build_tenant: TenantBuilder) -> None:
        """Downstream records key on the slug, so a display-name change is safe."""
        command = BookingCommand.parse(build_tenant(), **booking_args(service="hvac"))

        assert command.service.slug == "hvac"

    def test_the_chosen_slot_keeps_its_provider_identity_and_bounds(
        self, build_tenant: TenantBuilder
    ) -> None:
        """The command names the exact window, not just a label.

        ``slot_id`` is the provider's stable identity and ``slot_start`` is
        timezone-aware, which is what the reservation later keys on to reject a
        past or double-booked slot.
        """
        command = BookingCommand.parse(build_tenant(), **booking_args())

        assert command.slot == OFFERED_LABELS[0]
        assert command.slot_id == "slot-1"
        assert command.slot_start.tzinfo is not None
        assert command.slot_end > command.slot_start

    def test_substring_of_a_service_name_does_not_resolve(
        self, build_tenant: TenantBuilder
    ) -> None:
        """Loose matching books the wrong crew; asking costs one turn."""
        with pytest.raises(UnknownServiceError):
            BookingCommand.parse(build_tenant(), **booking_args(service="v"))


class TestBookingSlot:
    def test_slot_outside_the_current_offers_is_refused(self, build_tenant: TenantBuilder) -> None:
        """Nothing may book a time the tenant never put on the table.

        The caller supplying the slot is often the model, which will happily
        invent a plausible-looking time.
        """
        with pytest.raises(SlotUnavailableError) as caught:
            BookingCommand.parse(build_tenant(), **booking_args(slot="Sun Jul 7, 3:00 AM"))

        assert caught.value.offered == OFFERED_LABELS

    def test_a_slot_whose_window_has_passed_is_refused(self, build_tenant: TenantBuilder) -> None:
        """A past slot is an unavailable slot, whatever its label looks like."""
        past = OfferedSlot(
            id="slot-past",
            service_slug="hvac",
            start=datetime.now(UTC) - timedelta(hours=1),
            end=datetime.now(UTC),
        )
        with pytest.raises(SlotUnavailableError) as caught:
            BookingCommand.parse(
                build_tenant(),
                **booking_args(slot=past.label, offered_slots=(past,)),
            )

        assert caught.value.code == "slot_unavailable"

    def test_empty_offer_list_books_nothing(self, build_tenant: TenantBuilder) -> None:
        with pytest.raises(SlotUnavailableError):
            BookingCommand.parse(build_tenant(), **booking_args(offered_slots=()))


class TestLeadCapture:
    def test_lead_capture_disabled_tenant_refuses(self, build_tenant: TenantBuilder) -> None:
        tenant = build_tenant(lead_capture_enabled=False)

        with pytest.raises(LeadCaptureNotPermittedError):
            LeadCommand.parse(tenant, **lead_args())

    def test_filler_phone_number_is_rejected(self, build_tenant: TenantBuilder) -> None:
        with pytest.raises(InvalidContactError):
            LeadCommand.parse(build_tenant(), **lead_args(contact="0001234567"))

    def test_unrecognized_service_is_captured_rather_than_refused(
        self, build_tenant: TenantBuilder
    ) -> None:
        """A lead is a request for a human callback.

        The human can interpret a service the catalog does not list; refusing
        the lead loses a real customer over a vocabulary mismatch.
        """
        command = LeadCommand.parse(build_tenant(), **lead_args(service="gutter cleaning"))

        assert command.service == "gutter cleaning"
        assert command.service_slug is None

    def test_recognized_service_records_the_slug_for_routing(
        self, build_tenant: TenantBuilder
    ) -> None:
        command = LeadCommand.parse(build_tenant(), **lead_args(service="hvac"))

        assert command.service_slug == "hvac"

    def test_lead_still_requires_a_usable_contact(self, build_tenant: TenantBuilder) -> None:
        with pytest.raises(MissingRequiredFieldsError) as caught:
            LeadCommand.parse(build_tenant(), **lead_args(contact=""))

        assert caught.value.fields == (RequiredField.CONTACT,)


class TestLeadUrgency:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("emergency", LeadUrgency.EMERGENCY),
            ("  This_Week ", LeadUrgency.THIS_WEEK),
            ("", LeadUrgency.UNKNOWN),
            ("whenever you like", LeadUrgency.UNKNOWN),
        ],
    )
    def test_urgency_degrades_to_unknown_instead_of_failing(
        self, raw: str, expected: LeadUrgency, build_tenant: TenantBuilder
    ) -> None:
        """Urgency routes work; it never gates capture."""
        command = LeadCommand.parse(build_tenant(), **lead_args(urgency=raw))

        assert command.urgency is expected


class TestHandoffCommand:
    def test_a_tenant_cannot_switch_off_the_escape_hatch(self, build_tenant: TenantBuilder) -> None:
        """Every other command can refuse, so this one must not.

        A tenant with booking and lead capture both disabled would otherwise
        leave a customer with a conversation that can only say no.
        """
        command = HandoffCommand.parse(
            build_tenant(booking_enabled=False, lead_capture_enabled=False),
            reason="customer_request",
            summary="Customer asked for a person.",
        )

        assert command.reason is HandoffReason.CUSTOMER_REQUEST

    def test_a_handoff_without_context_is_refused(self, build_tenant: TenantBuilder) -> None:
        """A summary-less handoff makes the staff member read the whole transcript."""
        with pytest.raises(MissingRequiredFieldsError) as caught:
            HandoffCommand.parse(build_tenant(), reason="customer_request", summary="   ")

        assert caught.value.fields == (RequiredField.SUMMARY,)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("tool_failure", HandoffReason.TOOL_FAILURE),
            ("  Outside_Policy ", HandoffReason.OUTSIDE_POLICY),
            ("", HandoffReason.UNRESOLVED),
            ("the model gave up", HandoffReason.UNRESOLVED),
        ],
    )
    def test_an_unparseable_reason_degrades_instead_of_refusing(
        self, raw: str, expected: HandoffReason, build_tenant: TenantBuilder
    ) -> None:
        """This action exists because something already failed; it cannot fail too."""
        command = HandoffCommand.parse(build_tenant(), reason=raw, summary="Needs a person.")

        assert command.reason is expected
