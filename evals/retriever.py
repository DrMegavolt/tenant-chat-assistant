"""The retriever protocol the scoreboard evaluates, plus its two retrievers.

``RAG-004`` plugs the production hybrid retriever into :class:`Retriever`; the
scoreboard also scores :class:`LexicalOverlapRetriever`, a deterministic
baseline that keeps the scoreboard meaningful (a retriever that cannot beat
word-overlap is not worth tuning).

Both retrievers share the lexical vocabulary, stemming, and the
tenant/``active`` predicate with the production retrieval logic
(:mod:`tenantchat.api.retrieval`), so what the harness measures is the same
filter the index enforces. All scoring is deterministic: tokenization is
fixed, ties break on chunk id.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from evals.corpus import FixtureCorpus
from tenantchat.api.retrieval import (
    HybridRetrieverConfig,
    RetrievalFilters,
    chunk_is_retrievable,
    lexical_overlap,
    rank_chunks,
)
from tenantchat.api.search import ScriptedEmbedder
from tenantchat.core.text import query_words


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """One retrieved chunk and its deterministic relevance score."""

    chunk_id: str
    score: float


class Retriever(Protocol):
    """Something that returns the top ``k`` chunks for a tenant's query."""

    async def retrieve(self, query: str, *, tenant_id: str, k: int) -> Sequence[RetrievalResult]:
        """Rank the tenant's active chunks for one query."""
        ...


@dataclass(frozen=True, slots=True)
class RetrieverConfig:
    """The exact parameters a run used, so two runs are comparable."""

    name: str
    version: str
    k: int
    description: str
    parameters: Mapping[str, object] = field(default_factory=dict)


class LexicalOverlapRetriever:
    """Deterministic word-overlap baseline over the fixture corpus.

    Scores a chunk by the fraction of query words its text contains (with
    prefix stemming), filters to the tenant's active chunks, and ranks by
    ``(-score, chunk_id)``. The abstention threshold is separate: it is the
    scorer's decision boundary, not part of ranking.
    """

    def __init__(self, corpus: FixtureCorpus) -> None:
        self._corpus = corpus

    async def retrieve(self, query: str, *, tenant_id: str, k: int) -> Sequence[RetrievalResult]:
        query_terms = query_words(query)
        filters = RetrievalFilters(tenant_id=tenant_id)
        scored: list[tuple[float, str]] = []
        for chunk in self._corpus.chunks:
            if not chunk_is_retrievable(chunk, filters):
                continue
            score = lexical_overlap(query_terms, query_words(chunk.text))
            scored.append((score, chunk.chunk_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            RetrievalResult(chunk_id=chunk_id, score=score) for score, chunk_id in scored[:k]
        )


class HybridRetriever:
    """The `RAG-004` hybrid over the fixture corpus, scored by the shared logic.

    Embedding and scoring run through :func:`rank_chunks`, the same code path
    the production index adapter uses, so the harness measures the production
    retriever rather than a simulation of it. The calibrated abstention
    boundary travels with the config.
    """

    def __init__(self, corpus: FixtureCorpus, config: HybridRetrieverConfig) -> None:
        self._corpus = corpus
        self._config = config
        self._embedder = ScriptedEmbedder(model=corpus.embedding_model)

    @property
    def min_evidence_score(self) -> float:
        return self._config.min_evidence_score

    async def retrieve(self, query: str, *, tenant_id: str, k: int) -> Sequence[RetrievalResult]:
        ranked = await rank_chunks(
            embedder=self._embedder,
            chunks=self._corpus.chunks,
            query=query,
            filters=RetrievalFilters(tenant_id=tenant_id),
            config=self._config,
            k=k,
        )
        return tuple(RetrievalResult(chunk_id=item.chunk_id, score=item.score) for item in ranked)


def baseline_config(k: int) -> RetrieverConfig:
    """The v1 retriever configuration the scoreboard pins in its report."""
    return RetrieverConfig(
        name="lexical-overlap",
        version="v1",
        k=k,
        description="word-overlap baseline over active, tenant-filtered chunks",
    )


def hybrid_config(k: int, config: HybridRetrieverConfig) -> RetrieverConfig:
    """The hybrid's report configuration, pinning every tuned parameter."""
    return RetrieverConfig(
        name="hybrid",
        version=config.version,
        k=k,
        description="max-fused lexical+vector retrieval with reranking and calibrated abstention",
        parameters=config.parameters(),
    )
