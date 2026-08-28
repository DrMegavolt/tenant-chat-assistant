"""REL-001 failure injection for the Elasticsearch and embedding clients.

Every failure class the acceptance criteria name — timeout, reset, ``429``,
``5xx``, malformed response, and recovery — is exercised here against fake
transports, plus the two structural guarantees shared with the LLM client: a
cancelled request is never retried, and an open circuit fails fast without
touching the network. Every Elasticsearch write this adapter makes is idempotent
(deterministic ``_id`` bulk upserts, delete-by-query, update-by-query), so a
retry after a timeout cannot duplicate index state; the retries are bounded by
the policy.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import httpx
import pytest
from prometheus_client import CollectorRegistry

from tenantchat.api.metrics import PrometheusMetrics
from tenantchat.api.search import (
    ElasticsearchSearchIndex,
    EmbeddingResult,
    EmbeddingServiceClient,
    EmbeddingUnavailableError,
    SearchIndexOperationError,
)
from tenantchat.core.resilience import CircuitPolicy, ResiliencePolicy, RetryPolicy

INDEX_NAME = "tenant-knowledge-chunks"


def policy(
    *,
    max_attempts: int = 3,
    threshold: int = 5,
    cooldown: float = 30.0,
) -> ResiliencePolicy:
    return ResiliencePolicy(
        retries=RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=0.0,
            max_delay_seconds=0.01,
            jitter_seconds=0.0,
        ),
        circuit=CircuitPolicy(failure_threshold=threshold, cooldown_seconds=cooldown),
        read_timeout_seconds=10.0,
    )


def _index(handler: httpx.MockTransport, *, applied: ResiliencePolicy) -> ElasticsearchSearchIndex:
    return ElasticsearchSearchIndex(
        base_url="http://search:9200",
        username="elastic",
        password="pw",
        index_name=INDEX_NAME,
        policy=applied,
        transport=handler,
    )


def _count(index: ElasticsearchSearchIndex) -> int:
    async def invoke() -> int:
        try:
            return await index.active_chunk_count(tenant_id="t1")
        finally:
            await index.close()

    return asyncio.run(invoke())


def _fail(index: ElasticsearchSearchIndex) -> Exception:
    async def invoke() -> None:
        try:
            await index.active_chunk_count(tenant_id="t1")
        finally:
            await index.close()

    try:
        asyncio.run(invoke())
    except Exception as exc:
        return exc
    raise AssertionError("expected the search call to raise")


class TestElasticsearchFailures:
    def test_a_timeout_is_retried_then_raises_an_operation_error(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("search did not answer", request=request)

        exc = _fail(_index(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, SearchIndexOperationError)
        assert attempts == 3

    def test_a_connection_reset_is_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.RemoteProtocolError("connection reset", request=request)

        exc = _fail(_index(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, SearchIndexOperationError)
        assert attempts == 3

    def test_a_rate_limit_is_retried_and_recovers(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(429)
            return httpx.Response(200, json={"hits": {"total": 3}})

        index = _index(httpx.MockTransport(handler), applied=policy())
        assert _count(index) == 3
        assert attempts == 3

    def test_a_5xx_is_retried_then_raises(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        exc = _fail(_index(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, SearchIndexOperationError)
        assert attempts == 3

    def test_an_outage_that_recovers_mid_budget_succeeds(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"hits": {"total": 2}})

        index = _index(httpx.MockTransport(handler), applied=policy())
        assert _count(index) == 2
        assert attempts == 3

    def test_a_malformed_response_is_not_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, text="not json")

        exc = _fail(_index(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, SearchIndexOperationError)
        assert attempts == 1

    def test_a_missing_index_404_returns_empty_without_an_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        index = _index(httpx.MockTransport(handler), applied=policy())
        assert _count(index) == 0

    def test_a_retried_bulk_rewrites_the_same_deterministic_ids(self) -> None:
        """A retry after a committed write cannot duplicate chunk state.

        ``index_chunks`` keys every document by its server-derived
        ``chunk_id``, so a retried ``_bulk`` upserts the same ids it would have
        written the first time: a timeout after the first attempt committed can
        never leave duplicate active chunks behind.
        """
        from datetime import UTC, datetime

        from tenantchat.api.search import IndexedChunk

        chunk_ids = ["generation:version:000000", "generation:version:000001"]
        attempts = 0
        posted_ids: list[set[str]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            lines = [
                json.loads(line) for line in request.content.decode().splitlines() if line.strip()
            ]
            posted_ids.append({line["index"]["_id"] for line in lines if "index" in line})
            if attempts == 1:
                return httpx.Response(503)
            return httpx.Response(200, json={"errors": False})

        index = _index(httpx.MockTransport(handler), applied=policy())
        chunks = [
            IndexedChunk(
                chunk_id=chunk_id,
                tenant_id="t1",
                domain="residential",
                document_id=uuid.UUID(int=1),
                version_id=uuid.UUID(int=2),
                generation_id=uuid.UUID(int=3),
                title="t",
                section="s",
                text="content",
                embedding_model="m",
                embedding=(0.1, 0.2),
                created_at=datetime.now(UTC),
            )
            for chunk_id in chunk_ids
        ]

        async def invoke() -> None:
            try:
                assert await index.index_chunks(chunks) == 2
            finally:
                await index.close()

        asyncio.run(invoke())
        assert attempts == 2
        assert all(ids == set(chunk_ids) for ids in posted_ids)

    def test_a_non_rate_limit_4xx_is_not_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(400, json={"error": "bad request"})

        exc = _fail(_index(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, SearchIndexOperationError)
        assert attempts == 1


class TestEmbeddingFailures:
    def _client(
        self, handler: httpx.MockTransport, *, applied: ResiliencePolicy
    ) -> EmbeddingServiceClient:
        return EmbeddingServiceClient(
            base_url="http://embed:8000",
            token=None,
            policy=applied,
            transport=handler,
        )

    def _embed(self, client: EmbeddingServiceClient) -> EmbeddingResult:
        async def invoke() -> EmbeddingResult:
            try:
                return await client.embed(["alpha", "beta"])
            finally:
                await client.close()

        return asyncio.run(invoke())

    def _fail(self, client: EmbeddingServiceClient) -> Exception:
        async def invoke() -> None:
            try:
                await client.embed(["alpha", "beta"])
            finally:
                await client.close()

        try:
            asyncio.run(invoke())
        except Exception as exc:
            return exc
        raise AssertionError("expected the embedding call to raise")

    def test_a_timeout_is_retried_then_raises_unavailable(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.ReadTimeout("embed did not answer", request=request)

        exc = self._fail(self._client(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, EmbeddingUnavailableError)
        assert attempts == 3

    def test_a_connection_reset_is_retried_then_raises_unavailable(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.RemoteProtocolError("connection reset", request=request)

        exc = self._fail(self._client(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, EmbeddingUnavailableError)
        assert attempts == 3

    def test_a_5xx_is_retried_then_raises_unavailable(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        exc = self._fail(self._client(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, EmbeddingUnavailableError)
        assert attempts == 3

    def test_a_rate_limit_is_retried_and_recovers(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(429)
            return httpx.Response(
                200,
                json={
                    "model": "scripted",
                    "dimensions": 2,
                    "embeddings": [[0.1, 0.2], [0.3, 0.4]],
                },
            )

        client = self._client(httpx.MockTransport(handler), applied=policy())
        result = self._embed(client)
        assert result.model == "scripted"
        assert result.vectors == ((0.1, 0.2), (0.3, 0.4))
        assert attempts == 3

    def test_a_malformed_response_is_not_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, text="not json")

        exc = self._fail(self._client(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, EmbeddingUnavailableError)
        assert attempts == 1

    def test_a_provider_that_returns_no_vectors_is_refused(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"model": "scripted", "dimensions": 2})

        exc = self._fail(self._client(httpx.MockTransport(handler), applied=policy()))
        assert isinstance(exc, EmbeddingUnavailableError)


class TestSearchCircuitBreaking:
    def test_an_open_breaker_fails_fast_without_touching_the_network(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        index = _index(
            httpx.MockTransport(handler),
            applied=policy(max_attempts=1, threshold=2),
        )

        async def scenario() -> None:
            for _ in range(2):
                with pytest.raises(SearchIndexOperationError):
                    await index.active_chunk_count(tenant_id="t1")
            with pytest.raises(SearchIndexOperationError):
                await index.active_chunk_count(tenant_id="t1")
            await index.close()

        asyncio.run(scenario())
        assert attempts == 2

    def test_a_half_open_probe_recovers_the_breaker(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                return httpx.Response(503)
            return httpx.Response(200, json={"hits": {"total": 1}})

        index = _index(
            httpx.MockTransport(handler),
            applied=policy(max_attempts=1, threshold=2, cooldown=0.05),
        )

        async def scenario() -> None:
            for _ in range(2):
                with pytest.raises(SearchIndexOperationError):
                    await index.active_chunk_count(tenant_id="t1")
            with pytest.raises(SearchIndexOperationError):
                await index.active_chunk_count(tenant_id="t1")
            await asyncio.sleep(0.06)
            count = await index.active_chunk_count(tenant_id="t1")
            assert count == 1
            await index.close()

        asyncio.run(scenario())
        assert attempts == 3

    def test_cancellation_propagates_without_a_retry(self) -> None:
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(10)
            return httpx.Response(200, json={"count": 1})

        async def scenario() -> None:
            index = _index(httpx.MockTransport(handler), applied=policy())
            task = asyncio.create_task(index.active_chunk_count(tenant_id="t1"))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await index.close()

        asyncio.run(scenario())
        assert attempts == 1


class TestSearchObservability:
    def test_retries_and_circuit_state_render_through_the_prometheus_adapter(self) -> None:
        metrics = PrometheusMetrics(CollectorRegistry())
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"hits": {"total": 4}})

        recorded = ElasticsearchSearchIndex(
            base_url="http://search:9200",
            username="elastic",
            password="pw",
            index_name=INDEX_NAME,
            policy=policy(),
            metrics=metrics,
            transport=httpx.MockTransport(handler),
        )

        async def scenario() -> None:
            try:
                assert await recorded.active_chunk_count(tenant_id="t1") == 4
            finally:
                await recorded.close()

        asyncio.run(scenario())
        assert attempts == 2

        samples = {
            (sample.name, frozenset(sample.labels.items())): sample.value
            for collector in metrics.registry.collect()
            for sample in collector.samples
        }
        retries = {
            labels: value
            for (name, labels), value in samples.items()
            if name == "tenantchat_dependency_retries_total"
        }
        assert retries == {frozenset({("dependency", "search"), ("reason", "rate_limited")}): 1.0}
        state = {
            labels: value
            for (name, labels), value in samples.items()
            if name == "tenantchat_circuit_state"
        }
        assert state[frozenset({("dependency", "search"), ("state", "closed")})] == 1.0


class TestSearchUrls:
    """Every read path must address the index exactly once.

    `_request(..., use_index=True)` resolves its `url` argument below the
    index, so passing an already-resolved URL produced
    `http://search:9200/<index>/http://search:9200/<index>/_search`.
    Elasticsearch answered 400, the evidence source raised, and the turn
    recorded `retriever_version: "unavailable"` — an abstention indistinguishable
    from a genuine no-match, on every grounded answer.
    """

    def _capture(self, call: str) -> str:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(
                200,
                json={"count": 0, "hits": {"hits": []}, "aggregations": {}},
            )

        index = _index(httpx.MockTransport(handler), applied=policy())

        async def invoke() -> None:
            try:
                if call == "active_chunks":
                    await index.active_chunks(tenant_id="t1")
                elif call == "chunk_by_id":
                    await index.chunk_by_id(tenant_id="t1", chunk_id="c1")
                elif call == "active_version_ids":
                    await index.active_version_ids(tenant_id="t1", document_id=uuid.uuid4())
                elif call == "active_embedding_models":
                    await index.active_embedding_models(tenant_id="t1", version_id=uuid.uuid4())
                else:
                    await index.active_chunk_count(tenant_id="t1")
            finally:
                await index.close()

        asyncio.run(invoke())
        assert len(seen) == 1
        return seen[0]

    @pytest.mark.parametrize(
        "call",
        [
            # The evidence source reads through this one, so a doubled URL here
            # disables retrieval for every turn.
            "active_chunks",
            "chunk_by_id",
            "active_version_ids",
            "active_embedding_models",
            "active_chunk_count",
        ],
    )
    def test_a_read_url_names_the_index_once(self, call: str) -> None:
        url = self._capture(call)

        assert url.startswith(f"http://search:9200/{INDEX_NAME}/")
        assert url.count(INDEX_NAME) == 1
        assert url.count("http://") == 1


class TestForeignDocuments:
    def test_an_unreadable_chunk_does_not_empty_the_retrieval_pool(self) -> None:
        """One foreign document must not cost a tenant every grounded answer.

        The index is derived and shared: the retired prototype ingester keys
        its chunks `doc_id`, and `IndexedChunk.from_document` rejects those.
        Raising on the pool read made a single such document surface as
        `retriever_version: "unavailable"` for every turn in that tenant —
        an abstention indistinguishable from having no knowledge at all.
        """
        readable = {
            "_id": "chunk-1",
            "_source": {
                "tenant_id": "t1",
                "domain": "policy",
                "document_id": str(uuid.uuid4()),
                "version_id": str(uuid.uuid4()),
                "generation_id": str(uuid.uuid4()),
                "title": "Fees",
                "section": "3. Fees and Rates",
                "text": "The emergency after-hours call-out fee is $145.",
                "embedding_model": "test-model",
                "embedding": [0.1, 0.2],
                "active": True,
                "created_at": "2026-08-07T00:00:00+00:00",
            },
        }
        # The prototype's shape: `doc_id` instead of `document_id`.
        foreign = {
            "_id": "legacy-1",
            "_source": {
                "tenant_id": "t1",
                "doc_id": "financing-overview",
                "chunk_id": "legacy-1",
                "text": "Financing is available.",
                "active": True,
                "created_at": "2026-08-07T00:00:00+00:00",
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"hits": {"hits": [foreign, readable]}})

        index = _index(httpx.MockTransport(handler), applied=policy())

        async def invoke() -> tuple[str, ...]:
            try:
                chunks = await index.active_chunks(tenant_id="t1")
                return tuple(chunk.chunk_id for chunk in chunks)
            finally:
                await index.close()

        assert asyncio.run(invoke()) == ("chunk-1",)


class TestReadinessProbes:
    """R-25: readiness is a real signal, not a misdirected request.

    The index's probe used to hit the embedding server's ``/ready`` path on
    the Elasticsearch host, and the embedder client had no probe at all — so
    deployment readiness proved neither dependency. The index now probes its
    own cluster root and the client probes the embedding service."""

    def test_the_index_readiness_probe_hits_the_search_cluster_root(self) -> None:
        probed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            probed.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json={"version": {"number": "8.11.0"}})

        index = _index(httpx.MockTransport(handler), applied=policy())

        async def invoke() -> None:
            try:
                await index.ready()
            finally:
                await index.close()

        asyncio.run(invoke())
        assert probed == ["GET /"]

    def test_a_cluster_that_answers_without_its_identity_is_not_ready(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"status": "ok"})

        index = _index(httpx.MockTransport(handler), applied=policy())

        async def invoke() -> Exception:
            try:
                await index.ready()
            except Exception as exc:
                return exc
            finally:
                await index.close()
            raise AssertionError("ready() should have failed")

        assert isinstance(asyncio.run(invoke()), SearchIndexOperationError)

    def _client(self, handler: httpx.MockTransport) -> EmbeddingServiceClient:
        return EmbeddingServiceClient(
            base_url="http://embed:8000",
            token=None,
            policy=policy(),
            transport=handler,
        )

    def test_the_embedder_readiness_probe_hits_the_embedding_service(self) -> None:
        probed: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            probed.append(f"{request.method} {request.url.path}")
            return httpx.Response(200, json={"status": "ready", "modelLoaded": True})

        client = self._client(httpx.MockTransport(handler))

        async def invoke() -> None:
            try:
                await client.ready()
            finally:
                await client.close()

        asyncio.run(invoke())
        assert probed == ["GET /ready"]

    def test_an_embedding_service_still_loading_is_not_ready(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503, json={"status": "loading", "modelLoaded": False})

        client = self._client(httpx.MockTransport(handler))

        async def invoke() -> Exception:
            try:
                await client.ready()
            except Exception as exc:
                return exc
            finally:
                await client.close()
            raise AssertionError("ready() should have failed")

        assert isinstance(asyncio.run(invoke()), EmbeddingUnavailableError)

    def test_an_unreachable_embedding_service_is_not_ready(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = self._client(httpx.MockTransport(handler))

        async def invoke() -> Exception:
            try:
                await client.ready()
            except Exception as exc:
                return exc
            finally:
                await client.close()
            raise AssertionError("ready() should have failed")

        assert isinstance(asyncio.run(invoke()), EmbeddingUnavailableError)
