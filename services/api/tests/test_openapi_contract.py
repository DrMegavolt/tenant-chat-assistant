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
    ("get", "/readyz"),
    ("get", "/api/tenants"),
    ("get", "/api/tenants/{tenant_id}/availability"),
    # No direct `POST /api/book` or `/api/leads`
    ("post", "/api/chat"),
    ("post", "/api/chat/session"),
    ("get", "/api/chat/session"),
    ("post", "/api/chat/consent"),
    ("post", "/api/chat/confirmation"),
    # FEAT-008: the visitor's rating of one turn record, tenant- and
    # session-qualified before anything is written.
    ("post", "/api/chat/feedback"),
    # RAG-005: tenant-scoped source view behind the visitor's citation.
    ("get", "/api/chat/sources/{source_id}"),
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
    ("get", "/api/admin/jobs"),
    ("get", "/api/admin/jobs/{job_id}"),
    ("post", "/api/admin/jobs/{job_id}/retry"),
    ("post", "/api/admin/jobs/{job_id}/cancel"),
    # PRIV-002: the inference-plane surface. Reads are gated by the dedicated
    # trace-read grant and audited per read; grant and revoke are platform-admin
    # mutations like membership assignment.
    ("get", "/api/admin/traces/{turn_id}"),
    # OBS-004: the attribution surface. Search filters on the content-free
    # projection (manifest hash, cause, outcome); by-trace-id is the correlation
    # lookup, audited like the direct read.
    ("get", "/api/admin/traces"),
    ("get", "/api/admin/traces/by-trace-id/{trace_id}"),
    # FEAT-015: the explorer's replay surface (stored prompt through the current
    # model, no tools) and the gold-evidence overlay, both under the same
    # dedicated role and audit rules as the reads.
    ("post", "/api/admin/traces/{turn_id}/replay"),
    # L7-REPLAY: bounded repeated trials, immutable-index retrieval replay,
    # and template-version-pinned replay — three milestones serving Gate B
    # cases 2-7, each under the same trace-read role and audit rules.
    ("post", "/api/admin/traces/{turn_id}/replay/trials"),
    ("post", "/api/admin/traces/{turn_id}/replay/retrieval"),
    ("post", "/api/admin/traces/{turn_id}/replay/template"),
    ("get", "/api/admin/traces/gold-cases"),
    ("get", "/api/admin/trace-access"),
    ("post", "/api/admin/trace-access"),
    ("delete", "/api/admin/trace-access"),
    # FEAT-008: the review queue. The list is content-free; detail and every
    # mutation are trace-read gated, CSRF protected, and audited.
    ("get", "/api/admin/reviews"),
    ("get", "/api/admin/reviews/{review_id}"),
    ("post", "/api/admin/reviews/{review_id}/take"),
    ("post", "/api/admin/reviews/{review_id}/review"),
    ("post", "/api/admin/reviews/{review_id}/promote"),
    # RAG-002: the knowledge lifecycle surface FEAT-001's console builds on.
    ("post", "/api/admin/knowledge/uploads"),
    ("get", "/api/admin/knowledge/index-findings"),
    ("post", "/api/admin/knowledge/index-integrity-check"),
    # FEAT-001: the knowledge administration workflow — source creation, the
    # tenant knowledge tree, preview, and the audited lifecycle mutations.
    ("post", "/api/admin/knowledge/sources"),
    ("get", "/api/admin/knowledge"),
    ("get", "/api/admin/knowledge/documents/{document_id}"),
    ("get", "/api/admin/knowledge/versions/{version_id}/preview"),
    ("post", "/api/admin/knowledge/versions/{version_id}/approve"),
    ("post", "/api/admin/knowledge/versions/{version_id}/publish"),
    ("post", "/api/admin/knowledge/versions/{version_id}/reindex"),
    ("post", "/api/admin/knowledge/versions/{version_id}/expire"),
    ("delete", "/api/admin/knowledge/documents/{document_id}"),
    ("post", "/api/admin/knowledge/sources/{source_id}/enabled"),
    # RAG-007: the quarantine review queue. Both routes are content-free by
    # construction — the flagged text lives in object storage, never on this
    # surface.
    ("get", "/api/admin/knowledge/quarantine"),
    ("post", "/api/admin/knowledge/quarantine/{version_id}/review"),
    # FEAT-016: the audit read surface and the permissions view. The trail is
    # content-free, its reads are themselves audited, and the permissions view
    # lists the tenant's current roles and trace-read grants as distinct
    # controls, grantors resolved from the trail.
    ("get", "/api/admin/audit"),
    ("get", "/api/admin/permissions"),
    # FEAT-004: the staff handoff queue. The list is the open escalation
    # tickets; each ownership mutation is a conditional store write, so a race
    # to accept has exactly one winner regardless of which console fired.
    ("get", "/api/admin/handoffs"),
    ("post", "/api/admin/handoffs/{handoff_id}/accept"),
    ("post", "/api/admin/handoffs/{handoff_id}/release"),
    ("post", "/api/admin/handoffs/{handoff_id}/resolve"),
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
