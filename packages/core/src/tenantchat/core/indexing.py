"""The ingestion lifecycle's recorded results and its index-integrity findings.

An **index generation** is the immutable outcome of one ingestion job for one
document version: which parser, chunker, and embedding model produced it, how
many chunks it stored, and how many of those were verified in the retrieval
index. `OBS-004` pins a retrieval replay to exactly these identifiers, so the
difference between "ingestion failed" and "retrieval failed" is a comparison
between a generation's record and the index's actual contents — the two can
never be told apart if only one of them is persisted.

A finding is the **content-free** statement of that comparison. Every finding
names the affected tenant-qualified source version and index generation and
carries bounded counts and identifiers in ``detail``; nothing on this type can
hold document text, because the same type is what an operator console
(`FEAT-001`) and an inference trace (`OBS-004`) read from operational
telemetry.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

# Bounded identifiers follow the job-error-code contract: lowercase, short,
# and safe in a URL, a log line, and a SQL literal.
_FAULT_CODE_RE = "^[a-z][a-z0-9_.-]{0,99}$"

# How long a published version may stay outside the retrieval index before the
# detector reports lag. Documented here, in the detector, and on the finding so
# the threshold and its reason travel together.
INDEX_LAG_THRESHOLD = timedelta(hours=24)


class GenerationStatus(StrEnum):
    """Where one ingestion job's recorded generation stands.

    :attr:`COMPLETE` is the worker's claim that the index holds the version's
    chunks; the detector verifies that claim against the index rather than
    trusting it, which is what makes "partial chunk indexing" a detectable
    fault instead of a silent one.
    """

    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


class IndexingFault(StrEnum):
    """The bounded set of index-integrity faults the detector can report.

    Codes are part of the public contract (`FEAT-001`, `OBS-004` branch on
    them) and must not change casually.

    - :attr:`MISSING_GENERATION` — published, indexed content has no recorded
      generation, or the recorded generation has no chunks in the index.
    - :attr:`PARTIAL_GENERATION` — the generation recorded fewer chunks than
      the version's parsed content produced, or the index holds fewer than the
      generation recorded.
    - :attr:`CHUNK_COUNT_MISMATCH` — stored chunk count disagrees with the
      index's active count for the version.
    - :attr:`EMBEDDING_MODEL_MISMATCH` — the index holds chunks embedded with a
      different model than the generation recorded.
    - :attr:`LAG` — published content has been waiting to be indexed longer
      than :data:`INDEX_LAG_THRESHOLD`.
    - :attr:`SUPERSEDED_RETRIEVABLE` — a superseded version still has active
      chunks in the index, so it remains retrievable.
    """

    MISSING_GENERATION = "index_missing_generation"
    PARTIAL_GENERATION = "index_partial_generation"
    CHUNK_COUNT_MISMATCH = "index_chunk_count_mismatch"
    EMBEDDING_MODEL_MISMATCH = "index_embedding_model_mismatch"
    LAG = "index_lag"
    SUPERSEDED_RETRIEVABLE = "index_superseded_retrievable"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class IndexGeneration:
    """One immutable attempt to index one document version.

    The ``generation_id`` is deterministic per ``(tenant, version)`` — see the
    ingestion handler — so a retried job reuses the same identifier, can find
    and clean up the partial chunks its earlier attempt wrote, and never leaves
    two generations of one version answerable at once.
    """

    generation_id: uuid.UUID
    tenant_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    parser_version: str
    chunker_version: str
    embedding_model: str
    status: GenerationStatus
    # Chunks the parsed content produced; stored before indexing starts so a
    # mismatch with the index is measurable even when the job died early.
    chunk_count: int
    # Chunks verified present in the retrieval index after writing.
    indexed_chunk_count: int
    started_at: datetime
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.chunk_count < 0:
            raise ValueError(f"chunk_count {self.chunk_count} is negative")
        if not 0 <= self.indexed_chunk_count <= self.chunk_count:
            raise ValueError(
                f"indexed_chunk_count {self.indexed_chunk_count} outside 0..{self.chunk_count}"
            )

    @property
    def is_finished(self) -> bool:
        """Whether this generation ran to a terminal state."""
        return self.status is not GenerationStatus.IN_PROGRESS


@dataclass(frozen=True, slots=True)
class IndexIntegrityFinding:
    """One content-free index-integrity fault for an operator to work.

    ``detail`` carries only bounded values — counts, model names, generation
    identifiers, thresholds — never document text, so the type itself cannot
    leak content into logs or the admin surface.
    """

    code: IndexingFault
    tenant_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    generation_id: uuid.UUID | None
    detected_at: datetime
    detail: Mapping[str, object] = field(default_factory=dict)

    def __str__(self) -> str:
        """Safe by default: the finding reads as its bounded code alone."""
        return self.code.value
