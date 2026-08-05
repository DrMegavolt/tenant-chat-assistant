"""Index-integrity detection: every Gate B fault, bounded and content-free."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

from tenantchat.api.index_integrity import (
    IndexIntegrityDetector,
    InMemoryIndexIntegrityStore,
)
from tenantchat.api.search import IndexedChunk, InMemorySearchIndex
from tenantchat.api.store import InMemoryKnowledgeStore
from tenantchat.core.indexing import (
    INDEX_LAG_THRESHOLD,
    GenerationStatus,
    IndexGeneration,
    IndexingFault,
    IndexIntegrityFinding,
)
from tenantchat.core.knowledge import (
    ContentChecksum,
    KnowledgeDomain,
    SourceKind,
)
from tenantchat.core.lifecycle import IndexingState

FINANCING = KnowledgeDomain.parse("financing")
NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)
TENANT = "clearview"


class World:
    """A knowledge store, generation store, and index seeded to one shape."""

    def __init__(self) -> None:
        self.knowledge = InMemoryKnowledgeStore()
        self.generations = InMemoryIndexIntegrityStore()
        self.index = InMemorySearchIndex()
        self.detector = IndexIntegrityDetector(
            knowledge=self.knowledge, generations=self.generations, index=self.index
        )

    async def publish(
        self,
        *,
        content: bytes = b"terms",
        external_key: str = "terms.md",
        published_at: datetime = NOW,
        indexed: bool = True,
        version_id: uuid.UUID | None = None,
    ) -> uuid.UUID:
        source = await self.knowledge.register_source(
            TENANT, domain=FINANCING, kind=SourceKind.UPLOAD, display_name="Brochures"
        )
        checksum = ContentChecksum.of(content)
        document = await self.knowledge.stage_version(
            TENANT,
            source_id=source.source_id,
            external_key=external_key,
            title="Terms",
            checksum=checksum,
            byte_size=len(content),
            media_type="text/markdown",
            storage_key="tenants/clearview/key",
        )
        staged = document.version_with_checksum(checksum)
        assert staged is not None
        if version_id is not None:
            raise AssertionError("override unused")
        await self.knowledge.approve(
            TENANT, staged.version_id, approved_by="ops@example", at=published_at
        )
        await self.knowledge.publish(TENANT, staged.version_id, at=published_at)
        if indexed:
            await self.knowledge.record_indexed(TENANT, staged.version_id, at=published_at)
        return staged.version_id

    async def complete_generation(
        self, version_id: uuid.UUID, chunk_count: int = 2
    ) -> IndexGeneration:
        generation = IndexGeneration(
            generation_id=uuid.uuid4(),
            tenant_id=TENANT,
            document_id=(await self.knowledge.document_for_version(TENANT, version_id)).document_id,
            version_id=version_id,
            parser_version="markdown-sections.v1",
            chunker_version="token-window.v1",
            embedding_model="scripted-embedder.v1",
            status=GenerationStatus.COMPLETE,
            chunk_count=chunk_count,
            indexed_chunk_count=chunk_count,
            started_at=NOW,
            completed_at=NOW,
        )
        await self.generations.complete_generation(generation)
        return generation

    async def put_chunks(
        self,
        version_id: uuid.UUID,
        generation: IndexGeneration,
        count: int = 2,
    ) -> None:
        document_id = (await self.knowledge.document_for_version(TENANT, version_id)).document_id
        await self.index.index_chunks(
            [
                _chunk(
                    TENANT,
                    document_id,
                    version_id,
                    generation,
                    position,
                    model="scripted-embedder.v1",
                )
                for position in range(count)
            ]
        )

    def codes(self, findings: tuple[IndexIntegrityFinding, ...]) -> set[IndexingFault]:
        return {finding.code for finding in findings}


def _chunk(
    tenant_id: str,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    generation: IndexGeneration,
    position: int,
    *,
    model: str,
) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=f"{generation.generation_id}:{version_id}:{position}",
        tenant_id=tenant_id,
        domain="financing",
        document_id=document_id,
        version_id=version_id,
        generation_id=generation.generation_id,
        title="Terms",
        section="General",
        text=f"chunk {position}",
        embedding_model=model,
        embedding=(0.1, 0.2, 0.3, 0.4),
    )


def test_healthy_content_detects_nothing() -> None:
    asyncio.run(_run_test_healthy_content_detects_nothing())


async def _run_test_healthy_content_detects_nothing() -> None:
    world = World()
    version_id = await world.publish()
    generation = await world.complete_generation(version_id)
    await world.put_chunks(version_id, generation)

    findings = await world.detector.detect(TENANT, now=NOW)
    assert findings == ()


def test_published_indexed_content_without_a_generation_is_missing() -> None:
    asyncio.run(_run_test_published_indexed_content_without_a_generation_is_missing())


async def _run_test_published_indexed_content_without_a_generation_is_missing() -> None:
    world = World()
    version_id = await world.publish()

    findings = await world.detector.detect(TENANT, now=NOW)

    assert world.codes(findings) == {IndexingFault.MISSING_GENERATION}
    finding = findings[0]
    assert finding.version_id == version_id
    assert finding.generation_id is None
    assert "text" not in str(finding.detail)


def test_a_complete_generation_with_fewer_indexed_chunks_is_partial_and_mismatched() -> None:
    asyncio.run(
        _run_test_a_complete_generation_with_fewer_indexed_chunks_is_partial_and_mismatched()
    )


async def _run_test_a_complete_generation_with_fewer_indexed_chunks_is_partial_and_mismatched() -> (
    None
):
    world = World()
    version_id = await world.publish()
    generation = await world.complete_generation(version_id, chunk_count=3)
    await world.put_chunks(version_id, generation, count=2)

    findings = await world.detector.detect(TENANT, now=NOW)

    assert world.codes(findings) == {
        IndexingFault.PARTIAL_GENERATION,
        IndexingFault.CHUNK_COUNT_MISMATCH,
    }
    for finding in findings:
        assert finding.version_id == version_id
        assert finding.generation_id == generation.generation_id


def test_indexed_chunks_embedded_with_a_different_model_are_mismatched() -> None:
    asyncio.run(_run_test_indexed_chunks_embedded_with_a_different_model_are_mismatched())


async def _run_test_indexed_chunks_embedded_with_a_different_model_are_mismatched() -> None:
    world = World()
    version_id = await world.publish()
    generation = await world.complete_generation(version_id)
    await world.put_chunks(version_id, generation, count=2)

    # Re-embed one chunk under a different model, as a partial reindex would.
    document_id = (await world.knowledge.document_for_version(TENANT, version_id)).document_id
    await world.index.index_chunks(
        [
            _chunk(TENANT, document_id, version_id, generation, 0, model="other-model.v9"),
        ]
    )

    findings = await world.detector.detect(TENANT, now=NOW)

    assert world.codes(findings) == {IndexingFault.EMBEDDING_MODEL_MISMATCH}
    assert findings[0].detail["recorded_model"] == "scripted-embedder.v1"
    assert findings[0].detail["index_models"] == ["other-model.v9", "scripted-embedder.v1"]


def test_published_content_waiting_too_long_is_lagging() -> None:
    asyncio.run(_run_test_published_content_waiting_too_long_is_lagging())


async def _run_test_published_content_waiting_too_long_is_lagging() -> None:
    world = World()
    published_at = NOW - INDEX_LAG_THRESHOLD - timedelta(hours=1)
    await world.publish(published_at=published_at, indexed=False)

    quiet = await world.detector.detect(TENANT, now=NOW)
    assert world.codes(quiet) == {IndexingFault.LAG}
    assert quiet[0].detail["threshold_hours"] == 24

    recent = await world.detector.detect(
        TENANT, now=published_at + INDEX_LAG_THRESHOLD - timedelta(hours=1)
    )
    assert world.codes(recent) == set()


def test_failed_indexing_of_published_content_is_lagging_not_missing() -> None:
    asyncio.run(_run_test_failed_indexing_of_published_content_is_lagging_not_missing())


async def _run_test_failed_indexing_of_published_content_is_lagging_not_missing() -> None:
    world = World()
    published_at = NOW - INDEX_LAG_THRESHOLD - timedelta(hours=1)
    version_id = await world.publish(published_at=published_at, indexed=False)
    await world.knowledge.record_index_failure(TENANT, version_id, error_code="embedding_timeout")

    findings = await world.detector.detect(TENANT, now=NOW)
    assert world.codes(findings) == {IndexingFault.LAG}
    assert findings[0].detail["state"] == IndexingState.FAILED.value


def test_a_superseded_version_still_holding_active_chunks_is_retrievable() -> None:
    asyncio.run(_run_test_a_superseded_version_still_holding_active_chunks_is_retrievable())


async def _run_test_a_superseded_version_still_holding_active_chunks_is_retrievable() -> None:
    world = World()
    first_id = await world.publish(content=b"2026 terms", external_key="terms.md")
    second_id = await world.publish(content=b"2027 terms", external_key="terms.md")

    # The superseding generation was indexed, but the old version's chunks were
    # never deactivated — the exact partial-reindex failure mode.
    old_generation = await world.complete_generation(first_id, chunk_count=1)
    await world.put_chunks(first_id, old_generation, count=1)
    new_generation = await world.complete_generation(second_id, chunk_count=1)
    await world.put_chunks(second_id, new_generation, count=1)

    findings = await world.detector.detect(TENANT, now=NOW)

    assert world.codes(findings) == {IndexingFault.SUPERSEDED_RETRIEVABLE}
    assert findings[0].version_id == first_id


def test_findings_reconcile_when_a_fault_clears() -> None:
    asyncio.run(_run_test_findings_reconcile_when_a_fault_clears())


async def _run_test_findings_reconcile_when_a_fault_clears() -> None:
    world = World()
    version_id = await world.publish()

    first = await world.detector.detect(TENANT, now=NOW)
    await world.generations.sync_findings(TENANT, first)
    assert len(await world.generations.active_findings(TENANT)) == 1

    generation = await world.complete_generation(version_id)
    await world.put_chunks(version_id, generation)
    healed = await world.detector.detect(TENANT, now=NOW)
    await world.generations.sync_findings(TENANT, healed)
    assert await world.generations.active_findings(TENANT) == ()


def test_findings_preserve_the_first_detection_timestamp() -> None:
    asyncio.run(_run_test_findings_preserve_the_first_detection_timestamp())


async def _run_test_findings_preserve_the_first_detection_timestamp() -> None:
    world = World()
    await world.publish()

    first = await world.detector.detect(TENANT, now=NOW)
    await world.generations.sync_findings(TENANT, first)

    later = await world.detector.detect(TENANT, now=NOW + timedelta(days=1))
    await world.generations.sync_findings(TENANT, later)

    active = await world.generations.active_findings(TENANT)
    assert len(active) == 1
    assert active[0].detected_at == NOW


def test_findings_are_tenant_scoped() -> None:
    asyncio.run(_run_test_findings_are_tenant_scoped())


async def _run_test_findings_are_tenant_scoped() -> None:
    world = World()
    await world.publish()

    other = IndexIntegrityDetector(
        knowledge=world.knowledge,
        generations=world.generations,
        index=world.index,
    )
    assert await other.detect("apex", now=NOW) == ()

    findings = await world.detector.detect(TENANT, now=NOW)
    await world.generations.sync_findings(TENANT, findings)
    assert (await world.generations.active_findings("apex")) == ()
