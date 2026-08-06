"""The knowledge lifecycle HTTP surface: uploads and index-integrity findings."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from tenantchat.api.identity import CSRF_HEADER
from tenantchat.api.index_integrity import InMemoryIndexIntegrityStore
from tenantchat.api.store import InMemoryKnowledgeStore, InMemoryMembershipStore
from tenantchat.core.indexing import IndexingFault, IndexIntegrityFinding
from tenantchat.core.knowledge import ContentChecksum, KnowledgeDomain, SourceKind

FINANCING = KnowledgeDomain.parse("financing")


def _csrf(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def _grant(client: TestClient, membership_store: InMemoryMembershipStore, tenant: str) -> None:
    asyncio.run(
        membership_store.assign(tenant_id=tenant, subject="operator-7", role="tenant_admin")
    )


def _knowledge(client: TestClient) -> InMemoryKnowledgeStore:
    return cast(InMemoryKnowledgeStore, cast(FastAPI, client.app).state.knowledge_store)


def _findings(client: TestClient) -> InMemoryIndexIntegrityStore:
    return cast(InMemoryIndexIntegrityStore, cast(FastAPI, client.app).state.generation_findings)


def _register_source(client: TestClient, *, tenant_id: str = "clearview") -> uuid.UUID:
    async def arrange() -> uuid.UUID:
        source = await _knowledge(client).register_source(
            tenant_id,
            domain=FINANCING,
            kind=SourceKind.UPLOAD,
            display_name="Brochures",
        )
        return source.source_id

    return asyncio.run(arrange())


def _mutation_headers(
    client: TestClient, operator_headers: Callable[..., dict[str, str]]
) -> dict[str, str]:
    base = operator_headers(role="tenant_admin")
    return base | {CSRF_HEADER: _csrf(client, base)}


def _upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    tenant_id: str = "clearview",
    source_id: uuid.UUID | None = None,
    filename: str = "plan-terms.md",
    media_type: str = "text/markdown",
    content: bytes = b"# Plan terms\n\n0% APR for 12 months.",
) -> Response:
    return client.post(
        "/api/admin/knowledge/uploads",
        headers=headers,
        data={
            "tenant_id": tenant_id,
            "source_id": str(source_id or _register_source(client, tenant_id=tenant_id)),
            "external_key": "plan-terms.md",
            "title": "Plan terms",
        },
        files={"file": (filename, content, media_type)},
    )


def test_an_operator_uploads_a_valid_document_into_isolated_storage(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)

    response = _upload(client, headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "draft"
    assert body["indexing_state"] == "pending"
    assert body["checksum"] != ""
    assert "content" not in body

    document = asyncio.run(
        _knowledge(client).load_document("clearview", uuid.UUID(body["document_id"]))
    )
    version = document.version(uuid.UUID(body["version_id"]))
    assert version.storage_key.startswith("tenants/clearview/")
    assert version.media_type == "text/markdown"


def test_duplicate_uploads_of_identical_bytes_stage_one_draft(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)

    first = _upload(client, headers).json()
    second = _upload(client, headers).json()

    assert first["version_id"] == second["version_id"]
    assert first["revision"] == second["revision"] == 1


def test_an_unsupported_media_type_is_refused(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)

    response = _upload(
        client,
        headers,
        media_type="application/x-msdownload",
        content=b"MZ\x90\x00",
    )

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    ("filename", "media_type", "content"),
    [
        ("terms.pdf", "application/pdf", b"%PDF-1.4\n% mock"),
        (
            "terms.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            b"PK\x03\x04 mock",
        ),
    ],
)
def test_every_parser_media_type_is_an_acceptable_upload(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
    filename: str,
    media_type: str,
    content: bytes,
) -> None:
    """RAG-003: the upload route accepts exactly what the adapters can parse."""
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)

    response = _upload(client, headers, filename=filename, media_type=media_type, content=content)

    assert response.status_code == 200, response.text
    assert response.json()["indexing_state"] == "pending"


def test_index_findings_are_tenant_scoped_and_content_free(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    _grant(client, membership_store, "clearview")
    version_id = _seed_finding(client)

    listed = client.get(
        "/api/admin/knowledge/index-findings?tenant_id=clearview",
        headers=operator_headers(role="viewer"),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["findings"][0]["code"] == "index_missing_generation"
    assert listed.json()["findings"][0]["version_id"] == str(version_id)
    assert "text" not in listed.text

    other = client.get(
        "/api/admin/knowledge/index-findings?tenant_id=apex",
        headers=operator_headers(role="viewer"),
    )
    assert other.status_code == 200
    assert other.json()["findings"] == []


def test_the_integrity_check_detects_persists_and_lists_findings(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)
    version_id = _upload(client, headers).json()["version_id"]

    # Publish but never index: the detector must report the lag.
    async def publish() -> None:
        await _knowledge(client).approve(
            "clearview", uuid.UUID(version_id), approved_by="ops@example", at=datetime.now(UTC)
        )
        await _knowledge(client).publish(
            "clearview",
            uuid.UUID(version_id),
            at=datetime.now(UTC) - timedelta(hours=25),
        )

    asyncio.run(publish())

    checked = client.post(
        "/api/admin/knowledge/index-integrity-check?tenant_id=clearview", headers=headers
    )
    assert checked.status_code == 200, checked.text
    codes = {item["code"] for item in checked.json()["findings"]}
    assert codes == {"index_lag"}


def test_the_integrity_check_requires_the_mutation_role_and_csrf(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    _grant(client, membership_store, "clearview")

    refused = client.post(
        "/api/admin/knowledge/index-integrity-check?tenant_id=clearview",
        headers=operator_headers(role="viewer"),
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "forbidden"

    missing_csrf = client.post(
        "/api/admin/knowledge/index-integrity-check?tenant_id=clearview",
        headers=operator_headers(role="tenant_admin"),
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["code"] == "csrf_validation_failed"


def test_findings_route_is_unavailable_without_a_composed_search_index(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """The default test composition has an in-memory index; a deployment without
    one must fail closed, which the composition layer owns. This pins the route's
    503 contract through the dependency's None path."""
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)

    app = cast(FastAPI, client.app)
    saved = app.state.search_index
    app.state.search_index = None
    try:
        response = client.post(
            "/api/admin/knowledge/index-integrity-check?tenant_id=clearview", headers=headers
        )
        assert response.status_code == 503
        assert response.json()["code"] == "search_index_unavailable"
    finally:
        app.state.search_index = saved


def _seed_finding(client: TestClient) -> uuid.UUID:
    async def arrange() -> uuid.UUID:
        knowledge = _knowledge(client)
        source = await knowledge.register_source(
            "clearview", domain=FINANCING, kind=SourceKind.UPLOAD, display_name="Brochures"
        )
        checksum = ContentChecksum.of(b"seeded")
        document = await knowledge.stage_version(
            "clearview",
            source_id=source.source_id,
            external_key="seeded.md",
            title="Seeded",
            checksum=checksum,
            byte_size=6,
            media_type="text/markdown",
            storage_key="tenants/clearview/seeded",
        )
        staged = document.version_with_checksum(checksum)
        assert staged is not None
        await knowledge.approve(
            "clearview", staged.version_id, approved_by="ops@example", at=datetime.now(UTC)
        )
        await knowledge.publish("clearview", staged.version_id, at=datetime.now(UTC))
        await knowledge.record_indexed("clearview", staged.version_id, at=datetime.now(UTC))
        finding = IndexIntegrityFinding(
            code=IndexingFault.MISSING_GENERATION,
            tenant_id="clearview",
            document_id=staged.document_id,
            version_id=staged.version_id,
            generation_id=None,
            detected_at=datetime.now(UTC),
            detail={},
        )
        await _findings(client).sync_findings("clearview", [finding])
        return staged.version_id

    return asyncio.run(arrange())
