"""The system of record for versioned tenant knowledge.

Every lifecycle write follows the same shape: lock the document row, load the
aggregate, ask the domain for a plan, apply the plan's rows, return the reloaded
aggregate. The lock is what makes two operators publishing different versions
serialize rather than race; the domain plan is what keeps the rules out of SQL,
where they could not be tested without a database and would drift from the
retrieval filter that has to agree with them.

Reads are tenant-qualified in the ``WHERE`` clause rather than filtered after the
fact. A cross-tenant ID therefore returns nothing and raises the same not-found
as an ID that never existed, which is what stops the API from confirming that
another tenant's document exists.

The retrieval index is not written here. Elasticsearch holds derived data and is
rebuilt from these rows (ADR-0003), so a version becomes retrievable only once
the indexing job reports success against it — never as a side effect of the
publish transaction, which cannot span both stores.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tenantchat.api.persistence.tenancy import require_active_tenant
from tenantchat.core.errors import ConflictError, NotFoundError
from tenantchat.core.knowledge import (
    ContentChecksum,
    DocumentVersion,
    KnowledgeDocument,
    KnowledgeDomain,
    KnowledgeSource,
    RetrievalAudience,
    RetrievalContext,
    SourceKind,
    Visibility,
)
from tenantchat.core.lifecycle import IndexingState, VersionState
from tenantchat.core.safety import SafetyState

_DOCUMENT_COLUMNS = """
    d.id AS document_id, d.tenant_id, d.domain, d.external_key, d.title, d.deleted_at,
    s.id AS source_id, s.kind AS source_kind, s.display_name AS source_name,
    s.enabled AS source_enabled
"""

_VERSION_COLUMNS = """
    v.id, v.tenant_id, v.document_id, v.revision, v.state, v.indexing_state,
    v.visibility, v.safety_state, v.checksum, v.byte_size, v.media_type, v.storage_key,
    v.effective_at, v.expires_at, v.approved_at, v.approved_by, v.published_at,
    v.superseded_at, v.indexed_at, v.index_error_code
"""


def _source(row: object) -> KnowledgeSource:
    mapping = row._mapping  # type: ignore[attr-defined]
    return KnowledgeSource(
        source_id=mapping["source_id"],
        tenant_id=mapping["tenant_id"],
        domain=KnowledgeDomain.parse(mapping["domain"]),
        kind=SourceKind(mapping["source_kind"]),
        display_name=mapping["source_name"],
        enabled=mapping["source_enabled"],
    )


def _version(row: object) -> DocumentVersion:
    mapping = row._mapping  # type: ignore[attr-defined]
    return DocumentVersion(
        version_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        document_id=mapping["document_id"],
        revision=mapping["revision"],
        state=VersionState(mapping["state"]),
        indexing_state=IndexingState(mapping["indexing_state"]),
        visibility=Visibility(mapping["visibility"]),
        safety_state=SafetyState(mapping["safety_state"]),
        checksum=ContentChecksum.parse(mapping["checksum"]),
        byte_size=mapping["byte_size"],
        media_type=mapping["media_type"],
        storage_key=mapping["storage_key"],
        effective_at=mapping["effective_at"],
        expires_at=mapping["expires_at"],
        approved_at=mapping["approved_at"],
        approved_by=mapping["approved_by"],
        published_at=mapping["published_at"],
        superseded_at=mapping["superseded_at"],
        indexed_at=mapping["indexed_at"],
        index_error_code=mapping["index_error_code"],
    )


def _document(row: object, versions: tuple[DocumentVersion, ...]) -> KnowledgeDocument:
    mapping = row._mapping  # type: ignore[attr-defined]
    return KnowledgeDocument(
        document_id=mapping["document_id"],
        tenant_id=mapping["tenant_id"],
        source=_source(row),
        external_key=mapping["external_key"],
        title=mapping["title"],
        versions=versions,
        deleted=mapping["deleted_at"] is not None,
    )


async def _load(
    connection: AsyncConnection, tenant_id: str, document_id: uuid.UUID, *, lock: bool
) -> KnowledgeDocument:
    """Read one document and every revision of it.

    ``lock`` takes a row lock on the document, which is the serialization point
    for the whole aggregate: versions are only ever written through a plan built
    from a document loaded this way.

    Raises:
        NotFoundError: if the document is absent or belongs to another tenant.
    """
    header = await connection.execute(
        text(
            f"""
            SELECT {_DOCUMENT_COLUMNS}
            FROM knowledge_documents d
            JOIN knowledge_sources s
              ON s.tenant_id = d.tenant_id AND s.domain = d.domain AND s.id = d.source_id
            WHERE d.tenant_id = :tenant_id AND d.id = :document_id
            {"FOR NO KEY UPDATE OF d" if lock else ""}
            """  # noqa: S608 - `lock` is a bool, not caller text
        ),
        {"tenant_id": tenant_id, "document_id": document_id},
    )
    row = header.one_or_none()
    if row is None:
        raise NotFoundError(detail=f"document {document_id} absent or outside tenant {tenant_id}")

    versions = await connection.execute(
        text(
            f"""
            SELECT {_VERSION_COLUMNS}
            FROM knowledge_document_versions v
            WHERE v.tenant_id = :tenant_id AND v.document_id = :document_id
            ORDER BY v.revision
            """  # noqa: S608 - interpolates a module constant, never caller input
        ),
        {"tenant_id": tenant_id, "document_id": document_id},
    )
    return _document(row, tuple(_version(item) for item in versions.all()))


async def _document_id_for_version(
    connection: AsyncConnection, tenant_id: str, version_id: uuid.UUID
) -> uuid.UUID:
    """Resolve which document a version belongs to.

    Raises:
        NotFoundError: if the version is absent or belongs to another tenant.
    """
    result = await connection.execute(
        text(
            """
            SELECT document_id FROM knowledge_document_versions
            WHERE tenant_id = :tenant_id AND id = :version_id
            """
        ),
        {"tenant_id": tenant_id, "version_id": version_id},
    )
    document_id: uuid.UUID | None = result.scalar_one_or_none()
    if document_id is None:
        raise NotFoundError(detail=f"version {version_id} absent or outside tenant {tenant_id}")
    return document_id


def _live_version_or_raise(document: KnowledgeDocument, version_id: uuid.UUID) -> None:
    """Fail when a version cannot be acted on because its document is gone.

    The worker-facing paths (quarantine) bypass the domain plans, which are the
    only other guard against a stale aggregate; the SQL state guard catches
    state races, this catches deletion races.

    Raises:
        NotFoundError: if the document or the version is deleted.
    """
    if document.deleted or not any(
        version.version_id == version_id for version in document.versions
    ):
        raise NotFoundError(detail=f"version {version_id} absent or deleted")


class PostgresKnowledgeStore:
    """Authoritative storage for knowledge sources, documents, and versions."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def register_source(
        self,
        tenant_id: str,
        *,
        domain: KnowledgeDomain,
        kind: SourceKind,
        display_name: str,
        external_reference: str | None = None,
    ) -> KnowledgeSource:
        """Create a source, or return the one already registered under that name.

        Idempotent on ``(tenant, domain, display_name)`` so a re-run of tenant
        onboarding does not split one body of content across two sources.

        Raises:
            NotFoundError: if the tenant is absent or inactive.
        """
        source_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            inserted = await connection.execute(
                text(
                    """
                    INSERT INTO knowledge_sources
                        (id, tenant_id, domain, kind, display_name, external_reference)
                    VALUES
                        (:id, :tenant_id, :domain, :kind, :display_name, :external_reference)
                    ON CONFLICT (tenant_id, domain, display_name) DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": source_id,
                    "tenant_id": tenant_id,
                    "domain": domain.value,
                    "kind": kind.value,
                    "display_name": display_name,
                    "external_reference": external_reference,
                },
            )
            if inserted.scalar_one_or_none() is None:
                existing = await connection.execute(
                    text(
                        """
                        SELECT id FROM knowledge_sources
                        WHERE tenant_id = :tenant_id AND domain = :domain
                          AND display_name = :display_name
                        """
                    ),
                    {
                        "tenant_id": tenant_id,
                        "domain": domain.value,
                        "display_name": display_name,
                    },
                )
                source_id = existing.scalar_one()
            return await self._read_source(connection, tenant_id, source_id)

    async def set_source_enabled(
        self, tenant_id: str, source_id: uuid.UUID, *, enabled: bool
    ) -> KnowledgeSource:
        """Withdraw or restore every document under a source at once.

        Raises:
            NotFoundError: if the source is absent or belongs to another tenant.
        """
        async with self._engine.begin() as connection:
            updated = await connection.execute(
                text(
                    """
                    UPDATE knowledge_sources
                    SET enabled = :enabled, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :source_id
                    """
                ),
                {"tenant_id": tenant_id, "source_id": source_id, "enabled": enabled},
            )
            if updated.rowcount != 1:
                raise NotFoundError(
                    detail=f"source {source_id} absent or outside tenant {tenant_id}"
                )
            return await self._read_source(connection, tenant_id, source_id)

    async def stage_version(
        self,
        tenant_id: str,
        *,
        source_id: uuid.UUID,
        external_key: str,
        title: str,
        checksum: ContentChecksum,
        byte_size: int,
        media_type: str,
        storage_key: str,
        visibility: Visibility = Visibility.PUBLIC,
    ) -> KnowledgeDocument:
        """Record a draft revision of a document's content.

        Creates the document on first sight of ``external_key``. **Identical
        content returns the existing revision unchanged**, which is what makes a
        repeated upload or a scheduled re-crawl of an unmodified page free: no new
        revision to review, no re-embedding, no duplicate chunks to deactivate.
        The unique index on ``(tenant, document, checksum)`` enforces the same
        thing under concurrency.

        The document's title follows the latest staged revision, because a
        renamed document should not keep showing an operator its old name. The
        content of earlier revisions is untouched.

        Raises:
            NotFoundError: if the tenant, or the source, is absent, inactive, or
                outside the tenant; or if the document was deleted, which is a
                tombstone that re-uploading must not silently reverse.
        """
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            source = await self._read_source(connection, tenant_id, source_id)
            document_id = await self._upsert_document(
                connection,
                tenant_id,
                source=source,
                external_key=external_key,
                title=title,
            )
            document = await _load(connection, tenant_id, document_id, lock=True)
            if document.deleted:
                raise NotFoundError(detail=f"document {document_id} is deleted")

            existing = document.version_with_checksum(checksum)
            if existing is not None:
                return document

            await connection.execute(
                text(
                    """
                    INSERT INTO knowledge_document_versions
                        (id, tenant_id, domain, document_id, revision, visibility,
                         checksum, byte_size, media_type, storage_key)
                    VALUES
                        (:id, :tenant_id, :domain, :document_id, :revision, :visibility,
                         :checksum, :byte_size, :media_type, :storage_key)
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "tenant_id": tenant_id,
                    "domain": document.domain.value,
                    "document_id": document_id,
                    "revision": document.next_revision(),
                    "visibility": visibility.value,
                    "checksum": checksum.value,
                    "byte_size": byte_size,
                    "media_type": media_type,
                    "storage_key": storage_key,
                },
            )
            return await _load(connection, tenant_id, document_id, lock=False)

    async def load_document(self, tenant_id: str, document_id: uuid.UUID) -> KnowledgeDocument:
        """Read one document and its full revision history.

        Raises:
            NotFoundError: if the document is absent or belongs to another tenant.
        """
        async with self._engine.begin() as connection:
            return await _load(connection, tenant_id, document_id, lock=False)

    async def document_for_version(
        self, tenant_id: str, version_id: uuid.UUID
    ) -> KnowledgeDocument:
        """Resolve the document a version belongs to, tenant-qualified.

        The ingestion worker's payload names a version, and the document is
        needed for the domain gate and the document's domain filter.

        Raises:
            NotFoundError: if the version is absent or belongs to another tenant.
        """
        async with self._engine.begin() as connection:
            document_id = await _document_id_for_version(connection, tenant_id, version_id)
            return await _load(connection, tenant_id, document_id, lock=False)

    async def approve(
        self, tenant_id: str, version_id: uuid.UUID, *, approved_by: str, at: datetime
    ) -> KnowledgeDocument:
        """Record that a reviewer accepted a draft.

        Raises:
            NotFoundError: absent or cross-tenant version, or a deleted document.
            InvalidVersionTransitionError: the version is not a draft.
            ValidationError: ``approved_by`` is blank or ``at`` is naive.
        """
        async with self._engine.begin() as connection:
            document_id = await _document_id_for_version(connection, tenant_id, version_id)
            document = await _load(connection, tenant_id, document_id, lock=True)
            plan = document.plan_approval(version_id, approved_by=approved_by, at=at)

            updated = await connection.execute(
                text(
                    """
                    UPDATE knowledge_document_versions
                    SET state = 'approved', approved_at = :approved_at,
                        approved_by = :approved_by, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :version_id AND state = 'draft'
                    """
                ),
                {
                    "tenant_id": plan.tenant_id,
                    "version_id": plan.version_id,
                    "approved_at": plan.approved_at,
                    "approved_by": plan.approved_by,
                },
            )
            _expect_one(updated.rowcount, "approve")
            return await _load(connection, tenant_id, document_id, lock=False)

    async def publish(
        self,
        tenant_id: str,
        version_id: uuid.UUID,
        *,
        at: datetime,
        effective_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> KnowledgeDocument:
        """Make one version current, superseding whichever version was.

        Both writes happen in one transaction, and the outgoing version is
        demoted before the incoming one is promoted so the partial unique index
        never sees two current versions. Publishing a superseded version is a
        rollback and takes exactly this path, which is why a rollback cannot leave
        the two answering the same question differently.

        Raises:
            NotFoundError: absent or cross-tenant version, or a deleted document.
            InvalidVersionTransitionError: the version is a draft or deleted.
            ValidationError: a naive datetime, or an inverted effective window.
            ConflictError: the row state changed after the plan was built.
        """
        async with self._engine.begin() as connection:
            document_id = await _document_id_for_version(connection, tenant_id, version_id)
            document = await _load(connection, tenant_id, document_id, lock=True)
            plan = document.plan_publication(
                version_id, at=at, effective_at=effective_at, expires_at=expires_at
            )

            if plan.supersedes_version_id is not None:
                superseded = await connection.execute(
                    text(
                        """
                        UPDATE knowledge_document_versions
                        SET state = 'superseded', superseded_at = :at, updated_at = now()
                        WHERE tenant_id = :tenant_id AND id = :version_id AND state = 'published'
                        """
                    ),
                    {
                        "tenant_id": plan.tenant_id,
                        "version_id": plan.supersedes_version_id,
                        "at": plan.published_at,
                    },
                )
                _expect_one(superseded.rowcount, "supersede")

            promoted = await connection.execute(
                text(
                    """
                    UPDATE knowledge_document_versions
                    SET state = 'published', published_at = :published_at,
                        effective_at = :effective_at, expires_at = :expires_at,
                        superseded_at = NULL, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :version_id
                      AND state IN ('approved', 'published', 'superseded')
                    """
                ),
                {
                    "tenant_id": plan.tenant_id,
                    "version_id": plan.version_id,
                    "published_at": plan.published_at,
                    "effective_at": plan.effective_at,
                    "expires_at": plan.expires_at,
                },
            )
            _expect_one(promoted.rowcount, "publish")
            return await _load(connection, tenant_id, document_id, lock=False)

    async def expire(
        self, tenant_id: str, version_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        """End the current version's effective window.

        The version stays current so history still shows what the document last
        said; it simply stops being retrievable from ``at``.

        Raises:
            NotFoundError: absent or cross-tenant version, or a deleted document.
            InvalidVersionTransitionError: the version is not the published one.
            ValidationError: ``at`` is naive or not after the effective time.
        """
        async with self._engine.begin() as connection:
            document_id = await _document_id_for_version(connection, tenant_id, version_id)
            document = await _load(connection, tenant_id, document_id, lock=True)
            plan = document.plan_expiry(version_id, at=at)

            updated = await connection.execute(
                text(
                    """
                    UPDATE knowledge_document_versions
                    SET expires_at = :expires_at, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :version_id AND state = 'published'
                    """
                ),
                {
                    "tenant_id": plan.tenant_id,
                    "version_id": plan.version_id,
                    "expires_at": plan.expires_at,
                },
            )
            _expect_one(updated.rowcount, "expire")
            return await _load(connection, tenant_id, document_id, lock=False)

    async def record_indexing_started(
        self, tenant_id: str, version_id: uuid.UUID
    ) -> KnowledgeDocument:
        """Record that an ingestion job has claimed this version for indexing.

        Sets the ``indexing`` state the durable worker writes to, so a
        published version waiting on the queue is visibly ``pending`` and one
        mid-flight is visibly ``indexing`` rather than silently either.

        Raises:
            NotFoundError: absent or cross-tenant version, or a deleted document.
            InvalidVersionTransitionError: the version is a draft or deleted.
        """
        return await self._record_indexing(
            tenant_id,
            version_id,
            state=IndexingState.INDEXING,
            indexed_at=None,
            error_code=None,
        )

    async def record_indexed(
        self, tenant_id: str, version_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        """Record that the retrieval index now holds this version's chunks.

        Published content is not retrievable until this lands, so the window
        between the publish commit and the indexing job is visible rather than
        answered from an index that does not yet contain the content.

        Raises:
            NotFoundError: absent or cross-tenant version, or a deleted document.
            InvalidVersionTransitionError: the version is a draft or deleted.
        """
        return await self._record_indexing(
            tenant_id,
            version_id,
            state=IndexingState.INDEXED,
            indexed_at=at,
            error_code=None,
        )

    async def record_index_failure(
        self, tenant_id: str, version_id: uuid.UUID, *, error_code: str
    ) -> KnowledgeDocument:
        """Record that indexing failed, leaving the version unretrievable.

        ``error_code`` is a stable classification, never an upstream message: the
        message routinely carries index names, hostnames, and document text.

        Raises:
            NotFoundError: absent or cross-tenant version, or a deleted document.
            InvalidVersionTransitionError: the version is a draft or deleted.
        """
        return await self._record_indexing(
            tenant_id,
            version_id,
            state=IndexingState.FAILED,
            indexed_at=None,
            error_code=error_code,
        )

    async def delete_document(
        self, tenant_id: str, document_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        """Withdraw a document and every revision of it.

        A tombstone rather than a row delete: the indexing worker has to learn
        that chunks it wrote earlier are now retracted, and an audit of what the
        assistant used to answer with has to remain answerable.

        Idempotent — deleting an already-deleted document changes nothing.

        Raises:
            NotFoundError: if the document is absent or belongs to another tenant.
        """
        async with self._engine.begin() as connection:
            document = await _load(connection, tenant_id, document_id, lock=True)
            if document.deleted:
                return document

            await connection.execute(
                text(
                    """
                    UPDATE knowledge_document_versions
                    SET state = 'deleted', deleted_at = :at, updated_at = now()
                    WHERE tenant_id = :tenant_id AND document_id = :document_id
                      AND state <> 'deleted'
                    """
                ),
                {"tenant_id": tenant_id, "document_id": document_id, "at": at},
            )
            updated = await connection.execute(
                text(
                    """
                    UPDATE knowledge_documents
                    SET deleted_at = :at, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :document_id AND deleted_at IS NULL
                    """
                ),
                {"tenant_id": tenant_id, "document_id": document_id, "at": at},
            )
            _expect_one(updated.rowcount, "delete")
            return await _load(connection, tenant_id, document_id, lock=False)

    async def retrievable_versions(self, context: RetrievalContext) -> tuple[DocumentVersion, ...]:
        """Every version that may ground an answer for this context, right now.

        This is the SQL half of the predicate specified by
        :meth:`~tenantchat.core.knowledge.KnowledgeDocument.retrievable_version`,
        and the two are asserted to agree in the repository tests. `RAG-004`
        applies the same filters inside the search query; a filter dropped from
        one of the three is how another tenant's withdrawn draft ends up in an
        answer.
        """
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_VERSION_COLUMNS}
                    FROM knowledge_document_versions v
                    JOIN knowledge_documents d
                      ON d.tenant_id = v.tenant_id AND d.id = v.document_id
                    JOIN knowledge_sources s
                      ON s.tenant_id = d.tenant_id AND s.id = d.source_id
                    WHERE v.tenant_id = :tenant_id
                      AND v.domain = :domain
                      AND v.state = 'published'
                      AND v.indexing_state = 'indexed'
                      AND v.safety_state = 'clear'
                      AND d.deleted_at IS NULL
                      AND s.enabled
                      AND v.effective_at <= :moment
                      AND (v.expires_at IS NULL OR v.expires_at > :moment)
                      -- Cast explicitly: as a bare OR operand the parameter has
                      -- no inferable type and the statement fails to prepare.
                      AND (v.visibility = 'public' OR CAST(:staff AS boolean))
                    ORDER BY v.document_id, v.revision
                    """  # noqa: S608 - interpolates a module constant, never caller input
                ),
                {
                    "tenant_id": context.tenant_id,
                    "domain": context.domain.value,
                    "moment": context.moment,
                    "staff": context.audience is RetrievalAudience.STAFF,
                },
            )
            return tuple(_version(row) for row in result.all())

    async def versions_in_state(
        self, tenant_id: str, state: VersionState
    ) -> tuple[DocumentVersion, ...]:
        """Every version of one tenant currently in one state.

        Feeds the index-integrity detector: published versions are checked for
        missing, partial, mismatched, and lagging index contents, and superseded
        versions for chunks that remain retrievable.

        Raises:
            NotFoundError: if the tenant is absent or inactive.
        """
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_VERSION_COLUMNS}
                    FROM knowledge_document_versions v
                    WHERE v.tenant_id = :tenant_id AND v.state = :state
                    ORDER BY v.document_id, v.revision
                    """  # noqa: S608 - interpolates a module constant, never caller input
                ),
                {"tenant_id": tenant_id, "state": state.value},
            )
            return tuple(_version(row) for row in result.all())

    @property
    def engine(self) -> AsyncEngine:
        """Expose the engine so sibling stores can share one connection pool."""
        return self._engine

    async def quarantine(
        self, tenant_id: str, version_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        """Quarantine a version at the worker's request.

        A worker safety action, not a reviewer decision. Only the safety state
        is written — the lifecycle state is untouched, because quarantine
        survives across re-publishing and only a review clears it (`RAG-007`).
        The indexing state is reset, so even a version that was fully indexed
        must be re-scanned and re-embedded after review approval before it can
        answer again: the flagged bytes cannot return to retrieval from the
        index they were written to.

        Raises:
            NotFoundError: if the document is deleted or the version is unknown.
            ConflictError: if the version is a draft or deleted — quarantine
                before review is meaningless, and withdrawing it is the
                operator's job.
        """
        async with self._engine.begin() as connection:
            document_id = await _document_id_for_version(connection, tenant_id, version_id)
            document = await _load(connection, tenant_id, document_id, lock=True)
            _live_version_or_raise(document, version_id)

            updated = await connection.execute(
                text(
                    """
                    UPDATE knowledge_document_versions
                    SET safety_state = 'quarantined', indexing_state = 'pending',
                        indexed_at = NULL, index_error_code = NULL, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :version_id
                      AND state IN ('approved', 'published', 'superseded')
                    """
                ),
                {"tenant_id": tenant_id, "version_id": version_id, "at": at},
            )
            _expect_one(updated.rowcount, "quarantine")
            return await _load(connection, tenant_id, document_id, lock=False)

    async def quarantine_review(
        self,
        tenant_id: str,
        version_id: uuid.UUID,
        *,
        approved: bool,
        reviewed_by: str,
        at: datetime,
    ) -> KnowledgeDocument:
        """Apply a reviewer's decision on a quarantined version.

        Approval clears the quarantine so the ingestion worker re-embeds the
        reviewed bytes; rejection leaves the version quarantined and superseded.

        Raises:
            NotFoundError: if the document is deleted or the version is unknown.
            InvalidVersionTransitionError: if the version is not quarantined.
        """
        async with self._engine.begin() as connection:
            document_id = await _document_id_for_version(connection, tenant_id, version_id)
            document = await _load(connection, tenant_id, document_id, lock=True)
            plan = document.plan_quarantine_review(
                version_id, approved=approved, reviewed_by=reviewed_by, at=at
            )

            updated = await connection.execute(
                text(
                    """
                    UPDATE knowledge_document_versions
                    SET safety_state = :safety_state, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :version_id
                      AND safety_state = 'quarantined'
                    """
                ),
                {
                    "tenant_id": plan.tenant_id,
                    "version_id": plan.version_id,
                    "safety_state": (
                        SafetyState.CLEAR.value if plan.approved else SafetyState.QUARANTINED.value
                    ),
                },
            )
            _expect_one(updated.rowcount, "quarantine review")
            return await _load(connection, tenant_id, document_id, lock=False)

    async def versions_in_safety_state(
        self, tenant_id: str, safety_state: SafetyState
    ) -> tuple[DocumentVersion, ...]:
        """Every version of one tenant in one safety state.

        Feeds the policy detector: quarantined versions must never be indexed
        or retrievable, and a version the worker quarantined behind the
        detector's back is exactly what this sweep surfaces.

        Raises:
            NotFoundError: if the tenant is absent or inactive.
        """
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_VERSION_COLUMNS}
                    FROM knowledge_document_versions v
                    WHERE v.tenant_id = :tenant_id AND v.safety_state = :safety_state
                    ORDER BY v.document_id, v.revision
                    """  # noqa: S608 - interpolates a module constant, never caller input
                ),
                {"tenant_id": tenant_id, "safety_state": safety_state.value},
            )
            return tuple(_version(row) for row in result.all())

    async def list_sources(self, tenant_id: str) -> tuple[KnowledgeSource, ...]:
        """Every source one tenant owns, for the admin console.

        Raises:
            NotFoundError: if the tenant is absent or inactive.
        """
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    """
                    SELECT id AS source_id, tenant_id, domain, kind AS source_kind,
                           display_name AS source_name, enabled AS source_enabled
                    FROM knowledge_sources
                    WHERE tenant_id = :tenant_id
                    ORDER BY domain, display_name
                    """
                ),
                {"tenant_id": tenant_id},
            )
            return tuple(_source(row) for row in result.all())

    async def load_source(self, tenant_id: str, source_id: uuid.UUID) -> KnowledgeSource:
        """One source, tenant-qualified.

        Raises:
            NotFoundError: if the source is absent or belongs to another tenant.
        """
        async with self._engine.begin() as connection:
            return await self._read_source(connection, tenant_id, source_id)

    async def documents_for_source(
        self, tenant_id: str, source_id: uuid.UUID
    ) -> tuple[KnowledgeDocument, ...]:
        """Every document under one source, with full revision history.

        Raises:
            NotFoundError: if the source is absent, belongs to another tenant,
                or the tenant is inactive.
        """
        async with self._engine.begin() as connection:
            await self._read_source(connection, tenant_id, source_id)
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_DOCUMENT_COLUMNS}
                    FROM knowledge_documents d
                    JOIN knowledge_sources s
                      ON s.tenant_id = d.tenant_id AND s.domain = d.domain AND s.id = d.source_id
                    WHERE d.tenant_id = :tenant_id AND d.source_id = :source_id
                    ORDER BY d.external_key
                    """  # noqa: S608 - interpolates a module constant, never caller input
                ),
                {"tenant_id": tenant_id, "source_id": source_id},
            )
            documents: list[KnowledgeDocument] = []
            for row in result.all():
                versions = await connection.execute(
                    text(
                        f"""
                        SELECT {_VERSION_COLUMNS}
                        FROM knowledge_document_versions v
                        WHERE v.tenant_id = :tenant_id AND v.document_id = :document_id
                        ORDER BY v.revision
                        """  # noqa: S608 - interpolates a module constant, never caller input
                    ),
                    {"tenant_id": tenant_id, "document_id": row._mapping["document_id"]},
                )
                documents.append(_document(row, tuple(_version(item) for item in versions.all())))
            return tuple(documents)

    async def _record_indexing(
        self,
        tenant_id: str,
        version_id: uuid.UUID,
        *,
        state: IndexingState,
        indexed_at: datetime | None,
        error_code: str | None,
    ) -> KnowledgeDocument:
        async with self._engine.begin() as connection:
            document_id = await _document_id_for_version(connection, tenant_id, version_id)
            document = await _load(connection, tenant_id, document_id, lock=True)
            document.version_for_indexing(version_id)

            updated = await connection.execute(
                text(
                    """
                    UPDATE knowledge_document_versions
                    SET indexing_state = :state, indexed_at = :indexed_at,
                        index_error_code = :error_code, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :version_id AND state <> 'draft'
                    """
                ),
                {
                    "tenant_id": tenant_id,
                    "version_id": version_id,
                    "state": state.value,
                    "indexed_at": indexed_at,
                    "error_code": error_code,
                },
            )
            _expect_one(updated.rowcount, "record indexing")
            return await _load(connection, tenant_id, document_id, lock=False)

    async def _read_source(
        self, connection: AsyncConnection, tenant_id: str, source_id: uuid.UUID
    ) -> KnowledgeSource:
        result = await connection.execute(
            text(
                """
                SELECT id AS source_id, tenant_id, domain, kind AS source_kind,
                       display_name AS source_name, enabled AS source_enabled
                FROM knowledge_sources
                WHERE tenant_id = :tenant_id AND id = :source_id
                """
            ),
            {"tenant_id": tenant_id, "source_id": source_id},
        )
        row = result.one_or_none()
        if row is None:
            raise NotFoundError(detail=f"source {source_id} absent or outside tenant {tenant_id}")
        return _source(row)

    async def _upsert_document(
        self,
        connection: AsyncConnection,
        tenant_id: str,
        *,
        source: KnowledgeSource,
        external_key: str,
        title: str,
    ) -> uuid.UUID:
        parameters = {
            "id": uuid.uuid4(),
            "tenant_id": tenant_id,
            "domain": source.domain.value,
            "source_id": source.source_id,
            "external_key": external_key,
            "title": title,
        }
        inserted = await connection.execute(
            text(
                """
                INSERT INTO knowledge_documents
                    (id, tenant_id, domain, source_id, external_key, title)
                VALUES
                    (:id, :tenant_id, :domain, :source_id, :external_key, :title)
                ON CONFLICT (tenant_id, source_id, external_key)
                DO UPDATE SET title = EXCLUDED.title, updated_at = now()
                RETURNING id
                """
            ),
            parameters,
        )
        document_id: uuid.UUID = inserted.scalar_one()
        return document_id


def _expect_one(rowcount: int, operation: str) -> None:
    """Fail loudly when a guarded update matched something other than one row.

    The guards repeat the state the plan was built from, so a miss means the row
    changed between the read and the write despite the lock — a bug, or a caller
    holding a stale aggregate. Committing the rest of the transaction on top of
    that is how a document ends up with no current version at all.

    Raises:
        ConflictError: if ``rowcount`` is not exactly 1.
    """
    if rowcount != 1:
        raise ConflictError(detail=f"{operation} matched {rowcount} rows, expected 1")
