"""Hybrid retrieval: fused scoring, reranking, abstention, and budgets (`RAG-004`).

The index holds only derived data (ADR-0003), so the retrieval predicate is the
subset the index can enforce — tenant, domain, version, and the ``active`` soft
delete. The rest of :meth:`KnowledgeDocument.retrievable_version` (audience,
visibility, effective window) is decided in the domain before a query is issued;
this module is the index-side filter that predicate names.

**Fusion.** A chunk's evidence score is ``max(lexical_overlap, vector_weight *
cosine)``: the vector signal may lift a chunk above its lexical score, never
reduce it, so the calibrated lexical floor survives a bad embedding. The
fixture's scripted embedder is hash-based, so its cosine is deterministic but
not semantic; weighting it higher than the abstention boundary breaks
abstention, which is why ``vector_weight`` must stay below
``min_evidence_score``.

**Abstention.** The boundary is derived, not chosen: it is the lowest score any
known-relevant chunk earns in calibration, so a query whose best score falls
below it provably has no evidence. The reranker reorders but never rescores —
its tie-break cannot cross the boundary.
"""

from __future__ import annotations

import itertools
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from tenantchat.api.parsing.tokens import DEFAULT_TOKEN_PROFILE, TokenProfile, count_tokens
from tenantchat.api.search import Embedder, IndexedChunk
from tenantchat.core.knowledge import KnowledgeDomain

HYBRID_RETRIEVER_VERSION = "v1"
RERANKER_NAME = "bigram-overlap"

_WORD = re.compile(r"[a-z0-9']+")
_MIN_STEM = 4
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "can",
        "do",
        "does",
        "for",
        "how",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "our",
        "out",
        "that",
        "the",
        "there",
        "to",
        "we",
        "what",
        "who",
        "you",
        "your",
    }
)


def tokenize(text: str) -> list[str]:
    """Lowercased non-stopword tokens in order: the shared lexical vocabulary."""
    return [word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS]


def query_words(text: str) -> frozenset[str]:
    """The unordered query vocabulary, for overlap scoring."""
    return frozenset(tokenize(text))


def matches(query_word: str, chunk_word: str) -> bool:
    """Whole-word match, or a shared stem of at least four characters.

    Prefix stemming models how real lexical search treats inflections
    (``cleaning``/``clean``, ``upgrades``/``upgrade``) without adding a
    dependency; the minimum length keeps unrelated short words from
    collapsing onto each other.
    """
    if query_word == chunk_word:
        return True
    return len(query_word) >= _MIN_STEM and chunk_word.startswith(query_word)


def lexical_overlap(query: frozenset[str], chunk: frozenset[str]) -> float:
    """The fraction of query words a chunk contains, with prefix stemming."""
    if not query:
        return 0.0
    return sum(1 for qw in query if any(matches(qw, cw) for cw in chunk)) / len(query)


def cosine_similarity(first: Sequence[float], second: Sequence[float]) -> float:
    """Cosine over equal-length vectors; zero for a zero vector."""
    dot = sum(a * b for a, b in zip(first, second, strict=True))
    norm_first = math.sqrt(sum(a * a for a in first))
    norm_second = math.sqrt(sum(b * b for b in second))
    if norm_first == 0.0 or norm_second == 0.0:
        return 0.0
    return dot / (norm_first * norm_second)


def _bigrams(tokens: Sequence[str]) -> frozenset[tuple[str, str]]:
    return frozenset(itertools.pairwise(tokens))


def bigram_overlap(query: frozenset[tuple[str, str]], chunk: frozenset[tuple[str, str]]) -> float:
    """Jaccard similarity of word-pair sets; zero when either side has none.

    Bigrams are the stricter, order-aware second pass the reranker uses, and
    the empty-set rule keeps a one-word query from matching every chunk.
    """
    if not query or not chunk:
        return 0.0
    return len(query & chunk) / len(query | chunk)


@dataclass(frozen=True, slots=True)
class RetrievalFilters:
    """Which index rows may answer: everything the index can enforce."""

    tenant_id: str
    domain: str | None = None
    version_ids: frozenset[uuid.UUID] | None = None

    def __post_init__(self) -> None:
        if self.domain is not None:
            object.__setattr__(self, "domain", KnowledgeDomain.parse(self.domain).value)


def chunk_is_retrievable(chunk: IndexedChunk, filters: RetrievalFilters) -> bool:
    """Whether the index may return this chunk for the filters.

    ``active`` is the soft delete: a superseded version's chunks are still
    present, they are simply no longer supposed to answer.
    """
    return (
        chunk.active
        and chunk.tenant_id == filters.tenant_id
        and (filters.domain is None or chunk.domain == filters.domain)
        and (filters.version_ids is None or chunk.version_id in filters.version_ids)
    )


@dataclass(frozen=True, slots=True)
class CalibrationRecord:
    """How the abstention boundary was derived from measured scores."""

    method: str
    sample_size: int
    min_evidence: float


@dataclass(frozen=True, slots=True)
class HybridRetrieverConfig:
    """The versioned retrieval parameters a run pins, so two runs are comparable.

    ``vector_weight`` is the largest weight at which pure-vector noise cannot
    cross ``min_evidence_score``; the bound ``vector_weight <
    min_evidence_score`` is what keeps abstention correct for a query with no
    lexical signal at all.
    """

    name: str = "hybrid"
    version: str = HYBRID_RETRIEVER_VERSION
    k: int = 5
    vector_weight: float = 0.4
    candidate_k: int = 100
    max_chunks_per_document: int = 2
    rerank: bool = True
    max_sources: int = 3
    max_context_tokens: int = 1500
    min_evidence_score: float = 0.5
    calibration: CalibrationRecord | None = None

    def __post_init__(self) -> None:
        if self.k < 1:
            raise ValueError(f"k {self.k} is not positive")
        if not 0 < self.vector_weight < 1:
            raise ValueError(f"vector_weight {self.vector_weight} is outside (0, 1)")
        if self.candidate_k < 1:
            raise ValueError(f"candidate_k {self.candidate_k} is not positive")
        if self.max_chunks_per_document < 1:
            raise ValueError(
                f"max_chunks_per_document {self.max_chunks_per_document} is not positive"
            )
        if self.max_sources < 1:
            raise ValueError(f"max_sources {self.max_sources} is not positive")
        if self.max_context_tokens < 1:
            raise ValueError(f"max_context_tokens {self.max_context_tokens} is not positive")
        if not 0 < self.min_evidence_score <= 1:
            raise ValueError(f"min_evidence_score {self.min_evidence_score} is outside (0, 1]")

    def parameters(self) -> dict[str, object]:
        """Every tunable, as a stable dict for the run manifest."""
        calibration = self.calibration
        return {
            "vector_weight": self.vector_weight,
            "candidate_k": self.candidate_k,
            "max_chunks_per_document": self.max_chunks_per_document,
            "rerank": self.rerank,
            "reranker": RERANKER_NAME if self.rerank else None,
            "max_sources": self.max_sources,
            "max_context_tokens": self.max_context_tokens,
            "min_evidence_score": self.min_evidence_score,
            "calibration": None
            if calibration is None
            else {
                "method": calibration.method,
                "sample_size": calibration.sample_size,
                "min_evidence": calibration.min_evidence,
            },
        }


@dataclass(frozen=True, slots=True)
class RankedChunk:
    """One chunk with its evidence score, in retrieval order."""

    chunk_id: str
    score: float


class EvidenceVerdict(StrEnum):
    """The caller-branchable result of the evidence check (`RAG-004`)."""

    SUFFICIENT = "sufficient"
    INSUFFICIENT = "insufficient"


def evidence_verdict(ranked: Sequence[RankedChunk], min_evidence_score: float) -> EvidenceVerdict:
    """Abstain when no result reaches the calibrated boundary."""
    if all(item.score < min_evidence_score for item in ranked):
        return EvidenceVerdict.INSUFFICIENT
    return EvidenceVerdict.SUFFICIENT


@dataclass(frozen=True, slots=True)
class ContextBudget:
    """Bounds on the context assembled from retrieved results."""

    max_sources: int
    max_context_tokens: int

    def __post_init__(self) -> None:
        if self.max_sources < 1:
            raise ValueError(f"max_sources {self.max_sources} is not positive")
        if self.max_context_tokens < 1:
            raise ValueError(f"max_context_tokens {self.max_context_tokens} is not positive")


def assemble_context(
    *,
    chunks_by_id: Mapping[str, IndexedChunk],
    ranked: Sequence[RankedChunk],
    budget: ContextBudget,
    profile: TokenProfile = DEFAULT_TOKEN_PROFILE,
) -> tuple[RankedChunk, ...]:
    """Take results in rank order until a budget would be exceeded.

    The first result is always taken: like a single token longer than the
    chunker's window, a lone chunk over budget is still the best evidence the
    query has. Token counts are the documented model-family estimates.
    """
    selected: list[RankedChunk] = []
    sources: set[uuid.UUID] = set()
    tokens = 0
    for result in ranked:
        chunk = chunks_by_id.get(result.chunk_id)
        if chunk is None:
            continue
        chunk_tokens = count_tokens(chunk.text, profile)
        exceeds_source_budget = (
            chunk.document_id not in sources and len(sources) >= budget.max_sources
        )
        too_long = tokens + chunk_tokens > budget.max_context_tokens
        if selected and (too_long or exceeds_source_budget):
            continue
        selected.append(result)
        tokens += chunk_tokens
        sources.add(chunk.document_id)
    return tuple(selected)


def evidence_score(lexical: float, vector: float, config: HybridRetrieverConfig) -> float:
    """Max-fusion: a confident embedder may lift a chunk, never sink it."""
    return max(lexical, config.vector_weight * vector)


@dataclass(frozen=True, slots=True)
class _ScoredCandidate:
    chunk: IndexedChunk
    evidence: float
    tokens: tuple[str, ...]


async def rank_chunks(
    *,
    embedder: Embedder,
    chunks: Sequence[IndexedChunk],
    query: str,
    filters: RetrievalFilters,
    config: HybridRetrieverConfig,
    k: int | None = None,
) -> tuple[RankedChunk, ...]:
    """Rank the retrievable chunks for one query, deduped and bounded to ``k``.

    ``k`` defaults to the config's; pass ``None`` for the full ranked pool
    (calibration needs every score). Deterministic: ties break on chunk id.
    """
    pool = [chunk for chunk in chunks if chunk_is_retrievable(chunk, filters)]
    if not pool:
        return ()
    embedded = await embedder.embed([query])
    query_vector = embedded.vectors[0]
    query_terms = query_words(query)
    query_bigrams = _bigrams(tokenize(query))

    scored: list[_ScoredCandidate] = []
    for chunk in pool:
        tokens = tuple(tokenize(chunk.text))
        lexical = lexical_overlap(query_terms, frozenset(tokens))
        vector = cosine_similarity(query_vector, chunk.embedding)
        scored.append(_ScoredCandidate(chunk, evidence_score(lexical, vector, config), tokens))
    scored.sort(key=lambda candidate: (-candidate.evidence, candidate.chunk.chunk_id))
    scored = scored[: config.candidate_k]

    if config.rerank:
        scored = sorted(
            scored,
            key=lambda candidate: (
                -candidate.evidence,
                -bigram_overlap(query_bigrams, _bigrams(candidate.tokens)),
                candidate.chunk.chunk_id,
            ),
        )

    selected: list[RankedChunk] = []
    per_document: dict[uuid.UUID, int] = {}
    depth = len(scored) if k is None else k
    for candidate in scored:
        if per_document.get(candidate.chunk.document_id, 0) >= config.max_chunks_per_document:
            continue
        selected.append(RankedChunk(chunk_id=candidate.chunk.chunk_id, score=candidate.evidence))
        per_document[candidate.chunk.document_id] = (
            per_document.get(candidate.chunk.document_id, 0) + 1
        )
        if len(selected) == depth:
            break
    return tuple(selected)


async def calibrate_min_evidence(
    *,
    embedder: Embedder,
    chunks: Sequence[IndexedChunk],
    relevant_sets: Sequence[tuple[str, RetrievalFilters, Sequence[str]]],
    config: HybridRetrieverConfig,
) -> CalibrationRecord:
    """Derive the abstention boundary from measured relevance scores.

    The boundary is the lowest score the *best* relevant chunk of any
    calibration query earns: a query whose whole result set scores below it
    has no evidence by calibration, not by fiat. Only known-relevant pairs
    are used, so abstention correctness stays an independent measurement.
    """
    min_evidence = math.inf
    sample_size = 0
    for query, filters, relevant_ids in relevant_sets:
        pool = [chunk for chunk in chunks if chunk_is_retrievable(chunk, filters)]
        if not pool:
            continue
        embedded = await embedder.embed([query])
        query_vector = embedded.vectors[0]
        query_terms = query_words(query)
        relevant_set = frozenset(relevant_ids)
        best_relevant: float | None = None
        for chunk in pool:
            lexical = lexical_overlap(query_terms, query_words(chunk.text))
            vector = cosine_similarity(query_vector, chunk.embedding)
            score = evidence_score(lexical, vector, config)
            if chunk.chunk_id in relevant_set:
                sample_size += 1
                best_relevant = score if best_relevant is None else max(best_relevant, score)
        if best_relevant is not None:
            min_evidence = min(min_evidence, best_relevant)
    if math.isinf(min_evidence):
        raise ValueError("calibration needs at least one query with a relevant chunk")
    return CalibrationRecord(
        method="min-of-query-best-relevant",
        sample_size=sample_size,
        min_evidence=min_evidence,
    )
