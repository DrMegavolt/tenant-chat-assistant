"""Index generations and findings against a real PostgreSQL (RAG-002).

The generation and finding repositories are the durable half of `OBS-004`'s
failure attribution, so they are tested against Postgres rather than the fake:
the one-generation-per-version upsert and the reconcile semantics are enforced
by the schema, not by convention.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest

from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresKnowledgeStore,
)
from tenantchat.api.persistence.index_integrity import PostgresIndexIntegrityStore
from tenantchat.api.registry import TenantRegistry
from tenantchat.core.indexing import (
    GenerationStatus,
    IndexGeneration,
    IndexingFault,
    IndexIntegrityFinding,
)
from tenantchat.core.knowledge import ContentChecksum, KnowledgeDomain, SourceKind
from tenantchat.core.lifecycle import VersionState

TEST_POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=5)
FINANCING = KnowledgeDomain.parse("financing")
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
TENANT = "clearview"
OTHER_TENANT = "apex"


async def _database(repository_database_url: str) -> Database:
    database = Database.connect(repository_database_url, TEST_POOL)
    registry = TenantRegistry.seeded()
    await database.synchronize_tenants(
        (record.policy.tenant_id, record.policy.name) for record in registry.all().values()
    )
    return database


def run(
    repository_database_url: str,
    scenario: Callable[[PostgresKnowledgeStore, PostgresIndexIntegrityStore], Awaitable[None]],
) -> None:
    async def main() -> None:
        database = await _database(repository_database_url)
        try:
            await scenario(
                PostgresKnowledgeStore(database.engine),
                PostgresIndexIntegrityStore(database.engine),
            )
        finally:
            await database.dispose()

    asyncio.run(main())


async def stage_published(
    knowledge: PostgresKnowledgeStore, *, content: bytes = b"terms"
) -> uuid.UUID:
    """One published, indexed version, ready for a generation."""
    source = await knowledge.register_source(
        TENANT, domain=FINANCING, kind=SourceKind.UPLOAD, display_name="Brochures"
    )
    checksum = ContentChecksum.of(content)
    document = await knowledge.stage_version(
        TENANT,
        source_id=source.source_id,
        external_key="terms.md",
        title="Terms",
        checksum=checksum,
        byte_size=len(content),
        media_type="text/markdown",
        storage_key=f"tenants/{TENANT}/terms/{checksum.value[:12]}",
    )
    version = document.version_with_checksum(checksum)
    assert version is not None
    await knowledge.approve(TENANT, version.version_id, approved_by="ops@example", at=NOW)
    await knowledge.publish(TENANT, version.version_id, at=NOW)
    await knowledge.record_indexed(TENANT, version.version_id, at=NOW)
    return version.version_id


def generation(
    version_id: uuid.UUID,
    *,
    document_id: uuid.UUID,
    status: GenerationStatus = GenerationStatus.IN_PROGRESS,
) -> IndexGeneration:
    return IndexGeneration(
        generation_id=uuid.uuid4(),
        tenant_id=TENANT,
        document_id=document_id,
        version_id=version_id,
        parser_version="markdown-sections.v1",
        chunker_version="token-window.v1",
        embedding_model="scripted-embedder.v1",
        status=status,
        chunk_count=3,
        indexed_chunk_count=0,
        started_at=NOW,
        completed_at=None if status is GenerationStatus.IN_PROGRESS else NOW,
    )


@pytest.mark.integration
def test_a_generation_round_trips_through_the_lifecycle(repository_database_url: str) -> None:
    async def scenario(
        knowledge: PostgresKnowledgeStore, generations: PostgresIndexIntegrityStore
    ) -> None:
        version_id = await stage_published(knowledge)
        document = await knowledge.document_for_version(TENANT, version_id)
        started = generation(version_id, document_id=document.document_id)

        recorded = await generations.begin_generation(started)
        assert recorded.status is GenerationStatus.IN_PROGRESS
        assert recorded.chunk_count == 3

        complete = generation(
            version_id,
            document_id=document.document_id,
            status=GenerationStatus.COMPLETE,
        )
        await generations.complete_generation(complete)
        finished = await generations.generation(TENANT, version_id)
        assert finished is not None
        assert finished.status is GenerationStatus.COMPLETE
        assert finished.indexed_chunk_count == 0

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_there_is_exactly_one_generation_per_version_and_retries_reuse_it(
    repository_database_url: str,
) -> None:
    """A retried job rewrites its own row; the schema forbids rivals."""

    async def scenario(
        knowledge: PostgresKnowledgeStore, generations: PostgresIndexIntegrityStore
    ) -> None:
        version_id = await stage_published(knowledge)
        document = await knowledge.document_for_version(TENANT, version_id)
        first = generation(version_id, document_id=document.document_id)
        await generations.begin_generation(first)

        retry = generation(
            version_id, document_id=document.document_id, status=GenerationStatus.FAILED
        )
        await generations.fail_generation(retry)

        all_rows = await generations.generations_for_tenant(TENANT)
        assert len(all_rows) == 1
        assert all_rows[0].status is GenerationStatus.FAILED

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_findings_sync_replaces_the_set_and_preserves_first_detection(
    repository_database_url: str,
) -> None:
    async def scenario(
        knowledge: PostgresKnowledgeStore, generations: PostgresIndexIntegrityStore
    ) -> None:
        version_id = await stage_published(knowledge)
        document = await knowledge.document_for_version(TENANT, version_id)
        missing = IndexIntegrityFinding(
            code=IndexingFault.MISSING_GENERATION,
            tenant_id=TENANT,
            document_id=document.document_id,
            version_id=version_id,
            generation_id=None,
            detected_at=NOW,
            detail={},
        )
        await generations.sync_findings(TENANT, [missing])
        active = await generations.active_findings(TENANT)
        assert [finding.code for finding in active] == [IndexingFault.MISSING_GENERATION]

        # A re-detection keeps the first timestamp; a healed fault disappears.
        again = IndexIntegrityFinding(
            code=IndexingFault.MISSING_GENERATION,
            tenant_id=TENANT,
            document_id=document.document_id,
            version_id=version_id,
            generation_id=None,
            detected_at=NOW + timedelta(days=1),
            detail={},
        )
        await generations.sync_findings(TENANT, [again])
        active = await generations.active_findings(TENANT)
        assert len(active) == 1
        assert active[0].detected_at == NOW

        await generations.sync_findings(TENANT, [])
        assert await generations.active_findings(TENANT) == ()

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_findings_reconcile_only_inside_the_callers_tenant(repository_database_url: str) -> None:
    """One tenant's sync must not touch another tenant's finding set."""

    async def scenario(
        knowledge: PostgresKnowledgeStore, generations: PostgresIndexIntegrityStore
    ) -> None:
        version_id = await stage_published(knowledge)
        document = await knowledge.document_for_version(TENANT, version_id)
        recorded = generation(version_id, document_id=document.document_id)
        await generations.begin_generation(recorded)
        finding = IndexIntegrityFinding(
            code=IndexingFault.CHUNK_COUNT_MISMATCH,
            tenant_id=TENANT,
            document_id=document.document_id,
            version_id=version_id,
            generation_id=recorded.generation_id,
            detected_at=NOW,
            detail={"indexed": 2, "recorded": 5},
        )
        await generations.sync_findings(TENANT, [finding])

        # An empty detection for another tenant clears nothing of ours.
        await generations.sync_findings(OTHER_TENANT, [])
        assert len(await generations.active_findings(TENANT)) == 1
        assert await generations.active_findings(OTHER_TENANT) == ()

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_record_indexing_started_marks_a_version_mid_flight(repository_database_url: str) -> None:
    async def scenario(
        knowledge: PostgresKnowledgeStore, generations: PostgresIndexIntegrityStore
    ) -> None:
        source = await knowledge.register_source(
            TENANT, domain=FINANCING, kind=SourceKind.UPLOAD, display_name="Brochures"
        )
        checksum = ContentChecksum.of(b"mid flight")
        document = await knowledge.stage_version(
            TENANT,
            source_id=source.source_id,
            external_key="mid.md",
            title="Mid",
            checksum=checksum,
            byte_size=9,
            media_type="text/markdown",
            storage_key="tenants/clearview/mid",
        )
        version = document.version_with_checksum(checksum)
        assert version is not None
        await knowledge.approve(TENANT, version.version_id, approved_by="ops@example", at=NOW)

        started = await knowledge.record_indexing_started(TENANT, version.version_id)
        assert started.version(version.version_id).indexing_state.value == "indexing"

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_versions_in_state_feeds_the_integrity_detector(repository_database_url: str) -> None:
    async def scenario(
        knowledge: PostgresKnowledgeStore, generations: PostgresIndexIntegrityStore
    ) -> None:
        version_id = await stage_published(knowledge)

        published = await knowledge.versions_in_state(TENANT, VersionState.PUBLISHED)
        assert [version.version_id for version in published] == [version_id]
        assert await knowledge.versions_in_state(TENANT, VersionState.SUPERSEDED) == ()
        assert await knowledge.versions_in_state(OTHER_TENANT, VersionState.PUBLISHED) == ()

    run(repository_database_url, scenario)


@pytest.mark.integration
def test_document_for_version_resolves_and_stays_tenant_qualified(
    repository_database_url: str,
) -> None:
    async def scenario(
        knowledge: PostgresKnowledgeStore, generations: PostgresIndexIntegrityStore
    ) -> None:
        version_id = await stage_published(knowledge)

        document = await knowledge.document_for_version(TENANT, version_id)
        assert document.version(version_id).version_id == version_id

        from tenantchat.core.errors import NotFoundError

        with pytest.raises(NotFoundError):
            await knowledge.document_for_version(OTHER_TENANT, version_id)

    run(repository_database_url, scenario)
