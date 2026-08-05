"""The durable ingestion pipeline: parse, scan, chunk, embed, index.

One ingestion job takes one document version from staged content in object
storage to chunks in the retrieval index, and records everything `OBS-004`
needs to distinguish an ingestion failure from a retrieval failure afterwards:

- a deterministic **index generation** per ``(tenant, version)`` — immutable,
  reused across retries, and the unit of cleanup when an attempt dies
  mid-write;
- the parser, chunker, and embedding-model identifiers the generation was
  produced with;
- chunk counts recorded *before* indexing starts, so "the index holds fewer
  chunks than the content produced" is measurable even for a job that never
  finished.

The parser and chunker here are the prototype Markdown adapters, pinned to
versioned identifiers. `RAG-003` replaces them with production adapters behind
the same job shape; the version identifiers are what make that swap visible to
`OBS-004` instead of silent.

Every failure surfaces as a bounded safe code through
:class:`~tenantchat.api.jobs.JobExecutionError`; document content never
reaches a code, a message, or the durable job record.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from dataclasses import replace
from datetime import UTC, datetime

from tenantchat.api.index_integrity import IndexIntegrityStore
from tenantchat.api.jobs import JobExecutionError, JobHandler, JobKind, JobRecord, JobStore
from tenantchat.api.search import (
    Embedder,
    EmbeddingResult,
    EmbeddingUnavailableError,
    IndexedChunk,
    SearchIndex,
    SearchIndexOperationError,
)
from tenantchat.api.storage import ObjectStore, StorageKey
from tenantchat.api.store import KnowledgeStore
from tenantchat.core.errors import NotFoundError, ValidationError
from tenantchat.core.indexing import GenerationStatus, IndexGeneration
from tenantchat.core.knowledge import DocumentVersion, KnowledgeDocument
from tenantchat.core.lifecycle import VersionState

# The component identifiers `OBS-004` pins an answer's evidence to. Bumping one
# of these is a deliberate contract change: the detector treats a model swap as
# an integrity fault until a new generation records it.
PARSER_VERSION = "markdown-sections.v1"
CHUNKER_VERSION = "token-window.v1"

# The scan budget. Values are prototype bounds; `RAG-003` owns production
# limits, and the scanner here exists so a corrupt or hostile document fails
# the job with a safe code instead of poisoning a generation.
_MAX_DOCUMENT_BYTES = 10 * 1024 * 1024
_CHUNK_TOKENS = 650
_CHUNK_OVERLAP = 120

# Kinds the ingestion job may process. Drafts and deleted versions are refused
# by the domain's `version_for_indexing` gate: indexing either would put
# unreviewed or retracted content one approval away from being answerable.
_INDEXABLE_STATES = frozenset(
    {VersionState.APPROVED, VersionState.PUBLISHED, VersionState.SUPERSEDED}
)

_GENERATION_NAMESPACE = uuid.UUID("5f1d8c9e-2f6a-4c9b-9d51-3b2f0c0e1a23")


def generation_id_for(tenant_id: str, version_id: uuid.UUID) -> uuid.UUID:
    """The deterministic generation identifier for one tenant's version.

    Deterministic so a retried job reuses the same identifier: its partial
    chunks are found and removed under that identifier before the retry writes
    anything, which is what makes "retry without duplicate active chunks"
    structural rather than a convention.
    """
    return uuid.uuid5(_GENERATION_NAMESPACE, f"{tenant_id}:{version_id}")


class ParsedDocument:
    """The scanning-and-parsing output for one document.

    ``section`` is the last heading seen at the current text offset, which is
    the anchor `RAG-003` will replace with heading-hierarchy-aware locations.
    """

    def __init__(self, *, title: str, section: str, text: str) -> None:
        self.title = title
        self.section = section
        self.text = text


def scan_content(content: bytes) -> None:
    """Reject content the pipeline cannot safely process.

    Raises:
        ValidationError: the document exceeds the size budget, is not valid
            UTF-8, contains NUL bytes, or has no scannable text.
    """
    if len(content) > _MAX_DOCUMENT_BYTES:
        raise ValidationError(detail="document exceeds the size budget")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(detail="document is not valid UTF-8") from exc
    if "\x00" in text:
        raise ValidationError(detail="document contains NUL bytes")
    if not text.strip():
        raise ValidationError(detail="document has no text content")


def parse_markdown(content: bytes, *, title: str) -> ParsedDocument:
    """Parse Markdown into title, running section, and body text.

    The prototype parser: headings become section anchors, everything else is
    body text. `RAG-003` owns production parsing; the version identifier above
    pins this one.

    Raises:
        ValidationError: the content failed scanning or has no text.
    """
    scan_content(content)
    text = content.decode("utf-8")
    section = "General"
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            section = stripped.lstrip("#").strip() or section
            lines.append(stripped)
        elif stripped:
            lines.append(stripped)
    return ParsedDocument(title=title, section=section, text="\n".join(lines))


def chunk_text(text: str) -> list[str]:
    """Split one document's text into overlapping token windows.

    A token is whitespace-delimited, which is what makes the chunker
    deterministic without a model vocabulary. `RAG-003` replaces the tokenizer.
    """
    tokens = re.findall(r"\S+", text)
    step = max(1, _CHUNK_TOKENS - _CHUNK_OVERLAP)
    return [
        " ".join(tokens[start : start + _CHUNK_TOKENS])
        for start in range(0, len(tokens), step)
        if tokens[start : start + _CHUNK_TOKENS]
    ]


def ingestion_payload(version_id: uuid.UUID) -> dict[str, object]:
    """The job payload for one version. Identifiers only — never content."""
    return {"version_id": str(version_id)}


def ingestion_key(tenant_id: str, version_id: uuid.UUID) -> str:
    """The durable-job idempotency key, derived from the version identity."""
    return f"ingestion:{tenant_id}:{version_id}"


async def submit_ingestion(
    jobs: JobStore,
    *,
    tenant_id: str,
    version_id: uuid.UUID,
    max_attempts: int = 5,
) -> JobRecord:
    """Enqueue the durable ingestion job for one version.

    Idempotent: re-submitting the same version returns the existing job. This
    is the entry point `FEAT-001`'s approval and publish workflow calls.
    """
    return await jobs.enqueue(
        tenant_id,
        kind=JobKind.INGESTION,
        payload=ingestion_payload(version_id),
        idempotency_key=ingestion_key(tenant_id, version_id),
        max_attempts=max_attempts,
    )


class IngestionDependencies:
    """Everything one ingestion job needs, wired by the worker's composition."""

    def __init__(
        self,
        *,
        knowledge: KnowledgeStore,
        generations: IndexIntegrityStore,
        storage: ObjectStore,
        index: SearchIndex,
        embedder: Embedder,
    ) -> None:
        self.knowledge = knowledge
        self.generations = generations
        self.storage = storage
        self.index = index
        self.embedder = embedder


def ingestion_handler(dependencies: IngestionDependencies) -> JobHandler:
    """Build the durable ingestion job handler.

    The handler is idempotent under at-least-once delivery: a version whose
    generation is recorded complete is a no-op, so a worker restart after the
    commit point re-runs the job without duplicating active chunks.
    """

    async def handle(job: JobRecord) -> None:
        raw_version_id = job.payload.get("version_id")
        if not isinstance(raw_version_id, str):
            raise JobExecutionError("ingestion_version_invalid", retryable=False)
        try:
            version_id = uuid.UUID(raw_version_id)
        except ValueError as exc:
            raise JobExecutionError("ingestion_version_invalid", retryable=False) from exc

        await _ingest_version(dependencies, job.tenant_id, version_id)

    return handle


async def _ingest_version(
    dependencies: IngestionDependencies, tenant_id: str, version_id: uuid.UUID
) -> None:
    try:
        document = await dependencies.knowledge.document_for_version(tenant_id, version_id)
    except NotFoundError as exc:
        raise JobExecutionError("ingestion_version_missing", retryable=False) from exc
    try:
        version = document.version_for_indexing(version_id)
    except Exception as exc:
        # `version_for_indexing` refuses drafts and deleted versions. The job
        # raced the operator, or the version was never approved; retrying is
        # the right shape (an approval between attempts is what makes the retry
        # succeed) and the job dead-letters if approval never arrives.
        raise JobExecutionError("ingestion_version_not_indexable", retryable=True) from exc

    generation = await _prepared_generation(dependencies, version)
    if generation is None:
        # Already indexed by a complete generation: idempotent replay.
        return
    await _run_generation(dependencies, document, version, generation)


async def _prepared_generation(
    dependencies: IngestionDependencies, version: DocumentVersion
) -> IndexGeneration | None:
    """Return a generation to run, cleaning up any partial prior attempt.

    Returns ``None`` when the version is already indexed by a complete
    generation — the idempotent replay case.
    """
    tenant_id = version.tenant_id
    generation_id = generation_id_for(tenant_id, version.version_id)
    existing = await dependencies.generations.generation(tenant_id, version.version_id)

    if existing is not None:
        if existing.status is GenerationStatus.COMPLETE:
            return None
        # A partial or failed generation must not leave its chunks behind:
        # delete them before the retry writes, so a retry can never duplicate
        # active chunks.
        await dependencies.index.delete_generation_chunks(
            tenant_id=tenant_id, generation_id=generation_id
        )

    return IndexGeneration(
        generation_id=generation_id,
        tenant_id=tenant_id,
        document_id=version.document_id,
        version_id=version.version_id,
        parser_version=PARSER_VERSION,
        chunker_version=CHUNKER_VERSION,
        embedding_model="",  # filled when the embedder returns
        status=GenerationStatus.IN_PROGRESS,
        chunk_count=0,
        indexed_chunk_count=0,
        started_at=datetime.now(UTC),
    )


async def _run_generation(
    dependencies: IngestionDependencies,
    document: KnowledgeDocument,
    version: DocumentVersion,
    generation: IndexGeneration,
) -> None:
    tenant_id = version.tenant_id
    try:
        await dependencies.knowledge.record_indexing_started(tenant_id, version.version_id)
        await dependencies.generations.begin_generation(generation)

        content = await dependencies.storage.read(StorageKey.parse(version.storage_key))
        parsed = parse_markdown(content, title=document.title)
        chunks = chunk_text(parsed.text)
        generation = replace(generation, chunk_count=len(chunks))

        embedded = await dependencies.embedder.embed(
            [f"Title: {parsed.title}\nSection: {parsed.section}\n\n{chunk}" for chunk in chunks]
        )
        generation = replace(generation, embedding_model=embedded.model)

        indexed = await _write_chunks(
            dependencies, document, version, parsed, chunks, embedded, generation
        )
        if indexed != len(chunks):
            # The index accepted fewer chunks than the content produced. A
            # partial write must not be called success; the job fails retryably
            # and the next attempt's cleanup removes the partial chunks.
            raise JobExecutionError("ingestion_chunk_write_mismatch", retryable=True)

        complete = replace(
            generation,
            status=GenerationStatus.COMPLETE,
            indexed_chunk_count=indexed,
            completed_at=datetime.now(UTC),
        )
        await dependencies.generations.complete_generation(complete)
        await dependencies.knowledge.record_indexed(
            tenant_id, version.version_id, at=datetime.now(UTC)
        )
    except JobExecutionError:
        await _record_failure(dependencies, version, generation)
        raise
    except NotFoundError as exc:
        # The stored content is gone or the version was withdrawn mid-job.
        # Either can clear by itself, so the retry is worth taking.
        await _record_failure(dependencies, version, generation)
        raise JobExecutionError("ingestion_content_missing", retryable=True) from exc
    except SearchIndexOperationError as exc:
        await _record_failure(dependencies, version, generation)
        raise JobExecutionError("ingestion_index_unavailable", retryable=True) from exc
    except EmbeddingUnavailableError as exc:
        await _record_failure(dependencies, version, generation)
        raise JobExecutionError("ingestion_embedding_unavailable", retryable=True) from exc
    except ValidationError as exc:
        # A document that fails scanning is corrupt or hostile, not transient:
        # re-running it produces the same refusal.
        await _record_failure(dependencies, version, generation)
        raise JobExecutionError("ingestion_scan_rejected", retryable=False) from exc
    except Exception as exc:
        await _record_failure(dependencies, version, generation)
        raise JobExecutionError("ingestion_pipeline_failed", retryable=True) from exc


async def _write_chunks(
    dependencies: IngestionDependencies,
    document: KnowledgeDocument,
    version: DocumentVersion,
    parsed: ParsedDocument,
    chunks: list[str],
    embedded: EmbeddingResult,
    generation: IndexGeneration,
) -> int:
    """Write one generation's chunks, then retire older generations' chunks.

    Order matters: the new chunks are written active *before* the older
    generations are deactivated, so a superseded version is never retrievable
    while the current one is missing from the index.
    """
    written = await dependencies.index.index_chunks(
        [
            IndexedChunk(
                chunk_id=_chunk_id(version, generation, position),
                tenant_id=version.tenant_id,
                domain=document.domain.value,
                document_id=version.document_id,
                version_id=version.version_id,
                generation_id=generation.generation_id,
                title=parsed.title,
                section=parsed.section,
                text=chunk,
                embedding_model=embedded.model,
                embedding=embedded.vectors[position],
            )
            for position, chunk in enumerate(chunks)
        ]
    )
    await dependencies.index.deactivate_stale_chunks(
        tenant_id=version.tenant_id,
        document_id=version.document_id,
        keep_generation_id=generation.generation_id,
    )
    return written


def _chunk_id(version: DocumentVersion, generation: IndexGeneration, position: int) -> str:
    """The stable chunk identifier: generation-addressed and position-unique."""
    return f"{generation.generation_id}:{version.version_id}:{position:06d}"


async def _record_failure(
    dependencies: IngestionDependencies, version: DocumentVersion, generation: IndexGeneration
) -> None:
    # Failure recording must never mask the failure it reports, so each step
    # is isolated: a generation row that cannot be written still leaves the
    # version visibly failed.
    with contextlib.suppress(Exception):
        await dependencies.generations.fail_generation(
            replace(generation, status=GenerationStatus.FAILED, completed_at=datetime.now(UTC))
        )
    with contextlib.suppress(Exception):
        await dependencies.knowledge.record_index_failure(
            version.tenant_id, version.version_id, error_code="ingestion_failed"
        )
