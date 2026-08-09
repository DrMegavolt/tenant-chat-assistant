"""Regression: the ingestion surface cannot read files or cross tenants.

The prototype ingestion service (deleted in the `DEP-001` cutover) took a
caller-supplied filesystem path and read it, which made every container file a
document and every directory a source. `RAG-002` removed the path entirely:
uploads are validated bytes stored under server-derived keys, and every
identifier is tenant-qualified. These tests pin the exploit shapes that are
gone.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from tenantchat.api.identity import CSRF_HEADER
from tenantchat.api.store import InMemoryKnowledgeStore, InMemoryMembershipStore
from tenantchat.core.knowledge import KnowledgeDomain, SourceKind
from tenantchat.core.lifecycle import VersionState

FINANCING = KnowledgeDomain.parse("financing")


def _csrf(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def _grant(client: TestClient, membership_store: InMemoryMembershipStore) -> None:
    asyncio.run(
        membership_store.assign(tenant_id="clearview", subject="operator-7", role="tenant_admin")
    )


def _register_source(client: TestClient, *, tenant_id: str = "clearview") -> uuid.UUID:
    store = cast(InMemoryKnowledgeStore, cast(FastAPI, client.app).state.knowledge_store)

    async def arrange() -> uuid.UUID:
        source = await store.register_source(
            tenant_id,
            domain=FINANCING,
            kind=SourceKind.UPLOAD,
            display_name="Brochures",
        )
        return source.source_id

    return asyncio.run(arrange())


def _draft_versions(client: TestClient, tenant_id: str) -> tuple[object, ...]:
    store = cast(InMemoryKnowledgeStore, cast(FastAPI, client.app).state.knowledge_store)
    return asyncio.run(store.versions_in_state(tenant_id, VersionState.DRAFT))


def _upload(
    client: TestClient,
    headers: dict[str, str],
    *,
    tenant_id: str,
    source_id: uuid.UUID,
    filename: str = "brochure.md",
    content: bytes = b"# Terms\n\nSome terms.",
) -> Response:
    return client.post(
        "/api/admin/knowledge/uploads",
        headers=headers,
        data={
            "tenant_id": tenant_id,
            "source_id": str(source_id),
            "external_key": "brochure.md",
            "title": "Brochure",
        },
        files={"file": (filename, content, "text/markdown")},
    )


@pytest.mark.security
def test_a_path_shaped_filename_cannot_read_container_files(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """The exploit the prototype had: a path in the request reads that file."""
    _grant(client, membership_store)
    source_id = _register_source(client)
    base = operator_headers(role="tenant_admin")
    headers = base | {CSRF_HEADER: _csrf(client, base)}

    for hostile in (
        "../../etc/passwd",
        "../../../../etc/passwd",
        "/etc/passwd",
        "..%2fetc%2fpasswd",
    ):
        response = _upload(
            client, headers, tenant_id="clearview", source_id=source_id, filename=hostile
        )
        assert response.status_code == 422, response.text
        assert "passwd" not in response.text.lower()

    assert _draft_versions(client, "clearview") == ()


@pytest.mark.security
def test_a_path_shaped_filename_is_rejected_by_shape_not_by_value(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """The refusal must not echo the hostile value back: it is a probe."""
    _grant(client, membership_store)
    base = operator_headers(role="tenant_admin")
    headers = base | {CSRF_HEADER: _csrf(client, base)}

    response = _upload(
        client,
        headers,
        tenant_id="clearview",
        source_id=uuid.uuid4(),
        filename="../../etc/passwd",
    )
    assert response.status_code == 422
    assert "etc/passwd" not in response.text
    assert "../" not in response.text


@pytest.mark.security
def test_a_known_source_uuid_from_another_tenant_uploads_nothing(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """A leaked source UUID is not authorization (SEC-001's not-found contract)."""
    _grant(client, membership_store)
    source_id = _register_source(client, tenant_id="apex")
    base = operator_headers(role="tenant_admin")
    headers = base | {CSRF_HEADER: _csrf(client, base)}

    response = _upload(client, headers, tenant_id="clearview", source_id=source_id)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert _draft_versions(client, "clearview") == ()


@pytest.mark.security
def test_a_caller_cannot_stage_content_into_another_tenants_storage(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """The tenant binding is the membership, and it cannot be widened by body."""
    _grant(client, membership_store)
    source_id = _register_source(client, tenant_id="clearview")
    base = operator_headers(role="tenant_admin")
    headers = base | {CSRF_HEADER: _csrf(client, base)}

    response = _upload(client, headers, tenant_id="apex", source_id=source_id)

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


@pytest.mark.security
def test_the_upload_surface_requires_an_authenticated_operator(client: TestClient) -> None:
    response = _upload(client, {}, tenant_id="clearview", source_id=uuid.uuid4())
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
