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

Both surfaces are tenant-scoped exactly like the other admin routes: an
operator may touch only tenants their membership grants, and a refused request
is indistinguishable from an absent record.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile

from tenantchat.api.dependencies import (
    GenerationFindings,
    Knowledge,
    Memberships,
    ObjectStores,
    Registry,
    SearchIndexes,
    get_settings,
)
from tenantchat.api.faults import SearchIndexUnavailableError, StorageUnavailableError
from tenantchat.api.identity import (
    AdminIdentity,
    authorize_tenant_access,
    require_role,
    tenant_scoped,
    verify_csrf,
)
from tenantchat.api.index_integrity import IndexIntegrityDetector
from tenantchat.api.schemas import (
    IndexFindingsResponse,
    IndexFindingSummary,
    UploadedVersionResponse,
)
from tenantchat.api.storage import StorageKey, validated_filename
from tenantchat.core.errors import ValidationError
from tenantchat.core.knowledge import ContentChecksum, Visibility

router = APIRouter(tags=["admin-knowledge"])

_tenant_read = tenant_scoped("viewer")
_mutation_role = require_role("tenant_admin")

TenantReader = Annotated[AdminIdentity, Depends(_tenant_read)]
MutationIdentity = Annotated[AdminIdentity, Depends(_mutation_role)]
TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]

# Media types the pipeline can scan and parse today. `RAG-003` widens this as
# production parser adapters land; refusing here is what keeps an unsupported
# format from becoming a broken ingestion job.
_ACCEPTED_MEDIA_TYPES = frozenset({"text/markdown", "text/plain", "text/x-markdown", "text/html"})
_MAX_UPLOAD_BYTES = 10 * 1024 * 1024


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
        ValidationError: the filename, media type, or content is rejected.
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


@router.get("/api/admin/knowledge/index-findings", response_model=IndexFindingsResponse)
async def list_index_findings(
    identity: TenantReader,
    tenant_id: TenantIdQuery,
    registry: Registry,
    findings: GenerationFindings,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> IndexFindingsResponse:
    """The tenant's open index-integrity findings, newest first.

    Every field is bounded and content-free: the finding type the detector
    emits cannot carry document text, so this surface publishes none.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    records = await findings.active_findings(tenant_id)
    shown = tuple(reversed(records[-limit:]))
    return IndexFindingsResponse(
        findings=[IndexFindingSummary.of(item) for item in shown], limit=limit
    )


@router.post(
    "/api/admin/knowledge/index-integrity-check",
    response_model=IndexFindingsResponse,
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
) -> IndexFindingsResponse:
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
    return IndexFindingsResponse(
        findings=[IndexFindingSummary.of(item) for item in shown], limit=limit
    )
