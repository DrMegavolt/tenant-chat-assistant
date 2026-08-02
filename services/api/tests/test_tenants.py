"""GET /api/tenants and availability — the unauthenticated widget surface."""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestPublicTenantSurface:
    def test_every_configured_tenant_is_listed(self, client: TestClient) -> None:
        tenants = client.get("/api/tenants").json()["tenants"]

        assert set(tenants) == {"apex", "clearview"}

    def test_approved_prices_are_not_served_to_the_widget(self, client: TestClient) -> None:
        """Pricing is policy-gated; publishing the list would route around it."""
        body = client.get("/api/tenants").text

        assert "$150/hour" not in body
        assert "$120 diagnostic" not in body

    def test_served_zip_list_is_not_served_to_the_widget(self, client: TestClient) -> None:
        """Publishing coverage lets a competitor map the service area."""
        body = client.get("/api/tenants").text

        assert "97035" not in body
        assert "98101," not in body

    def test_booking_policy_is_reported_per_tenant(self, client: TestClient) -> None:
        tenants = client.get("/api/tenants").json()["tenants"]

        assert tenants["clearview"]["booking_enabled"] is True
        assert tenants["apex"]["booking_enabled"] is False


class TestAvailability:
    def test_slots_are_listed_for_an_offered_service(self, client: TestClient) -> None:
        response = client.get("/api/tenants/clearview/availability", params={"service": "HVAC"})

        assert response.status_code == 200
        assert response.json()["slots"]

    def test_service_alias_resolves(self, client: TestClient) -> None:
        response = client.get("/api/tenants/clearview/availability", params={"service": "a/c"})

        assert response.json()["service"] == "HVAC"

    def test_unknown_service_reports_what_is_offered(self, client: TestClient) -> None:
        response = client.get(
            "/api/tenants/clearview/availability", params={"service": "roof repair"}
        )

        assert response.status_code == 422
        assert response.json()["code"] == "unknown_service"

    def test_booking_disabled_tenant_offers_no_slots(self, client: TestClient) -> None:
        """Apex books nothing, so there is nothing for a caller to select."""
        response = client.get("/api/tenants/apex/availability", params={"service": "HVAC"})

        assert response.json()["slots"] == []

    def test_unknown_tenant_is_not_found(self, client: TestClient) -> None:
        response = client.get("/api/tenants/nope/availability", params={"service": "HVAC"})

        assert response.status_code == 404
