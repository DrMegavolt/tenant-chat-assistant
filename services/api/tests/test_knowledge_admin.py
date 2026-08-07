"""The FEAT-001 knowledge administration workflow: lifecycle mutations, the
operator-facing knowledge tree, and the findings-to-turns linkage."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx2 import Response

from tenantchat.api.identity import CSRF_HEADER
from tenantchat.api.index_integrity import InMemoryIndexIntegrityStore
from tenantchat.api.jobs import InMemoryJobStore, JobKind
from tenantchat.api.store import (
    InMemoryAuditStore,
    InMemoryKnowledgeStore,
    InMemoryMembershipStore,
    InMemoryTraceAccessStore,
    InMemoryTurnRecordStore,
)
from tenantchat.core.indexing import (
    GenerationStatus,
    IndexGeneration,
    IndexingFault,
    IndexIntegrityFinding,
)
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


def _turns(client: TestClient) -> InMemoryTurnRecordStore:
    return cast(InMemoryTurnRecordStore, cast(FastAPI, client.app).state.turn_record_store)


def _jobs(client: TestClient) -> InMemoryJobStore:
    return cast(InMemoryJobStore, cast(FastAPI, client.app).state.job_store)


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
    external_key: str = "plan-terms.md",
    title: str = "Plan terms",
    content: bytes = b"# Plan terms\n\n0% APR for 12 months.",
) -> Response:
    return client.post(
        "/api/admin/knowledge/uploads",
        headers=headers,
        data={
            "tenant_id": tenant_id,
            "source_id": str(source_id or _register_source(client, tenant_id=tenant_id)),
            "external_key": external_key,
            "title": title,
        },
        files={"file": ("plan-terms.md", content, "text/markdown")},
    )


def _post_json(
    client: TestClient,
    headers: dict[str, str],
    path: str,
    tenant_id: str,
    body: dict[str, object] | None = None,
) -> Response:
    return client.post(path, headers=headers, json={"tenant_id": tenant_id, **(body or {})})


def _approve_and_publish(client: TestClient, headers: dict[str, str], version_id: str) -> None:
    approved = _post_json(
        client, headers, f"/api/admin/knowledge/versions/{version_id}/approve", "clearview"
    )
    assert approved.status_code == 200, approved.text
    published = _post_json(
        client, headers, f"/api/admin/knowledge/versions/{version_id}/publish", "clearview"
    )
    assert published.status_code == 200, published.text


def test_an_operator_creates_a_source_idempotently_and_is_audited(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
    audit_store: InMemoryAuditStore,
) -> None:
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)

    created = client.post(
        "/api/admin/knowledge/sources",
        headers=headers,
        json={
            "tenant_id": "clearview",
            "domain": "financing",
            "kind": "upload",
            "display_name": "Rate sheets",
        },
    )
    assert created.status_code == 200, created.text
    source_id = created.json()["source_id"]

    again = client.post(
        "/api/admin/knowledge/sources",
        headers=headers,
        json={
            "tenant_id": "clearview",
            "domain": "financing",
            "kind": "upload",
            "display_name": "Rate sheets",
        },
    )
    assert again.status_code == 200
    assert again.json()["source_id"] == source_id

    events = [event for event in audit_store._events if event.action == "knowledge.source_created"]
    assert len(events) == 2  # both calls are audited; the source is not duplicated
    assert all(event.principal_id == "operator-7" for event in events)
    assert all(event.tenant_id == "clearview" for event in events)


def test_source_creation_requires_the_mutation_role_and_csrf(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    _grant(client, membership_store, "clearview")
    payload = {
        "tenant_id": "clearview",
        "domain": "financing",
        "kind": "upload",
        "display_name": "Rate sheets",
    }

    refused = client.post(
        "/api/admin/knowledge/sources", headers=operator_headers(role="viewer"), json=payload
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "forbidden"

    missing_csrf = client.post(
        "/api/admin/knowledge/sources",
        headers=operator_headers(role="tenant_admin"),
        json=payload,
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["code"] == "csrf_validation_failed"


def test_the_knowledge_tree_shows_versions_indexing_status_and_generation(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """The console reads indexing state, chunk counts, errors, and the last
    successful publish in one content-free tree, tenant-scoped."""
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)
    uploaded = _upload(client, headers).json()
    version_id = uuid.UUID(uploaded["version_id"])
    document_id = uuid.UUID(uploaded["document_id"])
    _approve_and_publish(client, headers, str(version_id))
    asyncio.run(_knowledge(client).record_indexed("clearview", version_id, at=datetime.now(UTC)))

    async def complete_generation() -> None:
        from tenantchat.api.ingestion import generation_id_for

        generation = await _findings(client).begin_generation(
            IndexGeneration(
                generation_id=generation_id_for("clearview", version_id),
                tenant_id="clearview",
                document_id=document_id,
                version_id=version_id,
                parser_version="markdown.v1",
                chunker_version="token-window.v2",
                embedding_model="test-embedding",
                status=GenerationStatus.COMPLETE,
                chunk_count=3,
                indexed_chunk_count=3,
                started_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
            )
        )
        await _findings(client).complete_generation(generation)

    asyncio.run(complete_generation())

    listed = client.get(
        "/api/admin/knowledge?tenant_id=clearview", headers=operator_headers(role="viewer")
    )
    assert listed.status_code == 200, listed.text
    body = listed.json()
    assert len(body["sources"]) == 1
    assert body["sources"][0]["display_name"] == "Brochures"
    documents = body["sources"][0]["documents"]
    assert len(documents) == 1
    assert documents[0]["document_id"] == str(document_id)
    version = documents[0]["versions"][0]
    assert version["state"] == "published"
    assert version["indexing_state"] == "indexed"
    assert version["generation_status"] == "complete"
    assert version["published_at"] is not None
    assert version["indexed_at"] is not None
    assert version["chunk_count"] == 3
    assert version["embedding_model"] == "test-embedding"
    assert "0% APR" not in listed.text

    other = client.get(
        "/api/admin/knowledge?tenant_id=apex", headers=operator_headers(role="viewer")
    )
    assert other.status_code == 200
    assert other.json()["sources"] == []


def test_the_knowledge_tree_surfaces_index_failure_not_retrieval_quality(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """A failed ingestion shows its safe error code beside the version, so an
    operator tells ingestion failure from retrieval-quality failure without
    inspecting logs."""
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)
    uploaded = _upload(client, headers).json()
    _approve_and_publish(client, headers, uploaded["version_id"])
    asyncio.run(
        _knowledge(client).record_index_failure(
            "clearview", uuid.UUID(uploaded["version_id"]), error_code="embedding_unavailable"
        )
    )

    listed = client.get(
        "/api/admin/knowledge?tenant_id=clearview", headers=operator_headers(role="viewer")
    )
    version = listed.json()["sources"][0]["documents"][0]["versions"][0]
    assert version["indexing_state"] == "failed"
    assert version["index_error_code"] == "embedding_unavailable"


def test_a_draft_version_can_be_previewed_bounded_and_tenant_scoped(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """Preview parses the staged bytes with the production parser and returns a
    bounded window, never the raw file."""
    _grant(client, membership_store, "clearview")
    _grant(client, membership_store, "apex")
    headers = _mutation_headers(client, operator_headers)
    uploaded = _upload(
        client,
        headers,
        content=b"# Rates\n\n## APR\n\n0% APR.\n\n## Fees\n\nNone.",
    ).json()

    preview = client.get(
        f"/api/admin/knowledge/versions/{uploaded['version_id']}/preview?tenant_id=clearview",
        headers=headers,
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["parser_version"].startswith("markdown")
    assert body["media_type"] == "text/markdown"
    assert body["chunk_count"] >= 1
    assert body["blocks"][0]["location"] != ""
    assert all(len(block["text"]) <= 400 for block in body["blocks"])
    assert any("0% APR" in block["text"] for block in body["blocks"])

    other = client.get(
        f"/api/admin/knowledge/versions/{uploaded['version_id']}/preview?tenant_id=apex",
        headers=headers,
    )
    assert other.status_code == 404

    viewer = client.get(
        f"/api/admin/knowledge/versions/{uploaded['version_id']}/preview?tenant_id=clearview",
        headers=operator_headers(role="viewer"),
    )
    assert viewer.status_code == 403


def test_approval_publish_expire_reindex_are_audited_and_gated(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
    audit_store: InMemoryAuditStore,
) -> None:
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)
    version_id = _upload(client, headers).json()["version_id"]
    before = len(audit_store._events)

    approved = _post_json(
        client, headers, f"/api/admin/knowledge/versions/{version_id}/approve", "clearview"
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["version"]["state"] == "approved"

    published = _post_json(
        client, headers, f"/api/admin/knowledge/versions/{version_id}/publish", "clearview"
    )
    assert published.status_code == 200, published.text
    assert published.json()["version"]["state"] == "published"
    assert published.json()["job"]["kind"] == "ingestion"

    jobs = asyncio.run(_jobs(client).for_tenant("clearview"))
    assert len(jobs) == 1
    assert jobs[0].kind is JobKind.INGESTION

    expired = _post_json(
        client, headers, f"/api/admin/knowledge/versions/{version_id}/expire", "clearview"
    )
    assert expired.status_code == 200, expired.text
    assert expired.json()["version"]["expires_at"] is not None

    reindexed = _post_json(
        client, headers, f"/api/admin/knowledge/versions/{version_id}/reindex", "clearview"
    )
    assert reindexed.status_code == 200, reindexed.text
    assert reindexed.json()["job"]["job_id"] == str(jobs[0].job_id)

    actions = {event.action for event in audit_store._events[before:]}
    assert actions == {
        "knowledge.version_approved",
        "knowledge.version_published",
        "knowledge.version_expired",
        "knowledge.version_reindexed",
    }

    viewer = _post_json(
        client,
        operator_headers(role="viewer"),
        f"/api/admin/knowledge/versions/{version_id}/expire",
        "clearview",
    )
    assert viewer.status_code == 403


def test_publishing_a_superseded_version_is_a_rollback(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """Rollback is the publish path: publishing the superseded version demotes
    the current one in the same transaction, never leaving two current."""
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)
    first = _upload(client, headers, external_key="terms.md", title="Terms v1").json()
    _approve_and_publish(client, headers, first["version_id"])
    second = _upload(
        client,
        headers,
        external_key="terms.md",
        title="Terms v2",
        content=b"# Plan terms\n\nUpdated 2% APR for 12 months.",
    ).json()
    assert second["version_id"] != first["version_id"]
    _approve_and_publish(client, headers, second["version_id"])

    rolled = _post_json(
        client, headers, f"/api/admin/knowledge/versions/{first['version_id']}/publish", "clearview"
    )
    assert rolled.status_code == 200, rolled.text
    assert rolled.json()["version"]["state"] == "published"

    listed = client.get(
        "/api/admin/knowledge?tenant_id=clearview", headers=operator_headers(role="viewer")
    ).json()
    versions = listed["sources"][0]["documents"][0]["versions"]
    states = {version["revision"]: version["state"] for version in versions}
    assert states == {1: "published", 2: "superseded"}


def test_publishing_an_unapproved_draft_is_refused(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """Draft content never becomes answerable: publication requires approval."""
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)
    version_id = _upload(client, headers).json()["version_id"]

    response = _post_json(
        client, headers, f"/api/admin/knowledge/versions/{version_id}/publish", "clearview"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_version_transition"


def test_reindexing_a_draft_is_refused(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)
    version_id = _upload(client, headers).json()["version_id"]

    response = _post_json(
        client, headers, f"/api/admin/knowledge/versions/{version_id}/reindex", "clearview"
    )
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_version_transition"


def test_deleting_a_document_is_a_tenant_scoped_idempotent_tombstone(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
    audit_store: InMemoryAuditStore,
) -> None:
    _grant(client, membership_store, "clearview")
    _grant(client, membership_store, "apex")
    headers = _mutation_headers(client, operator_headers)
    uploaded = _upload(client, headers).json()
    document_id = uploaded["document_id"]
    _approve_and_publish(client, headers, uploaded["version_id"])

    deleted = client.request(
        "DELETE",
        f"/api/admin/knowledge/documents/{document_id}",
        headers=headers,
        json={"tenant_id": "clearview"},
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["document"]["deleted"] is True

    again = client.request(
        "DELETE",
        f"/api/admin/knowledge/documents/{document_id}",
        headers=headers,
        json={"tenant_id": "clearview"},
    )
    assert again.status_code == 200
    assert again.json()["document"]["deleted"] is True

    cross = client.request(
        "DELETE",
        f"/api/admin/knowledge/documents/{document_id}",
        headers=headers,
        json={"tenant_id": "apex"},
    )
    assert cross.status_code == 404

    events = [
        event for event in audit_store._events if event.action == "knowledge.document_deleted"
    ]
    assert len(events) == 2  # both calls are audited; the tombstone is idempotent
    assert all(event.principal_id == "operator-7" for event in events)


def test_disabling_a_source_withdraws_its_documents_and_is_audited(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
    audit_store: InMemoryAuditStore,
) -> None:
    _grant(client, membership_store, "clearview")
    headers = _mutation_headers(client, operator_headers)
    source_id = _register_source(client)

    disabled = _post_json(
        client,
        headers,
        f"/api/admin/knowledge/sources/{source_id}/enabled",
        "clearview",
        {"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False

    enabled = _post_json(
        client,
        headers,
        f"/api/admin/knowledge/sources/{source_id}/enabled",
        "clearview",
        {"enabled": True},
    )
    assert enabled.json()["enabled"] is True

    events = [
        event for event in audit_store._events if event.action.startswith("knowledge.source_")
    ]
    actions = {event.action for event in events}
    assert {"knowledge.source_disabled", "knowledge.source_enabled"} <= actions


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
            title="Seeded terms",
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


def test_findings_link_each_fault_to_its_affected_source_version(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """A finding names the source version it affects, with its display metadata
    resolved for the console — and stays tenant-scoped and content-free."""
    _grant(client, membership_store, "clearview")
    version_id = _seed_finding(client)

    listed = client.get(
        "/api/admin/knowledge/index-findings?tenant_id=clearview",
        headers=operator_headers(role="viewer"),
    )
    assert listed.status_code == 200, listed.text
    finding = listed.json()["findings"][0]
    assert finding["code"] == "index_missing_generation"
    assert finding["version_id"] == str(version_id)
    assert finding["source_name"] == "Brochures"
    assert finding["document_title"] == "Seeded terms"
    assert finding["revision"] == 1
    assert "text" not in listed.text

    other = client.get(
        "/api/admin/knowledge/index-findings?tenant_id=apex",
        headers=operator_headers(role="viewer"),
    )
    assert other.json()["findings"] == []


def _seed_turns_citing(client: TestClient, generation_id: uuid.UUID) -> None:
    def record(source_generation_ids: tuple[uuid.UUID, ...]) -> None:
        asyncio.run(
            _turns(client).record(
                "clearview",
                uuid.uuid4(),
                content={"schema_version": "1", "retrieval": {"sufficient": True}},
                outcome="answered",
                source_generation_ids=source_generation_ids,
            )
        )

    record((generation_id,))
    record((generation_id, uuid.uuid4()))
    record(())


def test_related_turns_for_a_generation_require_the_trace_read_grant(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    """The findings-to-turns link is a trace-search filter: the dedicated
    trace-read grant decides who may follow it, and every search is audited."""
    _grant(client, membership_store, "clearview")
    generation_id = uuid.uuid4()
    _seed_turns_citing(client, generation_id)

    forbidden = client.get(
        f"/api/admin/traces?tenant_id=clearview&reason=quality_review&generation_id={generation_id}",
        headers=operator_headers(role="viewer"),
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "forbidden"

    grants = cast(InMemoryTraceAccessStore, cast(FastAPI, client.app).state.trace_access_store)
    asyncio.run(grants.grant("clearview", "operator-7", granted_by="platform-admin-1"))

    granted = client.get(
        f"/api/admin/traces?tenant_id=clearview&reason=quality_review&generation_id={generation_id}",
        headers=operator_headers(role="viewer"),
    )
    assert granted.status_code == 200, granted.text
    records = granted.json()["records"]
    assert len(records) == 2
    assert all(str(generation_id) in record["source_generation_ids"] for record in records)
