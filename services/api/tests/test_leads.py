"""POST /api/leads — callback capture."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

PayloadBuilder = Callable[..., dict[str, object]]


class TestAcceptedLead:
    def test_valid_lead_is_captured(self, client: TestClient, lead_payload: PayloadBuilder) -> None:
        response = client.post("/api/leads", json=lead_payload())

        assert response.status_code == 201
        assert response.json()["lead_id"].startswith("LD-")

    def test_unrecognized_service_is_captured_rather_than_refused(
        self, client: TestClient, lead_payload: PayloadBuilder
    ) -> None:
        """A human reads the lead and can interpret a service the catalog lacks."""
        response = client.post("/api/leads", json=lead_payload(service="gutter cleaning"))

        assert response.status_code == 201
        assert response.json()["service"] == "gutter cleaning"

    def test_unparseable_urgency_does_not_lose_the_lead(
        self, client: TestClient, lead_payload: PayloadBuilder
    ) -> None:
        response = client.post("/api/leads", json=lead_payload(urgency="sometime soonish"))

        assert response.status_code == 201
        assert response.json()["urgency"] == "unknown"


class TestRejectedLead:
    def test_filler_phone_number_is_rejected(
        self, client: TestClient, lead_payload: PayloadBuilder
    ) -> None:
        response = client.post("/api/leads", json=lead_payload(contact="0001234567"))

        assert response.status_code == 422
        assert response.json()["code"] == "invalid_contact"

    def test_missing_summary_is_named(
        self, client: TestClient, lead_payload: PayloadBuilder
    ) -> None:
        response = client.post("/api/leads", json=lead_payload(summary=""))

        assert response.status_code == 422
        assert response.json()["missingFields"] == ["summary"]


class TestLeadReadIsNotExposed:
    def test_captured_leads_have_no_unauthenticated_read_endpoint(self, client: TestClient) -> None:
        """Leads hold customer contact details.

        No unauthenticated route may return them. The read side waits for admin
        authentication and tenant-scoped RBAC (`SEC-001`).
        """
        assert client.get("/api/leads").status_code == 405
