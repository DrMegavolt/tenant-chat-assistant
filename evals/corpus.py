"""Load the golden fixture corpus and build the hermetic retrieval index.

The corpus is synthetic but shaped after the two seed tenants' real domains
(``services/api/src/tenantchat/api/registry.py``): the same services, ZIP
ranges, hours, and pricing posture (Apex quotes no prices; Clearview has
approved prices). Chunks carry the same fields as the production index
documents (``tenantchat.api.search.IndexedChunk``), including the ``active``
soft delete — which is what makes the stale-document case meaningful: a
superseded version's chunks are still present, they are simply no longer
supposed to answer.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from tenantchat.api.search import IndexedChunk, ScriptedEmbedder

_FIXTURES = Path(__file__).parent / "fixtures"
_NAMESPACE = uuid.UUID("6f1d3b3e-8e2e-4a3e-9f1e-0e3e4e5e6e7e")


@dataclass(frozen=True, slots=True)
class FixtureChunk:
    """One fixture chunk as authored: id, location, text, and activity."""

    id: str
    section: str
    text: str
    active: bool


@dataclass(frozen=True, slots=True)
class FixtureDocument:
    """One fixture document with stable, server-style identifiers.

    Identifiers are derived from the authoring id with ``uuid5`` so two runs
    index the identical ``chunk_id``/``document_id``/``generation_id`` values;
    ``generation_id`` is per (tenant, document, version), exactly as the
    ingestion worker derives it.
    """

    id: str
    tenant_id: str
    domain: str
    title: str
    version: str
    active: bool
    chunks: tuple[FixtureChunk, ...]

    def document_id(self) -> uuid.UUID:
        return _uuid(f"{self.tenant_id}/{self.id}")

    def version_id(self) -> uuid.UUID:
        return _uuid(f"{self.tenant_id}/{self.id}/{self.version}")

    def generation_id(self) -> uuid.UUID:
        return _uuid(f"{self.tenant_id}/{self.id}/{self.version}")


class FixtureCorpus:
    """The loaded corpus and the deterministic index entries built from it.

    Embeddings come from the same :class:`ScriptedEmbedder` the hermetic
    suites use, so any future vector retriever evaluated here reproduces the
    vectors a real embedding model would have produced deterministically.
    """

    def __init__(
        self, documents: tuple[FixtureDocument, ...], chunks: tuple[IndexedChunk, ...]
    ) -> None:
        self.documents = documents
        self.chunks = chunks
        self.embedding_model = chunks[0].embedding_model if chunks else ""

    @classmethod
    async def load(cls, path: Path | None = None) -> FixtureCorpus:
        """Load ``corpus.json`` and embed every chunk deterministically."""
        raw = json.loads((path or _FIXTURES / "corpus.json").read_text())
        documents = tuple(
            FixtureDocument(
                id=str(item["id"]),
                tenant_id=str(item["tenant_id"]),
                domain=str(item["domain"]),
                title=str(item["title"]),
                version=str(item["version"]),
                active=bool(item["active"]),
                chunks=tuple(
                    FixtureChunk(
                        id=str(chunk["id"]),
                        section=str(chunk["section"]),
                        text=str(chunk["text"]),
                        active=bool(chunk.get("active", True)),
                    )
                    for chunk in item["chunks"]
                ),
            )
            for item in raw["documents"]
        )
        embedder = ScriptedEmbedder(model=str(raw["embedding_model"]))
        chunks: list[IndexedChunk] = []
        for document in documents:
            texts = [chunk.text for chunk in document.chunks]
            result = await embedder.embed(texts)
            for chunk, vector in zip(document.chunks, result.vectors, strict=True):
                chunks.append(
                    IndexedChunk(
                        chunk_id=chunk.id,
                        tenant_id=document.tenant_id,
                        domain=document.domain,
                        document_id=document.document_id(),
                        version_id=document.version_id(),
                        generation_id=document.generation_id(),
                        title=document.title,
                        section=chunk.section,
                        text=chunk.text,
                        embedding_model=result.model,
                        embedding=vector,
                        active=chunk.active and document.active,
                    )
                )
        return cls(documents, tuple(chunks))

    def chunk_tenant(self, chunk_id: str) -> str | None:
        """The tenant that owns a chunk id, for cross-tenant leak checks."""
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk.tenant_id
        return None

    def chunk_text(self, chunk_id: str) -> str | None:
        """The chunk's text, for citation validity checks."""
        for chunk in self.chunks:
            if chunk.chunk_id == chunk_id:
                return chunk.text
        return None


def _uuid(value: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, value)


def load_corpus() -> FixtureCorpus:
    """Synchronous entry point for tests and the runner."""
    return asyncio.run(FixtureCorpus.load())
