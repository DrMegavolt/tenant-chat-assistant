"""Index-integrity detection: what the index holds versus what Postgres recorded.

The retrieval index is derived data that fails independently of the publish
transaction (ADR-0003), so "is content answerable?" cannot be trusted to the
ingestion job's own success record. This detector runs over the authoritative
versions in the knowledge store and the actual contents of the index, and turns
every divergence into a bounded, content-free :class:`IndexIntegrityFinding`
that `FEAT-001`'s console and `OBS-004`'s failure attribution read from the
same persisted store.

Each fault names the tenant-qualified source version and, where one exists,
the index generation involved. ``detail`` carries counts, model names, and
thresholds only — the finding type pins that contract.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Protocol

from tenantchat.api.search import SearchIndex
from tenantchat.core.indexing import (
    INDEX_LAG_THRESHOLD,
    GenerationStatus,
    IndexGeneration,
    IndexingFault,
    IndexIntegrityFinding,
)
from tenantchat.core.knowledge import DocumentVersion
from tenantchat.core.lifecycle import IndexingState, VersionState


class IndexIntegrityStore(Protocol):
    """Where generations and findings are persisted for operators and traces."""

    async def begin_generation(self, generation: IndexGeneration) -> IndexGeneration: ...
    async def complete_generation(self, generation: IndexGeneration) -> IndexGeneration: ...
    async def fail_generation(self, generation: IndexGeneration) -> IndexGeneration: ...
    async def generation(self, tenant_id: str, version_id: uuid.UUID) -> IndexGeneration | None: ...
    async def generations_for_tenant(self, tenant_id: str) -> tuple[IndexGeneration, ...]: ...
    async def sync_findings(
        self, tenant_id: str, findings: Sequence[IndexIntegrityFinding]
    ) -> None:
        """Replace the tenant's persisted finding set with the detected set.

        First-detected timestamps survive re-detection; a finding that stops
        occurring disappears, which is what lets a console show the current
        truth without a separate resolution workflow.
        """
        ...

    async def active_findings(self, tenant_id: str) -> tuple[IndexIntegrityFinding, ...]: ...


class KnowledgeIntegritySource(Protocol):
    """The knowledge-system surface the detector scans."""

    async def versions_in_state(
        self, tenant_id: str, state: VersionState
    ) -> tuple[DocumentVersion, ...]: ...


class InMemoryIndexIntegrityStore:
    """Hermetic fake for the generation and finding repository."""

    def __init__(self) -> None:
        self._generations: dict[tuple[str, uuid.UUID], IndexGeneration] = {}
        self._findings: dict[tuple[str, uuid.UUID, IndexingFault], IndexIntegrityFinding] = {}

    async def begin_generation(self, generation: IndexGeneration) -> IndexGeneration:
        self._generations[(generation.tenant_id, generation.version_id)] = generation
        return generation

    async def complete_generation(self, generation: IndexGeneration) -> IndexGeneration:
        self._generations[(generation.tenant_id, generation.version_id)] = generation
        return generation

    async def fail_generation(self, generation: IndexGeneration) -> IndexGeneration:
        self._generations[(generation.tenant_id, generation.version_id)] = generation
        return generation

    async def generation(self, tenant_id: str, version_id: uuid.UUID) -> IndexGeneration | None:
        return self._generations.get((tenant_id, version_id))

    async def generations_for_tenant(self, tenant_id: str) -> tuple[IndexGeneration, ...]:
        return tuple(
            generation
            for (owner, _version), generation in self._generations.items()
            if owner == tenant_id
        )

    async def sync_findings(
        self, tenant_id: str, findings: Sequence[IndexIntegrityFinding]
    ) -> None:
        current = {(finding.version_id, finding.code): finding for finding in findings}
        for key in list(self._findings):
            if key[0] == tenant_id and (key[1], key[2]) not in current:
                del self._findings[key]
        for finding in findings:
            key = (tenant_id, finding.version_id, finding.code)
            existing = self._findings.get(key)
            if existing is None:
                self._findings[key] = finding
            else:
                # Keep the first detection timestamp so the console shows how
                # long a fault has been open, not when it last reoccurred.
                self._findings[key] = replace(finding, detected_at=existing.detected_at)

    async def active_findings(self, tenant_id: str) -> tuple[IndexIntegrityFinding, ...]:
        return tuple(
            finding for finding in self._findings.values() if finding.tenant_id == tenant_id
        )


class IndexIntegrityDetector:
    """Compares the index's contents with the recorded generations and states.

    The detector reports no findings when the index refuses a query: a false
    "all clear" is worse than no answer, and the outage is visible through the
    index's own availability surface.
    """

    def __init__(
        self,
        *,
        knowledge: KnowledgeIntegritySource,
        generations: IndexIntegrityStore,
        index: SearchIndex,
    ) -> None:
        self._knowledge = knowledge
        self._generations = generations
        self._index = index

    async def detect(
        self, tenant_id: str, *, now: datetime | None = None
    ) -> tuple[IndexIntegrityFinding, ...]:
        """Detect every index-integrity fault for one tenant."""
        moment = now or datetime.now(UTC)
        findings: list[IndexIntegrityFinding] = []
        generations = {
            generation.version_id: generation
            for generation in await self._generations.generations_for_tenant(tenant_id)
        }
        published = await self._knowledge.versions_in_state(tenant_id, VersionState.PUBLISHED)
        superseded = await self._knowledge.versions_in_state(tenant_id, VersionState.SUPERSEDED)

        for version in published:
            findings.extend(await self._published_findings(tenant_id, version, generations, moment))
            findings.extend(await self._lag_finding(version, moment))

        for version in superseded:
            findings.extend(await self._superseded_findings(tenant_id, version))

        return tuple(findings)

    async def _published_findings(
        self,
        tenant_id: str,
        version: DocumentVersion,
        generations: dict[uuid.UUID, IndexGeneration],
        now: datetime,
    ) -> tuple[IndexIntegrityFinding, ...]:
        findings: list[IndexIntegrityFinding] = []
        generation = generations.get(version.version_id)

        if generation is None:
            if version.indexing_state is IndexingState.INDEXED:
                findings.append(
                    _finding(
                        IndexingFault.MISSING_GENERATION,
                        tenant_id,
                        version,
                        None,
                        now,
                        detail={"indexing_state": version.indexing_state.value},
                    )
                )
            return tuple(findings)

        if generation.status is GenerationStatus.COMPLETE:
            indexed = await self._index.active_chunk_count(
                tenant_id=tenant_id, version_id=version.version_id
            )
            if indexed < generation.chunk_count:
                findings.append(
                    _finding(
                        IndexingFault.PARTIAL_GENERATION,
                        tenant_id,
                        version,
                        generation.generation_id,
                        now,
                        detail={
                            "indexed": indexed,
                            "recorded": generation.chunk_count,
                        },
                    )
                )
            if indexed != generation.chunk_count:
                findings.append(
                    _finding(
                        IndexingFault.CHUNK_COUNT_MISMATCH,
                        tenant_id,
                        version,
                        generation.generation_id,
                        now,
                        detail={
                            "indexed": indexed,
                            "recorded": generation.chunk_count,
                        },
                    )
                )
            models = await self._index.active_embedding_models(
                tenant_id=tenant_id, version_id=version.version_id
            )
            if models and any(model != generation.embedding_model for model in models):
                findings.append(
                    _finding(
                        IndexingFault.EMBEDDING_MODEL_MISMATCH,
                        tenant_id,
                        version,
                        generation.generation_id,
                        now,
                        detail={
                            "recorded_model": generation.embedding_model,
                            "index_models": list(models),
                        },
                    )
                )
        return tuple(findings)

    async def _lag_finding(
        self, version: DocumentVersion, now: datetime
    ) -> tuple[IndexIntegrityFinding, ...]:
        # ``effective_at`` is the publish anchor (never None on a published
        # version), and a scheduled publication with a future effective date
        # has a negative delta — not yet lagging, which is the right reading.
        if (
            version.indexing_state
            in {IndexingState.PENDING, IndexingState.INDEXING, IndexingState.FAILED}
            and version.effective_at is not None
            and now - version.effective_at > INDEX_LAG_THRESHOLD
        ):
            return (
                _finding(
                    IndexingFault.LAG,
                    version.tenant_id,
                    version,
                    None,
                    now,
                    detail={
                        "threshold_hours": INDEX_LAG_THRESHOLD.total_seconds() / 3600,
                        "state": version.indexing_state.value,
                    },
                ),
            )
        return ()

    async def _superseded_findings(
        self, tenant_id: str, version: DocumentVersion
    ) -> tuple[IndexIntegrityFinding, ...]:
        active = await self._index.active_version_ids(
            tenant_id=tenant_id, document_id=version.document_id
        )
        if version.version_id in active:
            return (
                _finding(
                    IndexingFault.SUPERSEDED_RETRIEVABLE,
                    tenant_id,
                    version,
                    None,
                    datetime.now(UTC),
                    detail={"active_versions": [str(item) for item in active]},
                ),
            )
        return ()


def _finding(
    code: IndexingFault,
    tenant_id: str,
    version: DocumentVersion,
    generation_id: uuid.UUID | None,
    now: datetime,
    *,
    detail: dict[str, object],
) -> IndexIntegrityFinding:
    return IndexIntegrityFinding(
        code=code,
        tenant_id=tenant_id,
        document_id=version.document_id,
        version_id=version.version_id,
        generation_id=generation_id,
        detected_at=now,
        detail=detail,
    )
