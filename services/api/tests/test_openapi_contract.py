"""The published contract, pinned.

The schema is generated, which is exactly why it needs a test: nobody reviews a
generated document, so a route added without a second thought appears in it
without one either. The inventory below is the list a reviewer agreed to, and
changing the API means changing it deliberately.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient
from pydantic import BaseModel

from tenantchat.api import schemas
from tenantchat.api.app import create_app
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    InMemoryAuditStore,
    InMemoryBookingStore,
    InMemoryConsentStore,
    InMemoryConversationStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryLeadStore,
    InMemoryMembershipStore,
    InMemoryPrivacyStore,
)

PUBLISHED_OPERATIONS = {
    ("get", "/healthz"),
    ("get", "/api/tenants"),
    ("get", "/api/tenants/{tenant_id}/availability"),
    ("post", "/api/book"),
    ("post", "/api/leads"),
    ("post", "/api/chat"),
    ("post", "/api/chat/session"),
    ("get", "/api/chat/session"),
    ("post", "/api/chat/consent"),
    ("post", "/api/chat/confirmation"),
    ("get", "/api/admin/csrf-token"),
    ("get", "/api/admin/tenants"),
    ("get", "/api/admin/chats"),
    ("get", "/api/admin/chats/{session_id}"),
    ("post", "/api/admin/chats/{session_id}/messages"),
    ("get", "/api/admin/leads"),
    ("get", "/api/admin/bookings"),
    ("post", "/api/admin/memberships"),
    ("delete", "/api/admin/memberships"),
    ("post", "/api/admin/privacy/export"),
    ("post", "/api/admin/privacy/deletion-requests"),
    ("get", "/api/admin/privacy/deletion-requests"),
}


def request_models() -> list[type[BaseModel]]:
    return [
        attribute
        for name, attribute in vars(schemas).items()
        if name.endswith("Request") and isinstance(attribute, type)
    ]


def test_the_published_surface_is_the_reviewed_one(client: TestClient) -> None:
    document = client.get("/openapi.json")

    assert document.status_code == 200
    operations = {
        (method, path) for path, methods in document.json()["paths"].items() for method in methods
    }
    assert operations == PUBLISHED_OPERATIONS


def test_the_schema_is_withheld_when_docs_are_disabled(settings: Settings) -> None:
    """The schema names every field and error code, which is a map worth not handing out."""
    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    app = create_app(
        replace(settings, docs_enabled=False),
        booking_store=InMemoryBookingStore(),
        lead_store=InMemoryLeadStore(),
        conversation_store=conversations,
        handoff_store=InMemoryHandoffStore(),
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=InMemoryMembershipStore(),
        consent_store=consent,
        privacy_store=InMemoryPrivacyStore(
            conversations,
            InMemoryBookingStore(),
            InMemoryLeadStore(),
            InMemoryHandoffStore(),
            consent,
        ),
        audit_store=InMemoryAuditStore(),
    )

    with TestClient(app) as closed:
        assert closed.get("/openapi.json").status_code == 404
        assert closed.get("/docs").status_code == 404


def test_every_request_model_rejects_unknown_fields() -> None:
    """A typo in a field name must fail loudly rather than read as absent.

    Checked over the whole module rather than model by model, because the
    failure this prevents arrives with the *next* request model somebody writes.
    """
    assert request_models(), "no request models found; the check would pass vacuously"

    permissive = [
        model.__name__ for model in request_models() if model.model_config.get("extra") != "forbid"
    ]

    assert not permissive, f"request models accepting unknown fields: {permissive}"
