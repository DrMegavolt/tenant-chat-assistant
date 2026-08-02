"""Error responses: shape, request correlation, and what must never appear in them."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

from fastapi import Request
from fastapi.testclient import TestClient

from tenantchat.api.app import REQUEST_ID_HEADER, _handle_unexpected_error
from tenantchat.api.problems import problem_response
from tenantchat.core.errors import InvalidVersionTransitionError
from tenantchat.core.lifecycle import VersionState

PayloadBuilder = Callable[..., dict[str, object]]


class TestProblemDocumentShape:
    def test_domain_errors_use_the_problem_media_type(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        response = client.post("/api/book", json=booking_payload(contact="0001234567"))

        assert response.headers["content-type"].startswith("application/problem+json")

    def test_problem_carries_a_stable_machine_readable_code(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        """Clients branch on `code`; it is part of the public contract."""
        body = client.post("/api/book", json=booking_payload(contact="0001234567")).json()

        assert body["code"] == "invalid_contact"
        assert body["type"] == "/problems/invalid_contact"
        assert body["status"] == 422

    def test_problem_detail_is_prose_a_visitor_can_be_shown(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        body = client.post("/api/book", json=booking_payload(contact="0001234567")).json()

        assert body["detail"] == (
            "Provide a valid email address or a complete 10-digit US phone number "
            "including the area code."
        )


class TestOperatorDetailIsNotPublished:
    def test_domain_error_detail_never_reaches_the_response(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        """`DomainError.detail` exists to hold what is unsafe to publish.

        A booking for an unoffered slot builds a detail naming the slot and the
        resolved service slug. The response may carry the current offers, which
        are server-owned, but not that operator string.
        """
        response = client.post("/api/book", json=booking_payload(slot="Sun Jul 7, 3:00 AM"))

        assert "not offered for" not in response.text

    def test_rejected_input_is_not_echoed_by_schema_validation(self, client: TestClient) -> None:
        """Pydantic's error list carries the rejected value; the response must not.

        For this endpoint that value is a phone number or a home address, and
        error responses are among the most-logged objects in the system.
        """
        response = client.post(
            "/api/book",
            json={"tenant_id": "clearview", "contact": ["555-222-1919"]},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "malformed_request"
        assert "555-222-1919" not in response.text

    def test_schema_validation_still_names_the_offending_field(self, client: TestClient) -> None:
        """Withholding the value must not leave the caller unable to fix the request."""
        response = client.post(
            "/api/book",
            json={"tenant_id": "clearview", "contact": ["555-222-1919"]},
        )

        locations = [field["location"] for field in response.json()["invalidFields"]]
        assert any("contact" in location for location in locations)


class TestRequestCorrelation:
    def test_every_response_carries_a_request_id_header(self, client: TestClient) -> None:
        assert client.get("/healthz").headers[REQUEST_ID_HEADER]

    def test_problem_body_repeats_the_header_request_id(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        """A visitor reporting a failure can be matched to the log line."""
        response = client.post("/api/book", json=booking_payload(contact="0001234567"))

        assert response.json()["requestId"] == response.headers[REQUEST_ID_HEADER]

    def test_client_supplied_request_id_is_not_trusted(self, client: TestClient) -> None:
        """An unauthenticated caller could otherwise forge or repeat correlation IDs."""
        response = client.get("/healthz", headers={REQUEST_ID_HEADER: "forged-by-caller"})

        assert response.headers[REQUEST_ID_HEADER] != "forged-by-caller"

    def test_unexpected_error_keeps_its_request_id_and_hides_the_exception(self) -> None:
        """The outer error handler runs after middleware unwinds, so it sets its own header."""

        request = Request(
            {"type": "http", "method": "GET", "path": "/_test/unexpected", "headers": []}
        )
        request.state.request_id = "known-request-id"
        response = asyncio.run(
            _handle_unexpected_error(request, RuntimeError("deliberately-sensitive-value"))
        )
        body = json.loads(response.body)

        assert response.status_code == 500
        assert body["requestId"] == response.headers[REQUEST_ID_HEADER]
        assert b"deliberately-sensitive-value" not in response.body


class TestTypedExtensions:
    def test_rejected_knowledge_transition_publishes_both_states(self) -> None:
        """An operator console has to re-render the document, not parse prose.

        Checked directly rather than through a route because the knowledge
        endpoints arrive with `FEAT-001`, behind the admin authentication in
        `SEC-001`. The mapping is what those routes will depend on.
        """
        error = InvalidVersionTransitionError(
            current=VersionState.DRAFT,
            permitted=(VersionState.APPROVED, VersionState.PUBLISHED),
        )

        response = problem_response(error, request_id="known-request-id")
        body = json.loads(response.body)

        assert response.status_code == 409
        assert body["code"] == "invalid_version_transition"
        assert body["currentState"] == "draft"
        assert body["permittedStates"] == ["approved", "published"]


class TestRequestBounds:
    def test_unknown_field_is_rejected_rather_than_ignored(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        """A silently dropped typo looks like the field was never sent."""
        response = client.post("/api/book", json=booking_payload(customerName="Dana Ruiz"))

        assert response.status_code == 422
        assert response.json()["code"] == "malformed_request"

    def test_oversized_body_is_refused_before_it_is_read(
        self, client: TestClient, booking_payload: PayloadBuilder
    ) -> None:
        response = client.post(
            "/api/book",
            content=json.dumps(booking_payload(address="x" * 4096)),
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 413
        assert response.json()["code"] == "request_too_large"
        assert response.json()["requestId"] == response.headers[REQUEST_ID_HEADER]

    def test_malformed_json_does_not_reach_a_handler(self, client: TestClient) -> None:
        response = client.post(
            "/api/book",
            content="{not json",
            headers={"Content-Type": "application/json"},
        )

        assert response.status_code == 422
        assert response.json()["code"] == "malformed_request"
