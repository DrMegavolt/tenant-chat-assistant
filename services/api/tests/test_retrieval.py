"""RAG-004 unit proofs: hybrid scoring, reranking, abstention, and budgets.

These pin the properties the fixture scoreboard relies on: filters the index
can enforce, max-fusion evidence that can never sink a chunk below its lexical
score, a derived abstention boundary that distractor scores cannot move, and
context budgets that bound what reaches the model.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence

import pytest

from tenantchat.api.evidence import RetrievalEvidenceSource
from tenantchat.api.parsing.tokens import count_tokens
from tenantchat.api.retrieval import (
    RERANKER_NAME,
    CalibrationRecord,
    ContextBudget,
    EvidenceVerdict,
    HybridRetrieverConfig,
    RankedChunk,
    RetrievalFilters,
    assemble_context,
    bigram_overlap,
    calibrate_min_evidence,
    chunk_is_retrievable,
    cosine_similarity,
    evidence_score,
    evidence_verdict,
    lexical_overlap,
    rank_chunks,
)
from tenantchat.api.search import (
    Embedder,
    EmbeddingResult,
    EmbeddingUnavailableError,
    IndexedChunk,
    InMemorySearchIndex,
)
from tenantchat.api.store import InMemoryKnowledgeStore

QUERY_VECTOR = (1.0, 0.0, 0.0, 0.0)


class _FixedEmbedder:
    """Embedder that answers every text with the same vector, for control."""

    def __init__(self, vector: tuple[float, ...] = QUERY_VECTOR) -> None:
        self._vector = vector

    async def ready(self) -> None:
        return None

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors = [self._vector] * len(texts)
        return EmbeddingResult(model="test", dimensions=len(self._vector), vectors=vectors)


def _chunk(
    chunk_id: str,
    text: str,
    *,
    tenant_id: str = "apex",
    domain: str = "hvac",
    document_id: uuid.UUID | None = None,
    version_id: uuid.UUID | None = None,
    generation_id: uuid.UUID | None = None,
    embedding: tuple[float, ...] = QUERY_VECTOR,
    active: bool = True,
) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=chunk_id,
        tenant_id=tenant_id,
        domain=domain,
        document_id=document_id or uuid.uuid4(),
        version_id=version_id or uuid.uuid4(),
        generation_id=generation_id or uuid.uuid4(),
        title="",
        section="",
        text=text,
        embedding_model="scripted-embedder.v1",
        embedding=embedding,
        active=active,
    )


def _config(
    *,
    k: int = 5,
    vector_weight: float = 0.4,
    candidate_k: int = 100,
    max_chunks_per_document: int = 2,
    rerank: bool = True,
    max_sources: int = 3,
    max_context_tokens: int = 1500,
    min_evidence_score: float = 0.5,
    calibration: CalibrationRecord | None = None,
) -> HybridRetrieverConfig:
    return HybridRetrieverConfig(
        k=k,
        vector_weight=vector_weight,
        candidate_k=candidate_k,
        max_chunks_per_document=max_chunks_per_document,
        rerank=rerank,
        max_sources=max_sources,
        max_context_tokens=max_context_tokens,
        min_evidence_score=min_evidence_score,
        calibration=calibration,
    )


def test_retained_generation_read_includes_superseded_inactive_chunks() -> None:
    index = InMemorySearchIndex()
    generation = uuid.uuid4()
    document = uuid.uuid4()
    chunk = _chunk("old", "historical evidence", document_id=document, generation_id=generation)
    asyncio.run(index.index_chunks([chunk]))
    asyncio.run(
        index.deactivate_stale_chunks(
            tenant_id="apex",
            document_id=document,
            keep_generation_id=uuid.uuid4(),
        )
    )

    assert (
        asyncio.run(
            index.has_active_chunks_for_generation(tenant_id="apex", generation_id=generation)
        )
        is False
    )
    retained = asyncio.run(index.generation_chunks(tenant_id="apex", generation_id=generation))
    assert [item.chunk_id for item in retained] == ["old"]
    assert retained[0].active is False


def _rank(
    *,
    embedder: Embedder,
    chunks: Sequence[IndexedChunk],
    query: str,
    filters: RetrievalFilters,
    config: HybridRetrieverConfig,
    k: int | None = None,
) -> tuple[RankedChunk, ...]:
    return asyncio.run(
        rank_chunks(
            embedder=embedder, chunks=chunks, query=query, filters=filters, config=config, k=k
        )
    )


def _calibrate(
    *,
    embedder: Embedder,
    chunks: Sequence[IndexedChunk],
    relevant_sets: Sequence[tuple[str, RetrievalFilters, Sequence[str]]],
    config: HybridRetrieverConfig,
) -> CalibrationRecord:
    return asyncio.run(
        calibrate_min_evidence(
            embedder=embedder, chunks=chunks, relevant_sets=relevant_sets, config=config
        )
    )


class TestChunkIsRetrievable:
    def test_scopes_to_the_tenant(self) -> None:
        chunk = _chunk("a", "text", tenant_id="apex")
        assert chunk_is_retrievable(chunk, RetrievalFilters(tenant_id="apex"))
        assert not chunk_is_retrievable(chunk, RetrievalFilters(tenant_id="clearview"))

    def test_excludes_inactive_chunks(self) -> None:
        chunk = _chunk("a", "text", active=False)
        assert not chunk_is_retrievable(chunk, RetrievalFilters(tenant_id="apex"))

    def test_domain_is_a_normalized_slug(self) -> None:
        chunk = _chunk("a", "text", domain="hvac")
        filters = RetrievalFilters(tenant_id="apex", domain=" HVAC ")
        assert chunk_is_retrievable(chunk, filters)
        other = RetrievalFilters(tenant_id="apex", domain="plumbing")
        assert not chunk_is_retrievable(chunk, other)

    def test_version_ids_restrict_the_generation(self) -> None:
        kept, dropped = uuid.uuid4(), uuid.uuid4()
        chunks = [
            _chunk("kept", "text", version_id=kept),
            _chunk("dropped", "text", version_id=dropped),
        ]
        filters = RetrievalFilters(tenant_id="apex", version_ids=frozenset({kept}))
        retrievable = [c.chunk_id for c in chunks if chunk_is_retrievable(c, filters)]
        assert retrievable == ["kept"]


class TestScoring:
    def test_lexical_overlap_is_the_fraction_of_query_words_matched(self) -> None:
        query = frozenset({"faucet", "repair", "quote"})
        chunk = frozenset({"faucet", "repairing", "costs"})
        assert lexical_overlap(query, chunk) == 2 / 3

    def test_matching_stems_in_the_chunk_longer_direction(self) -> None:
        assert lexical_overlap(frozenset({"clean"}), frozenset({"cleaning"})) == 1.0
        assert lexical_overlap(frozenset({"cleaning"}), frozenset({"clean"})) == 0.0

    def test_bigram_overlap_is_order_aware_jaccard(self) -> None:
        query = frozenset({("clean", "screens")})
        assert bigram_overlap(query, frozenset({("clean", "screens")})) == 1.0
        assert bigram_overlap(query, frozenset({("screens", "clean")})) == 0.0

    def test_bigram_overlap_with_an_empty_side_is_zero(self) -> None:
        assert bigram_overlap(frozenset(), frozenset({("a", "b")})) == 0.0

    def test_evidence_is_max_fusion(self) -> None:
        config = _config(vector_weight=0.4)
        assert evidence_score(0.5, 1.0, config) == 0.5
        assert evidence_score(0.2, 1.0, config) == 0.4
        assert evidence_score(0.2, 0.1, config) == 0.2
        assert evidence_score(0.2, -1.0, config) == 0.2

    def test_cosine_is_zero_for_a_zero_vector(self) -> None:
        assert cosine_similarity((0.0, 0.0), (1.0, 1.0)) == 0.0


class TestEvidenceVerdict:
    def test_abstains_when_every_score_is_below_the_boundary(self) -> None:
        verdict = evidence_verdict([RankedChunk("a", 0.4), RankedChunk("b", 0.2)], 0.5)
        assert verdict is EvidenceVerdict.INSUFFICIENT

    def test_answers_when_any_score_reaches_the_boundary(self) -> None:
        verdict = evidence_verdict([RankedChunk("a", 0.4), RankedChunk("b", 0.5)], 0.5)
        assert verdict is EvidenceVerdict.SUFFICIENT

    def test_abstains_on_an_empty_result_set(self) -> None:
        assert evidence_verdict([], 0.5) is EvidenceVerdict.INSUFFICIENT


class TestRankChunks:
    def test_never_returns_foreign_tenant_or_inactive_chunks(self) -> None:
        chunks = [
            _chunk("local", "faucet repair service"),
            _chunk("foreign", "faucet repair service", tenant_id="clearview"),
            _chunk("inactive", "faucet repair service", active=False),
        ]
        ranked = _rank(
            embedder=_FixedEmbedder(),
            chunks=chunks,
            query="faucet repair",
            filters=RetrievalFilters(tenant_id="apex"),
            config=_config(),
            k=10,
        )
        assert [item.chunk_id for item in ranked] == ["local"]

    def test_dedupes_to_max_chunks_per_document(self) -> None:
        document = uuid.uuid4()
        chunks = [
            _chunk("d-1", "faucet repair", document_id=document),
            _chunk("d-2", "faucet repair", document_id=document),
            _chunk("d-3", "faucet repair", document_id=document),
            _chunk("other", "faucet repair"),
        ]
        ranked = _rank(
            embedder=_FixedEmbedder(),
            chunks=chunks,
            query="faucet repair",
            filters=RetrievalFilters(tenant_id="apex"),
            config=_config(k=3),
        )
        ids = [item.chunk_id for item in ranked]
        assert ids == ["d-1", "d-2", "other"]

    def test_rerank_breaks_evidence_ties_on_bigram_overlap(self) -> None:
        query = "clean screens"
        chunks = [
            _chunk("chunk-a", "cleaning screens"),
            _chunk("chunk-b", "clean screens twice"),
        ]
        ranked = _rank(
            embedder=_FixedEmbedder(),
            chunks=chunks,
            query=query,
            filters=RetrievalFilters(tenant_id="apex"),
            config=_config(rerank=True, k=2),
        )
        assert [item.chunk_id for item in ranked] == ["chunk-b", "chunk-a"]

    def test_without_rerank_ties_break_on_chunk_id(self) -> None:
        chunks = [_chunk("chunk-a", "cleaning screens"), _chunk("chunk-b", "clean screens twice")]
        ranked = _rank(
            embedder=_FixedEmbedder(),
            chunks=chunks,
            query="clean screens",
            filters=RetrievalFilters(tenant_id="apex"),
            config=_config(rerank=False, k=2),
        )
        assert [item.chunk_id for item in ranked] == ["chunk-a", "chunk-b"]

    def test_vector_signal_can_lift_a_lexically_empty_chunk(self) -> None:
        chunks = [_chunk("winter", "winter storm season")]
        ranked = _rank(
            embedder=_FixedEmbedder(),
            chunks=chunks,
            query="faucet repair",
            filters=RetrievalFilters(tenant_id="apex"),
            config=_config(vector_weight=0.4, k=1),
        )
        assert ranked == (RankedChunk(chunk_id="winter", score=0.4),)

    def test_pure_vector_noise_stays_below_the_boundary(self) -> None:
        chunks = [_chunk("noise", "winter storm season")]
        ranked = _rank(
            embedder=_FixedEmbedder(),
            chunks=chunks,
            query="faucet repair",
            filters=RetrievalFilters(tenant_id="apex"),
            config=_config(vector_weight=0.4, k=1),
        )
        assert evidence_verdict(ranked, 0.5) is EvidenceVerdict.INSUFFICIENT

    def test_candidate_k_bounds_the_pool_before_dedup(self) -> None:
        chunks = [_chunk(f"c-{i}", "faucet repair") for i in range(5)]
        ranked = _rank(
            embedder=_FixedEmbedder(),
            chunks=chunks,
            query="faucet repair",
            filters=RetrievalFilters(tenant_id="apex"),
            config=_config(candidate_k=2, k=5),
        )
        assert len(ranked) == 2


class TestAssembleContext:
    def test_first_result_is_always_taken_even_over_budget(self) -> None:
        chunk = _chunk("only", "x" * 200)
        selected = assemble_context(
            chunks_by_id={"only": chunk},
            ranked=[RankedChunk("only", 1.0)],
            budget=ContextBudget(max_sources=1, max_context_tokens=10),
        )
        assert [item.chunk_id for item in selected] == ["only"]

    def test_token_budget_stops_additional_chunks(self) -> None:
        chunks = {
            "a": _chunk("a", "abc"),
            "b": _chunk("b", "abc abc abc abc"),
            "c": _chunk("c", "x" * 20),
        }
        ranked = [RankedChunk(cid, 1.0) for cid in ("a", "b", "c")]
        selected = assemble_context(
            chunks_by_id=chunks,
            ranked=ranked,
            budget=ContextBudget(max_sources=3, max_context_tokens=6),
        )
        assert [item.chunk_id for item in selected] == ["a", "b"]

    def test_source_budget_skips_new_documents(self) -> None:
        first = _chunk("a", "abc")
        same_document = _chunk("b", "abc abc abc abc", document_id=first.document_id)
        chunks = {"a": first, "b": same_document, "c": _chunk("c", "abc")}
        ranked = [RankedChunk(cid, 1.0) for cid in ("a", "b", "c")]
        selected = assemble_context(
            chunks_by_id=chunks,
            ranked=ranked,
            budget=ContextBudget(max_sources=1, max_context_tokens=100),
        )
        assert [item.chunk_id for item in selected] == ["a", "b"]

    def test_budget_units_are_documented_token_estimates(self) -> None:
        chunk = _chunk("a", "four words here")
        assert count_tokens(chunk.text) == 4


ORTHOGONAL_EMBEDDER = _FixedEmbedder(vector=(0.0, 1.0, 0.0, 0.0))


class TestCalibrateMinEvidence:
    def test_boundary_is_the_min_of_best_relevant_scores(self) -> None:
        chunks = [
            _chunk("c1", "faucet repair service"),
            _chunk("c2", "winter storm season"),
            _chunk("d1", "leak detection service"),
            _chunk("d2", "pipe replacement"),
        ]
        record = _calibrate(
            embedder=ORTHOGONAL_EMBEDDER,
            chunks=chunks,
            relevant_sets=[
                ("faucet repair", RetrievalFilters(tenant_id="apex"), ("c1",)),
                ("faucet leak fix", RetrievalFilters(tenant_id="apex"), ("c1", "d1")),
            ],
            config=_config(),
        )
        assert record.min_evidence == 1 / 3
        assert record.sample_size == 3
        assert record.method == "min-of-query-best-relevant"

    def test_distractor_scores_never_move_the_boundary(self) -> None:
        chunks = [
            _chunk("gold", "faucet repair service"),
            _chunk("noise", "faucet leak fix leak"),
        ]
        record = _calibrate(
            embedder=ORTHOGONAL_EMBEDDER,
            chunks=chunks,
            relevant_sets=[("faucet leak fix", RetrievalFilters(tenant_id="apex"), ("gold",))],
            config=_config(),
        )
        assert record.min_evidence == 1 / 3

    def test_requires_at_least_one_relevant_pair(self) -> None:
        chunks = [_chunk("c1", "faucet repair service")]
        with pytest.raises(
            ValueError, match="calibration needs at least one query with a relevant chunk"
        ):
            _calibrate(
                embedder=_FixedEmbedder(),
                chunks=chunks,
                relevant_sets=[("faucet repair", RetrievalFilters(tenant_id="apex"), ())],
                config=_config(),
            )


class TestConfigAndManifest:
    def test_parameters_are_stable_and_versioned(self) -> None:
        first = _config().parameters()
        second = _config().parameters()
        assert first == second
        assert first["reranker"] == RERANKER_NAME
        assert first["calibration"] is None

    def test_calibration_lands_in_the_manifest(self) -> None:
        record = CalibrationRecord(
            method="min-of-query-best-relevant", sample_size=2, min_evidence=0.25
        )
        parameters = _config(calibration=record).parameters()
        assert parameters["calibration"] == {
            "method": "min-of-query-best-relevant",
            "sample_size": 2,
            "min_evidence": 0.25,
        }

    def test_disabling_rerank_reports_no_reranker(self) -> None:
        assert _config(rerank=False).parameters()["reranker"] is None

    @pytest.mark.parametrize(
        "invalid",
        [
            pytest.param(lambda: HybridRetrieverConfig(k=0), id="k-zero"),
            pytest.param(lambda: HybridRetrieverConfig(vector_weight=0.0), id="vector-weight-zero"),
            pytest.param(lambda: HybridRetrieverConfig(vector_weight=1.0), id="vector-weight-one"),
            pytest.param(lambda: HybridRetrieverConfig(candidate_k=0), id="candidate-k-zero"),
            pytest.param(
                lambda: HybridRetrieverConfig(max_chunks_per_document=0),
                id="max-chunks-per-document-zero",
            ),
            pytest.param(lambda: HybridRetrieverConfig(max_sources=0), id="max-sources-zero"),
            pytest.param(
                lambda: HybridRetrieverConfig(max_context_tokens=0), id="max-context-tokens-zero"
            ),
            pytest.param(
                lambda: HybridRetrieverConfig(min_evidence_score=0.0), id="min-evidence-zero"
            ),
            pytest.param(
                lambda: HybridRetrieverConfig(min_evidence_score=1.5), id="min-evidence-over-one"
            ),
        ],
    )
    def test_config_rejects_invalid_parameters(
        self, invalid: Callable[[], HybridRetrieverConfig]
    ) -> None:
        with pytest.raises(ValueError):
            invalid()


class _NotReadyEmbedder:
    """An embedder whose provider is still loading; the readiness probe fails."""

    def __init__(self) -> None:
        self.probed = 0

    async def ready(self) -> None:
        self.probed += 1
        raise EmbeddingUnavailableError("embedding provider is not ready")

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors = [(1.0, 0.0, 0.0, 0.0)] * len(texts)
        return EmbeddingResult(model="scripted-embedder.v1", dimensions=4, vectors=vectors)


class _ReadyEmbedder(_NotReadyEmbedder):
    async def ready(self) -> None:
        self.probed += 1


class TestReadinessContract:
    """R-25: readiness must prove both retrieval dependencies, not just one.

    The probe used to test ``getattr(embedder, "ready", None)`` — an attribute
    the real client never had — so a deployment whose embedding service was
    down or still loading reported ready and served degraded answers."""

    @staticmethod
    def _source(embedder: Embedder) -> RetrievalEvidenceSource:
        return RetrievalEvidenceSource(
            index=InMemorySearchIndex(),
            embedder=embedder,
            knowledge=InMemoryKnowledgeStore(),
            config=HybridRetrieverConfig(),
        )

    def test_readiness_probes_the_embedder(self) -> None:
        source = self._source(_ReadyEmbedder())
        asyncio.run(source.ready(tenant_id="apex"))

    def test_an_embedder_that_is_not_ready_makes_the_source_not_ready(self) -> None:
        embedder = _NotReadyEmbedder()
        source = self._source(embedder)
        with pytest.raises(EmbeddingUnavailableError):
            asyncio.run(source.ready(tenant_id="apex"))
        assert embedder.probed == 1
