"""The knowledge lifecycle surface `FEAT-001` builds its console on.

Two capabilities land here in `RAG-002`:

- **Uploads.** A validated document becomes a staged draft in the tenant's
  isolated object storage and the system of record. The caller never names a
  filesystem path; the storage key is derived server-side from the tenant, the
  authorized source, and the content checksum — the removal of the prototype's
  caller-supplied path.
- **Index-integrity findings.** The bounded, content-free faults the detector
  produces, tenant-qualified, for `FEAT-001`'s console and `OBS-004`'s
  attribution to read.

`RAG-007` adds the third: **quarantine review.** The ingestion worker's
content-safety scan files suspicious content as a quarantined version; the
review queue lists those versions and the review action clears or keeps the
quarantine. Both surfaces are content-free by construction — the text that
triggered the scan lives in object storage and never crosses this API.

All surfaces are tenant-scoped exactly like the other admin routes: an
operator may touch only tenants their membership grants, and a refused request
is indistinguishable from an absent record.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from tenantchat.api.dependencies import (
    Audit,
    GenerationFindings,
    Jobs,
    Knowledge,
    Memberships,
    ObjectStores,
    Registry,
    RequestId,
    SearchIndexes,
    get_settings,
)
from tenantchat.api.faults import (
    EmptyUploadError,
    SearchIndexUnavailableError,
    StorageUnavailableError,
)
from tenantchat.api.identity import (
    AdminIdentity,
    authorize_tenant_access,
    require_role,
    tenant_scoped,
    verify_csrf,
)
from tenantchat.api.index_integrity import IndexIntegrityDetector, IndexIntegrityStore
from tenantchat.api.ingestion import submit_ingestion
from tenantchat.api.parsing import (
    MAX_DOCUMENT_BYTES,
    SUPPORTED_MEDIA_TYPES,
    chunk_document,
    parse_document,
    scan_bytes,
)
from tenantchat.api.schemas import (
    KnowledgeDeleteRequest,
    KnowledgeDocumentDetailResponse,
    KnowledgeFindingsResponse,
    KnowledgeFindingSummary,
    KnowledgePreviewBlock,
    KnowledgePreviewResponse,
    KnowledgeResponse,
    KnowledgeSourceCreateRequest,
    KnowledgeSourceEnabledRequest,
    KnowledgeSourceResponse,
    KnowledgeSourceSummary,
    KnowledgeVersionActionRequest,
    KnowledgeVersionActionResponse,
    QuarantinedVersionSummary,
    QuarantineListResponse,
    QuarantineReviewRequest,
    QuarantineReviewResponse,
    UploadedVersionResponse,
)
from tenantchat.api.storage import StorageKey, validated_filename
from tenantchat.api.store import AuditActorType, AuditEvent
from tenantchat.core.errors import ValidationError
from tenantchat.core.indexing import IndexGeneration, IndexIntegrityFinding
from tenantchat.core.knowledge import (
    ContentChecksum,
    DocumentVersion,
    KnowledgeDocument,
    KnowledgeDomain,
    SourceKind,
    Visibility,
)
from tenantchat.core.safety import SafetyState

router = APIRouter(tags=["admin-knowledge"])

_tenant_read = tenant_scoped("viewer")
_mutation_role = require_role("tenant_admin")

TenantReader = Annotated[AdminIdentity, Depends(_tenant_read)]
MutationIdentity = Annotated[AdminIdentity, Depends(_mutation_role)]
TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]

# Media types the pipeline can parse: exactly the adapters' set, so a format
# that would become a broken ingestion job is refused at the door. The upload
# budget matches the scanner's, so an oversized document fails here too.
_ACCEPTED_MEDIA_TYPES = SUPPORTED_MEDIA_TYPES
_MAX_UPLOAD_BYTES = MAX_DOCUMENT_BYTES


def _check_brand_consistency(registry: Registry, tenant_id: str, field: str, value: str) -> None:
    """Verify a source or document metadata value does not claim another tenant's brand.

    A source's ``display_name`` or a document's ``title`` that contains another
    registered tenant's name or slug is a content integrity defect: it points
    retrieval at the wrong brand's policy, financing terms, or service area.

    Raises:
        ValidationError: ``value`` contains a known identifier of a different tenant.
    """
    all_tenants = registry.all()
    own = all_tenants.get(tenant_id)
    if own is None:
        return

    folded = value.casefold()

    for other_id, record in all_tenants.items():
        if other_id == tenant_id:
            continue
        other_labels = {record.policy.name.casefold(), record.policy.tenant_id.casefold()}
        for label in other_labels:
            if label in folded:
                raise ValidationError(
                    detail=(
                        f"{field} {value!r} references brand {record.policy.name!r}"
                        f" ({record.policy.tenant_id!r}), not the current tenant"
                        f" {own.policy.name!r} ({own.policy.tenant_id!r})"
                    )
                )


async def _authorize_mutation(
    request: Request,
    identity: AdminIdentity,
    memberships: Memberships,
    tenant_id: str,
) -> None:
    verify_csrf(request, identity, get_settings(request))
    await authorize_tenant_access(
        identity,
        memberships,
        tenant_id,
        minimum="tenant_admin",
        path=request.url.path,
    )


@router.post("/api/admin/knowledge/uploads", response_model=UploadedVersionResponse)
async def upload_knowledge(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    registry: Registry,
    knowledge: Knowledge,
    object_stores: ObjectStores,
    tenant_id: Annotated[str, Form(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")],
    source_id: Annotated[uuid.UUID, Form()],
    external_key: Annotated[str, Form(min_length=1, max_length=512)],
    title: Annotated[str, Form(min_length=1, max_length=300)],
    file: Annotated[UploadFile, File()],
    visibility: Annotated[str, Form()] = "public",
) -> UploadedVersionResponse:
    """Validate an upload, isolate it, stage it, and return its draft identity.

    The multipart form is validated before anything is stored: the filename
    must be a plain name (path traversal is refused by shape), the media type
    must be parseable, and the bytes must fit the upload budget. The stored
    key is derived from the tenant, source, external key, and checksum — never
    from the filename's path.

    Raises:
        NotFoundError: the source is absent or belongs to another tenant.
        ValidationError: the filename, media type, content, or brand consistency
            is rejected.
    """
    await _authorize_mutation(request, identity, memberships, tenant_id)
    validated_filename(file.filename or "")
    if object_stores is None:
        raise StorageUnavailableError

    if file.content_type not in _ACCEPTED_MEDIA_TYPES:
        raise ValidationError(detail="upload media type is not accepted")

    content = await file.read(_MAX_UPLOAD_BYTES + 1)
    if len(content) > _MAX_UPLOAD_BYTES:
        raise ValidationError(detail="upload exceeds the size budget")
    if not content:
        # Refused here rather than at the version table's `byte_size > 0`
        # check: an empty document is a caller mistake the console can point
        # at, not a 500 behind a generic banner.
        raise EmptyUploadError

    source = await knowledge.load_source(tenant_id, source_id)
    _check_brand_consistency(registry, tenant_id, "title", title)
    _check_brand_consistency(registry, tenant_id, "source display_name", source.display_name)

    checksum = ContentChecksum.of(content)
    key = StorageKey.build(
        tenant_id=tenant_id,
        source_id=source_id,
        external_key=external_key,
        checksum=checksum.value,
    )
    await object_stores.put(key, content)

    document = await knowledge.stage_version(
        tenant_id,
        source_id=source_id,
        external_key=external_key,
        title=title,
        checksum=checksum,
        byte_size=len(content),
        media_type=file.content_type,
        storage_key=str(key),
        visibility=Visibility(visibility),
    )
    staged = document.version_with_checksum(checksum)
    if staged is None:
        raise ValidationError(detail="staged version missing after upload")
    return UploadedVersionResponse.of(document, staged)


@router.get("/api/admin/knowledge/index-findings", response_model=KnowledgeFindingsResponse)
async def list_index_findings(
    identity: TenantReader,
    tenant_id: TenantIdQuery,
    registry: Registry,
    findings: GenerationFindings,
    knowledge: Knowledge,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> KnowledgeFindingsResponse:
    """The tenant's open index-integrity findings, newest first.

    Every field is bounded and content-free: the finding type the detector
    emits cannot carry document text, so this surface publishes none. Each
    finding is linked to the affected source version — its document title,
    source name, and revision — so the console renders the fault against the
    content it names.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    records = await findings.active_findings(tenant_id)
    shown = tuple(reversed(records[-limit:]))
    summaries = await _findings_with_source(knowledge, shown)
    return KnowledgeFindingsResponse(findings=summaries, limit=limit)


@router.post(
    "/api/admin/knowledge/index-integrity-check",
    response_model=KnowledgeFindingsResponse,
)
async def run_index_integrity_check(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    tenant_id: TenantIdQuery,
    knowledge: Knowledge,
    findings: GenerationFindings,
    search_index: SearchIndexes,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> KnowledgeFindingsResponse:
    """Detect index-integrity faults for one tenant and persist the findings.

    Runs the detector over the tenant's published and superseded versions and
    the index's actual contents, then reconciles the persisted finding set, so
    `FEAT-001`'s console and `OBS-004` read the same content-free faults the
    check just produced.

    Raises:
        TransportError (503): the deployment composed no search index.
    """
    await _authorize_mutation(request, identity, memberships, tenant_id)
    if search_index is None:
        raise SearchIndexUnavailableError
    detector = IndexIntegrityDetector(knowledge=knowledge, generations=findings, index=search_index)
    detected = await detector.detect(tenant_id)
    await findings.sync_findings(tenant_id, detected)
    shown = tuple(reversed(detected[-limit:]))
    summaries = await _findings_with_source(knowledge, shown)
    return KnowledgeFindingsResponse(findings=summaries, limit=limit)


@router.post(
    "/api/admin/knowledge/sources",
    response_model=KnowledgeSourceResponse,
)
async def create_source(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    payload: KnowledgeSourceCreateRequest,
    registry: Registry,
    knowledge: Knowledge,
    audit: Audit,
    request_id: RequestId,
) -> KnowledgeSourceResponse:
    """Register a source under one authorized tenant.

    Idempotent on ``(tenant, domain, display_name)``: a re-run of onboarding
    returns the existing source instead of creating a rival. The kind never
    names a filesystem path; it only constrains how a future refresh may work.

    Raises:
        NotFoundError: the tenant is absent or inactive.
        ValidationError: the display_name references another tenant's brand.
    """
    await _authorize_mutation(request, identity, memberships, payload.tenant_id)
    _check_brand_consistency(registry, payload.tenant_id, "display_name", payload.display_name)
    source = await knowledge.register_source(
        payload.tenant_id,
        domain=KnowledgeDomain.parse(payload.domain),
        kind=SourceKind(payload.kind),
        display_name=payload.display_name,
    )
    await audit.record(
        AuditEvent(
            tenant_id=payload.tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="knowledge.source_created",
            resource_type="knowledge_source",
            resource_id=source.source_id,
            request_id=request_id,
            details={"domain": source.domain.value, "kind": source.kind.value},
        )
    )
    return KnowledgeSourceResponse.of(source)


@router.get("/api/admin/knowledge", response_model=KnowledgeResponse)
async def list_knowledge(
    identity: TenantReader,
    tenant_id: TenantIdQuery,
    registry: Registry,
    knowledge: Knowledge,
    findings: GenerationFindings,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> KnowledgeResponse:
    """The tenant's knowledge tree: sources, documents, and versions.

    Content-free by construction — titles, states, counts, and timestamps
    only. Indexing status, chunk count, error codes, and the last successful
    publish ride on each version so an operator can tell ingestion failure
    from retrieval-quality failure without leaving the console.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    generations = {
        generation.version_id: generation
        for generation in await findings.generations_for_tenant(tenant_id)
    }
    sources = await knowledge.list_sources(tenant_id)
    shown = sources[-limit:]
    summaries: list[KnowledgeSourceSummary] = []
    for source in shown:
        documents = await knowledge.documents_for_source(tenant_id, source.source_id)
        summaries.append(KnowledgeSourceSummary.of(source, documents, generations))
    return KnowledgeResponse(sources=summaries, limit=limit)


@router.get(
    "/api/admin/knowledge/documents/{document_id}",
    response_model=KnowledgeDocumentDetailResponse,
)
async def read_document(
    identity: TenantReader,
    tenant_id: TenantIdQuery,
    document_id: uuid.UUID,
    knowledge: Knowledge,
    findings: GenerationFindings,
) -> KnowledgeDocumentDetailResponse:
    """One document with every revision, for the version-history surface.

    Raises:
        NotFoundError: the document is absent or belongs to another tenant.
    """
    document = await knowledge.load_document(tenant_id, document_id)
    generations = {
        generation.version_id: generation
        for generation in await findings.generations_for_tenant(tenant_id)
    }
    return KnowledgeDocumentDetailResponse.of(document, generations)


@router.get(
    "/api/admin/knowledge/versions/{version_id}/preview",
    response_model=KnowledgePreviewResponse,
)
async def preview_version(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    tenant_id: TenantIdQuery,
    version_id: uuid.UUID,
    knowledge: Knowledge,
    object_stores: ObjectStores,
) -> KnowledgePreviewResponse:
    """Parse one version's stored bytes and preview how it will be indexed.

    The preview runs the exact scan and parser the ingestion worker would run,
    so a corrupt or unsupported document is caught here — before approval —
    rather than in a job that fails after the operator said yes. The response
    is bounded: the first handful of source blocks with their chunk count and
    parser version, never the raw file.

    Raises:
        StorageUnavailableError: the deployment composed no object storage.
        NotFoundError: the version is absent or belongs to another tenant.
        ValidationError: the stored content failed scanning or parsing.
    """
    await authorize_tenant_access(
        identity,
        memberships,
        tenant_id,
        minimum="tenant_admin",
        path=request.url.path,
    )
    if object_stores is None:
        raise StorageUnavailableError
    document = await knowledge.document_for_version(tenant_id, version_id)
    version = document.version(version_id)
    content = await object_stores.read(StorageKey.parse(version.storage_key))
    scan_bytes(content)
    parsed = parse_document(content, media_type=version.media_type, title=document.title)
    chunks = chunk_document(parsed)
    blocks = [
        KnowledgePreviewBlock(location=str(block.location), text=block.text[:400])
        for block in parsed.blocks[:20]
    ]
    return KnowledgePreviewResponse(
        version_id=version.version_id,
        document_id=document.document_id,
        title=document.title,
        media_type=parsed.media_type,
        parser_version=parsed.parser_version,
        chunk_count=len(chunks),
        blocks=blocks,
    )


@router.post(
    "/api/admin/knowledge/versions/{version_id}/approve",
    response_model=KnowledgeVersionActionResponse,
)
async def approve_version(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    version_id: uuid.UUID,
    payload: KnowledgeVersionActionRequest,
    knowledge: Knowledge,
    findings: GenerationFindings,
    audit: Audit,
    request_id: RequestId,
) -> KnowledgeVersionActionResponse:
    """Mark a draft reviewed and publishable.

    Draft content stays unretrievable until approval *and* publication both
    land: the retrievability predicate requires a published, indexed version,
    so this step alone never changes what an answer cites.

    Raises:
        NotFoundError: the version is absent or belongs to another tenant.
        InvalidVersionTransitionError: the version is not a draft.
    """
    await _authorize_mutation(request, identity, memberships, payload.tenant_id)
    document = await knowledge.approve(
        payload.tenant_id,
        version_id,
        approved_by=identity.subject,
        at=datetime.now(UTC),
    )
    version = document.version(version_id)
    await _audit_version(
        audit,
        identity,
        payload.tenant_id,
        "knowledge.version_approved",
        request_id,
        document,
        version,
        {},
    )
    return KnowledgeVersionActionResponse.of(
        document, version, await _generations_by_version(findings, payload.tenant_id)
    )


@router.post(
    "/api/admin/knowledge/versions/{version_id}/publish",
    response_model=KnowledgeVersionActionResponse,
)
async def publish_version(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    version_id: uuid.UUID,
    payload: KnowledgeVersionActionRequest,
    knowledge: Knowledge,
    findings: GenerationFindings,
    jobs: Jobs,
    audit: Audit,
    request_id: RequestId,
) -> KnowledgeVersionActionResponse:
    """Make one approved version current and enqueue its ingestion job.

    Publishing a superseded version is a rollback and takes exactly this path:
    the outgoing version is demoted in the same transaction, so there is no
    window in which two versions answer the same question. The ingestion job
    is idempotent per version, so re-publishing an already-indexed version
    rewrites nothing.

    Raises:
        NotFoundError: the version is absent or belongs to another tenant.
        InvalidVersionTransitionError: the version is a draft or deleted.
    """
    await _authorize_mutation(request, identity, memberships, payload.tenant_id)
    document = await knowledge.publish(
        payload.tenant_id,
        version_id,
        at=datetime.now(UTC),
        effective_at=payload.effective_at,
        expires_at=payload.expires_at,
    )
    version = document.version(version_id)
    job = await submit_ingestion(jobs, tenant_id=payload.tenant_id, version_id=version_id)
    await _audit_version(
        audit,
        identity,
        payload.tenant_id,
        "knowledge.version_published",
        request_id,
        document,
        version,
        {"job_id": str(job.job_id)},
    )
    return KnowledgeVersionActionResponse.of(
        document, version, await _generations_by_version(findings, payload.tenant_id), job=job
    )


@router.post(
    "/api/admin/knowledge/versions/{version_id}/reindex",
    response_model=KnowledgeVersionActionResponse,
)
async def reindex_version(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    version_id: uuid.UUID,
    payload: KnowledgeVersionActionRequest,
    knowledge: Knowledge,
    findings: GenerationFindings,
    jobs: Jobs,
    audit: Audit,
    request_id: RequestId,
) -> KnowledgeVersionActionResponse:
    """Re-run a version's ingestion job.

    Idempotent: the durable job deduplicates on the version, and the worker
    reuses the same generation so a retry cannot duplicate active chunks. A
    draft is refused here — indexing unapproved content is one publish away
    from being answerable.

    Raises:
        NotFoundError: the version is absent or belongs to another tenant.
        InvalidVersionTransitionError: the version is a draft or deleted.
    """
    await _authorize_mutation(request, identity, memberships, payload.tenant_id)
    document = await knowledge.document_for_version(payload.tenant_id, version_id)
    document.version_for_indexing(version_id)
    job = await submit_ingestion(jobs, tenant_id=payload.tenant_id, version_id=version_id)
    version = document.version(version_id)
    await _audit_version(
        audit,
        identity,
        payload.tenant_id,
        "knowledge.version_reindexed",
        request_id,
        document,
        version,
        {"job_id": str(job.job_id)},
    )
    return KnowledgeVersionActionResponse.of(
        document, version, await _generations_by_version(findings, payload.tenant_id), job=job
    )


@router.post(
    "/api/admin/knowledge/versions/{version_id}/expire",
    response_model=KnowledgeVersionActionResponse,
)
async def expire_version(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    version_id: uuid.UUID,
    payload: KnowledgeVersionActionRequest,
    knowledge: Knowledge,
    findings: GenerationFindings,
    audit: Audit,
    request_id: RequestId,
) -> KnowledgeVersionActionResponse:
    """End the current version's effective window now.

    The version stays current in history (a rollback target) but stops being
    retrievable from this moment.

    Raises:
        NotFoundError: the version is absent or belongs to another tenant.
        InvalidVersionTransitionError: the version is not the published one.
    """
    await _authorize_mutation(request, identity, memberships, payload.tenant_id)
    document = await knowledge.expire(payload.tenant_id, version_id, at=datetime.now(UTC))
    version = document.version(version_id)
    await _audit_version(
        audit,
        identity,
        payload.tenant_id,
        "knowledge.version_expired",
        request_id,
        document,
        version,
        {},
    )
    return KnowledgeVersionActionResponse.of(
        document, version, await _generations_by_version(findings, payload.tenant_id)
    )


@router.delete(
    "/api/admin/knowledge/documents/{document_id}",
    response_model=KnowledgeDocumentDetailResponse,
)
async def delete_document(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    document_id: uuid.UUID,
    payload: KnowledgeDeleteRequest,
    knowledge: Knowledge,
    findings: GenerationFindings,
    audit: Audit,
    request_id: RequestId,
) -> KnowledgeDocumentDetailResponse:
    """Withdraw a document and every revision of it.

    A tombstone, not a row delete: the audit of what the assistant used to
    answer with stays answerable, and the indexing worker learns the chunks it
    wrote are retracted. Idempotent — deleting a deleted document changes
    nothing.

    Raises:
        NotFoundError: the document is absent or belongs to another tenant.
    """
    await _authorize_mutation(request, identity, memberships, payload.tenant_id)
    document = await knowledge.delete_document(payload.tenant_id, document_id, at=datetime.now(UTC))
    await audit.record(
        AuditEvent(
            tenant_id=payload.tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="knowledge.document_deleted",
            resource_type="knowledge_document",
            resource_id=document_id,
            request_id=request_id,
            details={"document_id": str(document_id)},
        )
    )
    generations = await _generations_by_version(findings, payload.tenant_id)
    return KnowledgeDocumentDetailResponse.of(document, generations)


@router.post(
    "/api/admin/knowledge/sources/{source_id}/enabled",
    response_model=KnowledgeSourceResponse,
)
async def set_source_enabled(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    source_id: uuid.UUID,
    payload: KnowledgeSourceEnabledRequest,
    knowledge: Knowledge,
    audit: Audit,
    request_id: RequestId,
) -> KnowledgeSourceResponse:
    """Withdraw or restore every document under a source at once.

    Disabling never rewrites version history: the documents stay published in
    the system of record, they simply stop answering. Re-enabling restores
    exactly what was withdrawn.

    Raises:
        NotFoundError: the source is absent or belongs to another tenant.
    """
    await _authorize_mutation(request, identity, memberships, payload.tenant_id)
    source = await knowledge.set_source_enabled(
        payload.tenant_id, source_id, enabled=payload.enabled
    )
    await audit.record(
        AuditEvent(
            tenant_id=payload.tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="knowledge.source_enabled" if payload.enabled else "knowledge.source_disabled",
            resource_type="knowledge_source",
            resource_id=source_id,
            request_id=request_id,
            details={"enabled": payload.enabled},
        )
    )
    return KnowledgeSourceResponse.of(source)


@router.get("/api/admin/knowledge/quarantine", response_model=QuarantineListResponse)
async def list_quarantined_versions(
    identity: TenantReader,
    tenant_id: TenantIdQuery,
    registry: Registry,
    knowledge: Knowledge,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> QuarantineListResponse:
    """The tenant's quarantined versions, the review queue.

    The queue the policy detector files into: every version the ingestion
    worker's content-safety scan flagged, still unretrievable for every
    audience until a reviewer acts. Identifiers and states only — the text
    that triggered the quarantine never crosses this surface.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    versions = await knowledge.versions_in_safety_state(tenant_id, SafetyState.QUARANTINED)
    shown = tuple(versions[-limit:])
    summaries: list[QuarantinedVersionSummary] = []
    for version in shown:
        document = await knowledge.document_for_version(tenant_id, version.version_id)
        summaries.append(QuarantinedVersionSummary.of(document, version))
    return QuarantineListResponse(versions=summaries, limit=limit)


@router.post(
    "/api/admin/knowledge/quarantine/{version_id}/review",
    response_model=QuarantineReviewResponse,
)
async def review_quarantined_version(
    request: Request,
    identity: MutationIdentity,
    memberships: Memberships,
    tenant_id: TenantIdQuery,
    version_id: uuid.UUID,
    payload: QuarantineReviewRequest,
    knowledge: Knowledge,
    audit: Audit,
    request_id: RequestId,
) -> QuarantineReviewResponse:
    """Record a reviewer's decision on one quarantined version.

    Approval clears the quarantine so the version may be re-published and
    re-indexed; rejection keeps it quarantined and superseded, the safe
    default. The decision is audited with the reviewer's identity; the
    flagged content itself remains in object storage.

    Raises:
        NotFoundError: the version is absent, deleted, or belongs to another
            tenant.
        InvalidVersionTransitionError: the version is not quarantined.
    """
    await _authorize_mutation(request, identity, memberships, tenant_id)
    document = await knowledge.quarantine_review(
        tenant_id,
        version_id,
        approved=payload.approved,
        reviewed_by=payload.reviewed_by,
        at=datetime.now(UTC),
    )
    version = document.version(version_id)
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="knowledge.quarantine_review",
            resource_type="knowledge_version",
            resource_id=version_id,
            request_id=request_id,
            details={"document_id": str(document.document_id), "approved": payload.approved},
        )
    )
    return QuarantineReviewResponse.of(document, version)


async def _generations_by_version(
    findings: IndexIntegrityStore, tenant_id: str
) -> Mapping[uuid.UUID, IndexGeneration]:
    """One index generation per version, for the operator version summaries."""
    return {
        generation.version_id: generation
        for generation in await findings.generations_for_tenant(tenant_id)
    }


async def _findings_with_source(
    knowledge: Knowledge,
    findings: tuple[IndexIntegrityFinding, ...],
) -> list[KnowledgeFindingSummary]:
    """Attach the affected source version's display metadata to each finding.

    A finding names a document and version; the console links it to the source
    version it affects, so the document title, source name, and revision are
    resolved here — never by reading document content, which does not exist on
    this surface.
    """
    loaded: dict[uuid.UUID, KnowledgeDocument | None] = {}
    for item in findings:
        if item.document_id not in loaded:
            try:
                loaded[item.document_id] = await knowledge.load_document(
                    item.tenant_id, item.document_id
                )
            except Exception:
                loaded[item.document_id] = None
    summaries: list[KnowledgeFindingSummary] = []
    for item in findings:
        document = loaded[item.document_id]
        summary = KnowledgeFindingSummary.of(item)
        if document is not None:
            version = document.version(item.version_id)
            summary.source_name = document.source.display_name
            summary.document_title = document.title
            summary.revision = version.revision
        summaries.append(summary)
    return summaries


async def _audit_version(
    audit: Audit,
    identity: AdminIdentity,
    tenant_id: str,
    action: str,
    request_id: str,
    document: KnowledgeDocument,
    version: DocumentVersion,
    details: dict[str, object],
) -> None:
    """Audit one version mutation to the acting operator and request.

    ``version`` carries only identifiers into the log; its content never does.
    """
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action=action,
            resource_type="knowledge_version",
            resource_id=version.version_id,
            request_id=request_id,
            details={
                "document_id": str(document.document_id),
                "revision": version.revision,
                **details,
            },
        )
    )
