"""The API's evidence port: hybrid retrieval resolved into citable passages.

`RAG-005` asks the graph to ground tool-less answers in approved knowledge and
cite what it used. This module is the service side of that contract: it
implements :class:`~tenantchat.core.ports.EvidenceSource` over the retrieval
index, the embedding provider, and the knowledge system of record.

The pipeline mirrors the `RAG-004` calibration shape:

1. The tenant's active chunks are read back from the index — the same derived
   data the ingestion job wrote.
2. :func:`rank_chunks` scores and ranks them with the production hybrid
   (lexical + vector fusion, bigram reranking), deterministically.
3. The evidence verdict is re-derived over what survived as evidence, so the
   abstention boundary is applied to what the model would actually be given.
4. Each passage is checked against the *domain* retrievability predicate
   (:meth:`KnowledgeDocument.retrievable_version`): the index cannot see
   publication, expiry, or visibility, so a chunk that is active in the index
   but whose version is no longer answerable is dropped here. This is the
   step that makes a citation "stale" impossible by construction.
5. Citation metadata (source name, revision, effective window) comes from
   :meth:`KnowledgeDocument.public_view`, the same curated projection the rest
   of the API uses — never from the raw domain record.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import datetime

from tenantchat.api.retrieval import (
    RERANKER_NAME,
    ContextBudget,
    EvidenceVerdict,
    HybridRetrieverConfig,
    RankedChunk,
    RetrievalFilters,
    assemble_context,
    evidence_verdict,
    rank_chunks,
)
from tenantchat.api.search import Embedder, IndexedChunk, SearchIndex
from tenantchat.api.store import KnowledgeStore
from tenantchat.api.visitor import utc_now
from tenantchat.core.errors import NotFoundError, ValidationError
from tenantchat.core.knowledge import (
    DocumentVersion,
    KnowledgeDocument,
    RetrievalAudience,
    RetrievalContext,
)
from tenantchat.core.ports import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceUnavailableError,
)

logger = logging.getLogger(__name__)


class RetrievalEvidenceSource:
    """Serves one tenant's approved knowledge as evidence for one query.

    Raises:
        EvidenceUnavailableError: the index or the embedding provider failed,
            which the graph treats as insufficient evidence — a broken index
            must make the assistant abstain, never answer from nothing.
    """

    def __init__(
        self,
        *,
        index: SearchIndex,
        embedder: Embedder,
        knowledge: KnowledgeStore,
        config: HybridRetrieverConfig,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._knowledge = knowledge
        self._config = config
        self._now = now

    async def retrieve(self, *, tenant_id: str, query: str) -> EvidenceBundle:
        try:
            pool = await self._index.active_chunks(tenant_id=tenant_id)
            ranked = await rank_chunks(
                embedder=self._embedder,
                chunks=pool,
                query=query,
                filters=RetrievalFilters(tenant_id=tenant_id),
                config=self._config,
                k=self._config.k,
            )
        except EvidenceUnavailableError:
            raise
        except Exception as error:
            # The retriever and the index fail in their own ways; to the graph
            # every failure is the same verdict: nothing to ground an answer.
            raise EvidenceUnavailableError("retrieval could not run for this turn") from error

        admitted = assemble_context(
            chunks_by_id={chunk.chunk_id: chunk for chunk in pool},
            ranked=ranked,
            budget=ContextBudget(
                max_sources=self._config.max_sources,
                max_context_tokens=self._config.max_context_tokens,
            ),
        )
        items = tuple(await self._items(tenant_id, pool, admitted))

        # The verdict is applied to what actually became evidence, not to the
        # retrieved pool: a query whose only relevant chunks were withdrawn
        # between indexing and now must abstain, not answer from nothing.
        sufficient = (
            evidence_verdict(
                tuple(RankedChunk(chunk_id=item.source_id, score=item.score) for item in items),
                self._config.min_evidence_score,
            )
            is EvidenceVerdict.SUFFICIENT
        )
        filters = RetrievalFilters(tenant_id=tenant_id)
        return EvidenceBundle(
            items=items,
            sufficient=sufficient,
            retriever_version=self._config.version,
            reranker=RERANKER_NAME if self._config.rerank else None,
            min_evidence_score=self._config.min_evidence_score,
            embedding_model=pool[0].embedding_model if pool else "",
            generation_id=pool[0].generation_id if pool else None,
            retriever_parameters=self._config.parameters(),
            filters={
                "tenant_id": filters.tenant_id,
                "domain": filters.domain,
                "version_ids": sorted(str(version) for version in filters.version_ids or ()),
            },
            budget={
                "max_sources": self._config.max_sources,
                "max_context_tokens": self._config.max_context_tokens,
            },
        )

    async def _items(
        self,
        tenant_id: str,
        pool: Sequence[IndexedChunk],
        admitted: Sequence[RankedChunk],
    ) -> tuple[EvidenceItem, ...]:
        by_id = {chunk.chunk_id: chunk for chunk in pool}
        items: list[EvidenceItem] = []
        for result in admitted:
            chunk = by_id[result.chunk_id]
            item = await self._resolve(tenant_id, chunk, result.score)
            if item is not None:
                items.append(item)
        return tuple(items)

    async def _resolve(
        self, tenant_id: str, chunk: IndexedChunk, score: float
    ) -> EvidenceItem | None:
        """One admitted chunk as evidence, or ``None`` when it is not answerable.

        ``None`` covers every withdrawal — superseded version, expired window,
        disabled source, deleted document, or an index row whose document no
        longer exists (drift the integrity detector owns). The caller must not
        distinguish, because a citation to any of them is a broken promise.
        """
        try:
            document = await self._knowledge.document_for_version(tenant_id, chunk.version_id)
        except NotFoundError:
            return None
        retrievable = self._retrievable(document)
        if retrievable is None or retrievable.version_id != chunk.version_id:
            return None
        try:
            view = document.public_view(retrievable)
        except ValidationError:
            return None
        return EvidenceItem(
            source_id=chunk.chunk_id,
            title=chunk.title,
            source_name=view.source_name,
            location=chunk.section,
            content=chunk.text,
            document_id=chunk.document_id,
            version_id=chunk.version_id,
            generation_id=chunk.generation_id,
            embedding_model=chunk.embedding_model,
            score=score,
            revision=view.revision,
            effective_at=view.effective_at,
        )

    def _retrievable(self, document: KnowledgeDocument) -> DocumentVersion | None:
        """The version a visitor may be told right now, or ``None``."""
        return document.retrievable_version(
            RetrievalContext(
                tenant_id=document.tenant_id,
                domain=document.domain,
                audience=RetrievalAudience.VISITOR,
                moment=self._now(),
            )
        )
