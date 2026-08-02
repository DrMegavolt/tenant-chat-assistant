"""POST /api/book — the contract a booking form and a model tool call both hit."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi.testclient import TestClient

PayloadBuilder = Callable[..., dict[str, object]]


class TestAcceptedBooking:
    def test_valid_booking_is_confirmed(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        response = client.post("/api/book", json=booking_payload())

        assert response.status_code == 201
        assert response.json()["booking_id"].startswith("BK-")

    def test_contact_is_confirmed_back_in_canonical_form(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        """The customer should see the number the business will actually dial."""
        response = client.post("/api/book", json=booking_payload(contact="+1 (555) 222-1919"))

        assert response.json()["contact"] == "(555) 222-1919"

    def test_service_is_confirmed_by_display_name_not_the_typed_string(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        response = client.post("/api/book", json=booking_payload(service="a/c"))

        assert response.json()["service"] == "HVAC"

    def test_repeated_bookings_get_distinct_references(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        """Two customers must never be handed the same confirmation number."""
        first = client.post("/api/book", json=booking_payload()).json()
        second = client.post("/api/book", json=booking_payload()).json()

        assert first["booking_id"] != second["booking_id"]


class TestRejectedContact:
    def test_filler_phone_number_is_rejected(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        """`0001234567` reaches the API as ten plausible digits and must not book.

        Counting digits accepts it and confirms an appointment against a number
        nobody can dial, which the business discovers as a no-show.
        """
        response = client.post("/api/book", json=booking_payload(contact="0001234567"))

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_contact"

    @pytest.mark.parametrize(
        "contact",
        ["555-1919", "not a contact", "dana@", "1112223333"],
    )
    def test_unusable_contacts_are_rejected(
        self, client: TestClient, booking_payload: PayloadBuilder, contact: str
    ) -> None:
        response = client.post("/api/book", json=booking_payload(contact=contact))

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_contact"


class TestPolicyAndCompleteness:
    def test_booking_disabled_tenant_is_refused(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        """Apex routes booking to its phone team; the API must agree with the policy."""
        response = client.post("/api/book", json=booking_payload(tenant_id="apex"))

        assert response.status_code == 403
        assert response.json()["code"] == "booking_not_permitted"

    def test_missing_fields_are_named_in_one_response(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        response = client.post("/api/book", json=booking_payload(customer_name="", address="   "))

        assert response.status_code == 422
        assert set(response.json()["missingFields"]) == {"customer_name", "address"}

    def test_unknown_service_reports_what_is_offered(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        response = client.post("/api/book", json=booking_payload(service="roof repair"))

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "unknown_service"
        assert "HVAC" in body["offeredServices"]

    def test_unoffered_slot_is_refused_with_the_current_offers(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        """A slot nobody offered must not book, however plausible it looks."""
        response = client.post("/api/book", json=booking_payload(slot="Sun Jul 7, 3:00 AM"))

        assert response.status_code == 409
        body = response.json()
        assert body["code"] == "slot_unavailable"
        assert "Mon Jul 1, 2:00 PM" in body["offeredSlots"]

    def test_offered_slots_match_the_availability_endpoint(self, client: TestClient) -> None:
        """A slot the widget was shown is a slot the booking endpoint accepts."""
        offered = client.get(
            "/api/tenants/clearview/availability", params={"service": "HVAC"}
        ).json()["slots"]

        rejected = client.post(
            "/api/book",
            json={
                "tenant_id": "clearview",
                "service": "HVAC",
                "slot": offered[0],
                "customer_name": "Dana Ruiz",
                "address": "12 Alder Court, Portland, OR 97205",
                "contact": "555-222-1919",
            },
        )

        assert rejected.status_code == 201

    def test_unknown_tenant_is_not_confirmed_or_denied(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        response = client.post("/api/book", json=booking_payload(tenant_id="does-not-exist"))

        assert response.status_code == 404
        assert "does-not-exist" not in response.text
