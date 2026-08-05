"""The index-generation and finding contracts `OBS-004` and `FEAT-001` read."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tenantchat.core.indexing import (
    INDEX_LAG_THRESHOLD,
    GenerationStatus,
    IndexGeneration,
    IndexingFault,
    IndexIntegrityFinding,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def generation(**overrides: Any) -> IndexGeneration:
    values: dict[str, Any] = {
        "generation_id": uuid.uuid4(),
        "tenant_id": "clearview",
        "document_id": uuid.uuid4(),
        "version_id": uuid.uuid4(),
        "parser_version": "markdown-sections.v1",
        "chunker_version": "token-window.v1",
        "embedding_model": "scripted-embedder.v1",
        "status": GenerationStatus.IN_PROGRESS,
        "chunk_count": 3,
        "indexed_chunk_count": 0,
        "started_at": NOW,
    }
    values.update(overrides)
    return IndexGeneration(**values)


def test_every_indexing_fault_code_is_a_bounded_safe_identifier() -> None:
    """The codes branch on in URLs, log lines, and SQL literals: lowercase and short."""
    for fault in IndexingFault:
        assert fault.value == fault.value.lower()
        assert len(fault.value) <= 40
        assert fault.value.replace("_", "").isalnum()


def test_the_lag_threshold_is_documented_and_positive() -> None:
    assert timedelta(0) < INDEX_LAG_THRESHOLD


def test_a_generation_rejects_impossible_counts() -> None:
    with pytest.raises(ValueError):
        generation(chunk_count=-1)
    with pytest.raises(ValueError):
        generation(chunk_count=2, indexed_chunk_count=3)
    with pytest.raises(ValueError):
        generation(chunk_count=2, indexed_chunk_count=-1)


def test_a_generation_is_finished_only_in_a_terminal_state() -> None:
    assert not generation(status=GenerationStatus.IN_PROGRESS).is_finished
    assert generation(status=GenerationStatus.COMPLETE).is_finished
    assert generation(status=GenerationStatus.FAILED).is_finished


def test_a_finding_carries_only_bounded_identifiers() -> None:
    """The finding type is the content-free contract: no field can hold document text."""
    finding = IndexIntegrityFinding(
        code=IndexingFault.CHUNK_COUNT_MISMATCH,
        tenant_id="clearview",
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation_id=uuid.uuid4(),
        detected_at=NOW,
        detail={"indexed": 2, "recorded": 5},
    )

    assert finding.code is IndexingFault.CHUNK_COUNT_MISMATCH
    assert str(finding) == "index_chunk_count_mismatch"
    assert "document text" not in str(finding)


def test_a_finding_requires_no_generation_for_missing_generation_faults() -> None:
    finding = IndexIntegrityFinding(
        code=IndexingFault.MISSING_GENERATION,
        tenant_id="clearview",
        document_id=uuid.uuid4(),
        version_id=uuid.uuid4(),
        generation_id=None,
        detected_at=NOW,
        detail={},
    )
    assert finding.generation_id is None


def test_the_generation_embeds_the_immutable_component_identifiers() -> None:
    """Parser, chunker, and model versions travel with the generation, so OBS-004
    can attribute an answer to the exact pipeline that produced it."""
    recorded = generation(
        parser_version="markdown-sections.v1",
        chunker_version="token-window.v1",
        embedding_model="Qwen/Qwen3-Embedding-0.6B",
    )
    assert recorded.parser_version == "markdown-sections.v1"
    assert recorded.chunker_version == "token-window.v1"
    assert recorded.embedding_model == "Qwen/Qwen3-Embedding-0.6B"
