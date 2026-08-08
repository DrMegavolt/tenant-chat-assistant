"""The retrieval index as the ingestion worker and integrity detector see it.

The index holds **derived** data (ADR-0003): every chunk is rebuildable from
the authoritative versions in Postgres, and the index is written only by the
ingestion job, never inside a publish transaction. Two properties make
integrity detectable rather than assumed:

- Every chunk carries the :class:`~tenantchat.core.indexing.IndexGeneration`
  identifier that produced it, plus the embedding model used, so the detector
  can compare what the index *holds* against what Postgres *recorded*.
- ``active`` is a soft delete. Deactivation instead of deletion is what lets a
  superseded-but-still-retrievable version be found: its chunks are still
  there, they are simply no longer supposed to answer.

The Elasticsearch adapter is the production implementation and is exercised
against the disposable local cluster (`make up`); the hermetic suite uses
:class:`InMemorySearchIndex`, which mirrors the same semantics.
"""

from __future__ import annotations

import base64
import json
import logging
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from tenantchat.core.errors import ValidationError
from tenantchat.core.metrics import MetricsReporter
from tenantchat.core.resilience import (
    AsyncResilientCaller,
    Dependency,
    FailureKind,
    ResiliencePolicy,
)

# The connection pool each search/embedding client reuses across requests. A
# per-request client (the prototype's shape) never reuses a connection, so every
# call paid for a TCP handshake and TLS setup.
_HTTP_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

logger = logging.getLogger(__name__)


def _classify_http_error(exc: Exception) -> FailureKind:
    """Map a dependency's HTTP exception to the bounded retry decision (REL-001).

    Timeouts, connection failures, resets, ``429``, and ``5xx`` are
    outage-shaped and retryable. A non-``429`` ``4xx`` and a malformed response
    are contract or policy failures that a retry cannot fix, so they are never
    retried and never trip the breaker.
    """
    if isinstance(exc, httpx.TimeoutException):
        return FailureKind.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return FailureKind.CONNECT
    if isinstance(exc, httpx.TransportError):
        return FailureKind.RESET
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429:
            return FailureKind.RATE_LIMITED
        if 500 <= exc.response.status_code < 600:
            return FailureKind.SERVER_ERROR
        return FailureKind.REFUSED
    if isinstance(exc, ValueError):
        return FailureKind.MALFORMED
    return FailureKind.REFUSED


@dataclass(frozen=True, slots=True)
class IndexedChunk:
    """One chunk as it is written to the retrieval index.

    ``text`` is document content and belongs to the inference plane; the
    integrity findings never reference this type. ``chunk_id`` is a stable
    server-derived identifier, which is what makes bulk upserts idempotent.
    """

    chunk_id: str
    tenant_id: str
    domain: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    generation_id: uuid.UUID
    title: str
    section: str
    text: str
    embedding_model: str
    embedding: tuple[float, ...]
    active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_document(self) -> dict[str, object]:
        return {
            "tenant_id": self.tenant_id,
            "domain": self.domain,
            "document_id": str(self.document_id),
            "version_id": str(self.version_id),
            "generation_id": str(self.generation_id),
            "title": self.title,
            "section": self.section,
            "text": self.text,
            "embedding_model": self.embedding_model,
            "active": self.active,
            "created_at": self.created_at.isoformat(),
            "embedding": list(self.embedding),
        }

    @classmethod
    def from_document(cls, raw: Mapping[str, object], *, chunk_id: str) -> IndexedChunk:
        """Rebuild a chunk from a stored index document (the read side of
        :meth:`to_document`), used by the retrieval-pool and citation reads.

        ``chunk_id`` is the document's ``_id``, which the stored source does
        not repeat.

        Raises:
            ValueError: the document is missing or malformed. A stored index
                document is derived data this adapter wrote, so a shape change
                fails loudly here rather than silently yielding a chunk with
                no embeddings.
        """
        try:
            embedding_values = raw["embedding"]
            if not isinstance(embedding_values, list | tuple):
                raise ValueError("embedding is not a list")
            embedding = tuple(float(value) for value in embedding_values)
            return cls(
                chunk_id=chunk_id,
                tenant_id=str(raw["tenant_id"]),
                domain=str(raw["domain"]),
                document_id=uuid.UUID(str(raw["document_id"])),
                version_id=uuid.UUID(str(raw["version_id"])),
                generation_id=uuid.UUID(str(raw["generation_id"])),
                title=str(raw["title"]),
                section=str(raw["section"]),
                text=str(raw["text"]),
                embedding_model=str(raw["embedding_model"]),
                embedding=embedding,
                active=bool(raw.get("active", True)),
                created_at=datetime.fromisoformat(str(raw.get("created_at"))),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"index document is not a valid chunk: {error}") from error


class SearchIndex(Protocol):
    """The retrieval store's write and integrity-query surface."""

    async def index_chunks(self, chunks: Sequence[IndexedChunk]) -> int:
        """Bulk-upsert chunks by ``chunk_id``; returns the number written.

        Raises:
            ValidationError: a chunk carries no generation identifier.
        """
        ...

    async def deactivate_stale_chunks(
        self, *, tenant_id: str, document_id: uuid.UUID, keep_generation_id: uuid.UUID
    ) -> int:
        """Deactivate every active chunk of the document from older generations.

        Called after the new generation's chunks are written, so a
        superseded version stops being retrievable without a window in which
        the document has no chunks at all.
        """
        ...

    async def delete_generation_chunks(self, *, tenant_id: str, generation_id: uuid.UUID) -> int:
        """Remove every chunk a partial or failed generation wrote.

        This is the cleanup a retried job runs before restarting, so a retry
        can never duplicate active chunks.
        """
        ...

    async def active_chunk_count(
        self, *, tenant_id: str, version_id: uuid.UUID | None = None
    ) -> int:
        """How many active chunks a tenant holds, optionally for one version."""
        ...

    async def active_embedding_models(
        self, *, tenant_id: str, version_id: uuid.UUID
    ) -> tuple[str, ...]:
        """Distinct embedding models among a version's active chunks."""
        ...

    async def active_version_ids(
        self, *, tenant_id: str, document_id: uuid.UUID
    ) -> tuple[uuid.UUID, ...]:
        """Versions of one document that still have active chunks in the index."""
        ...

    async def active_chunks(self, *, tenant_id: str) -> tuple[IndexedChunk, ...]:
        """The tenant's active chunks: the pool retrieval ranks (`RAG-005`).

        Derived data read back for ranking; the caller still applies the
        retrievability predicate (`RAG-004`), because the index cannot see
        publication state.
        """
        ...

    async def chunk_by_id(self, *, tenant_id: str, chunk_id: str) -> IndexedChunk | None:
        """One active chunk of the tenant's, for a citation's source view.

        ``None`` covers absent, inactive, and other-tenant alike: a citation
        resolution must not distinguish why a source is unavailable.
        """
        ...


class InMemorySearchIndex:
    """Hermetic fake mirroring the Elasticsearch adapter's semantics.

    Keyed by ``chunk_id`` so upserts are naturally idempotent; ``active`` is a
    field, exactly as it is in the index documents.
    """

    def __init__(self) -> None:
        self._chunks: dict[str, IndexedChunk] = {}

    async def index_chunks(self, chunks: Sequence[IndexedChunk]) -> int:
        for chunk in chunks:
            if chunk.generation_id is None:
                raise ValidationError(detail="a chunk needs a generation identifier")
            self._chunks[chunk.chunk_id] = chunk
        return len(chunks)

    async def deactivate_stale_chunks(
        self, *, tenant_id: str, document_id: uuid.UUID, keep_generation_id: uuid.UUID
    ) -> int:
        deactivated = 0
        for chunk in self._chunks.values():
            if (
                chunk.tenant_id == tenant_id
                and chunk.document_id == document_id
                and chunk.generation_id != keep_generation_id
                and chunk.active
            ):
                self._chunks[chunk.chunk_id] = _inactive(chunk)
                deactivated += 1
        return deactivated

    async def delete_generation_chunks(self, *, tenant_id: str, generation_id: uuid.UUID) -> int:
        removed = [
            chunk_id
            for chunk_id, chunk in self._chunks.items()
            if chunk.tenant_id == tenant_id and chunk.generation_id == generation_id
        ]
        for chunk_id in removed:
            del self._chunks[chunk_id]
        return len(removed)

    async def active_chunk_count(
        self, *, tenant_id: str, version_id: uuid.UUID | None = None
    ) -> int:
        return sum(
            1
            for chunk in self._chunks.values()
            if chunk.tenant_id == tenant_id
            and chunk.active
            and (version_id is None or chunk.version_id == version_id)
        )

    async def active_embedding_models(
        self, *, tenant_id: str, version_id: uuid.UUID
    ) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    chunk.embedding_model
                    for chunk in self._chunks.values()
                    if chunk.tenant_id == tenant_id
                    and chunk.version_id == version_id
                    and chunk.active
                }
            )
        )

    async def active_version_ids(
        self, *, tenant_id: str, document_id: uuid.UUID
    ) -> tuple[uuid.UUID, ...]:
        return tuple(
            sorted(
                {
                    chunk.version_id
                    for chunk in self._chunks.values()
                    if chunk.tenant_id == tenant_id
                    and chunk.document_id == document_id
                    and chunk.active
                }
            )
        )

    async def active_chunks(self, *, tenant_id: str) -> tuple[IndexedChunk, ...]:
        return tuple(
            sorted(
                (
                    chunk
                    for chunk in self._chunks.values()
                    if chunk.tenant_id == tenant_id and chunk.active
                ),
                key=lambda chunk: chunk.chunk_id,
            )
        )

    async def chunk_by_id(self, *, tenant_id: str, chunk_id: str) -> IndexedChunk | None:
        chunk = self._chunks.get(chunk_id)
        if chunk is None or chunk.tenant_id != tenant_id or not chunk.active:
            return None
        return chunk


def _inactive(chunk: IndexedChunk) -> IndexedChunk:
    # dataclasses.replace preserves the frozen contract without hand-writing
    # the constructor call.
    return replace(chunk, active=False)


def _chunk_from_hit(hit: Mapping[str, object]) -> IndexedChunk:
    """One Elasticsearch hit as a chunk, with the document ``_id`` as its id.

    Raises:
        SearchIndexOperationError: the hit carries no source document.
    """
    source = hit.get("_source")
    if not isinstance(source, Mapping):
        raise SearchIndexOperationError("search index returned a hit without a source")
    chunk_id = hit.get("_id")
    if not isinstance(chunk_id, str) or not chunk_id:
        raise SearchIndexOperationError("search index returned a hit without an _id")
    return IndexedChunk.from_document(source, chunk_id=chunk_id)


class ElasticsearchSearchIndex:
    """Production adapter over the local or in-cluster Elasticsearch 8 cluster.

    Speaks the bulk, delete-by-query, update-by-query, and count HTTP APIs
    directly over httpx (like the LLM adapter speaks chat-completions): the
    surface is small enough that a client library would add weight without
    making the contract clearer. Credentials mirror the demo cluster's
    authentication-on, TLS-off shape (ADR-0003).

    Raises:
        SearchIndexOperationError: the cluster rejected a write or query,
            carrying the Elasticsearch status text rather than response bodies
            (which can contain index contents).
    """

    def __init__(
        self,
        *,
        base_url: str,
        username: str | None,
        password: str | None,
        index_name: str,
        policy: ResiliencePolicy | None = None,
        metrics: MetricsReporter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._index_name = index_name
        resolved = policy or ResiliencePolicy()
        # httpx 0.28 has connect/read/write/pool phases and no total; the total
        # deadline is enforced by the resilient caller across the logical call.
        self._timeout = httpx.Timeout(
            connect=resolved.connect_timeout_seconds,
            read=resolved.read_timeout_seconds,
            write=resolved.write_timeout_seconds,
            pool=resolved.pool_timeout_seconds,
        )
        auth = None
        if username and password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            auth = {"Authorization": f"Basic {token}"}
        self._headers = {"Content-Type": "application/json", **(auth or {})}
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._headers,
            limits=_HTTP_LIMITS,
            transport=transport,
        )
        self._resilience = AsyncResilientCaller(
            dependency=Dependency.SEARCH,
            policy=resolved,
            classify=_classify_http_error,
            metrics=metrics,
        )

    def _url(self, suffix: str) -> str:
        return f"{self._base_url}/{self._index_name}/{suffix}"

    async def close(self) -> None:
        """Release the underlying connection pool."""
        await self._client.aclose()

    async def index_chunks(self, chunks: Sequence[IndexedChunk]) -> int:
        if not chunks:
            return 0
        lines: list[str] = []
        for chunk in chunks:
            lines.append(json.dumps({"index": {"_index": self._index_name, "_id": chunk.chunk_id}}))
            lines.append(json.dumps(chunk.to_document()))
        response = await self._request("POST", f"{self._base_url}/_bulk", "\n".join(lines) + "\n")
        if response.get("errors"):
            raise SearchIndexOperationError("index write reported item errors")
        return len(chunks)

    async def deactivate_stale_chunks(
        self, *, tenant_id: str, document_id: uuid.UUID, keep_generation_id: uuid.UUID
    ) -> int:
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"document_id": str(document_id)}},
                        {"term": {"active": True}},
                    ],
                    "must_not": [{"term": {"generation_id": str(keep_generation_id)}}],
                }
            }
        }
        body = {"query": query["query"], "script": {"source": "ctx._source.active = false"}}
        response = await self._request(
            "POST",
            f"{self._base_url}/{self._index_name}/_update_by_query?refresh=true",
            json.dumps(body),
        )
        return int(response.get("updated", 0))

    async def delete_generation_chunks(self, *, tenant_id: str, generation_id: uuid.UUID) -> int:
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"generation_id": str(generation_id)}},
                    ]
                }
            }
        }
        response = await self._request(
            "POST",
            f"{self._base_url}/{self._index_name}/_delete_by_query?refresh=true",
            json.dumps(query),
        )
        return int(response.get("deleted", 0))

    async def active_chunk_count(
        self, *, tenant_id: str, version_id: uuid.UUID | None = None
    ) -> int:
        query: dict[str, Any] = {
            "query": {
                "bool": {"must": [{"term": {"tenant_id": tenant_id}}, {"term": {"active": True}}]}
            }
        }
        if version_id is not None:
            must = query["query"]["bool"]["must"]
            must.append({"term": {"version_id": str(version_id)}})
        response = await self._request("POST", "_count", json.dumps(query), use_index=True)
        return int(response.get("count", 0))

    async def active_embedding_models(
        self, *, tenant_id: str, version_id: uuid.UUID
    ) -> tuple[str, ...]:
        query = {
            "size": 0,
            "aggs": {"models": {"terms": {"field": "embedding_model", "size": 20}}},
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"version_id": str(version_id)}},
                        {"term": {"active": True}},
                    ]
                }
            },
        }
        response = await self._request("POST", "_search", json.dumps(query), use_index=True)
        buckets = response.get("aggregations", {}).get("models", {}).get("buckets", [])
        return tuple(str(bucket["key"]) for bucket in buckets)

    async def active_version_ids(
        self, *, tenant_id: str, document_id: uuid.UUID
    ) -> tuple[uuid.UUID, ...]:
        query = {
            "size": 0,
            "aggs": {"versions": {"terms": {"field": "version_id", "size": 100}}},
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"document_id": str(document_id)}},
                        {"term": {"active": True}},
                    ]
                }
            },
        }
        response = await self._request("POST", "_search", json.dumps(query), use_index=True)
        buckets = response.get("aggregations", {}).get("versions", {}).get("buckets", [])
        version_ids = [uuid.UUID(str(bucket["key"])) for bucket in buckets]
        return tuple(sorted(version_ids))

    async def active_chunks(self, *, tenant_id: str) -> tuple[IndexedChunk, ...]:
        # Bounded by the cluster's search window: a tenant beyond it is an
        # operator problem, not something retrieval should silently truncate.
        query = {
            "size": 10000,
            "sort": [{"chunk_id": "asc"}],
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"active": True}},
                    ]
                }
            },
        }
        response = await self._request("POST", "_search", json.dumps(query), use_index=True)
        hits = response.get("hits", {}).get("hits", [])
        # A document this adapter did not write is skipped, not raised on. The
        # index is derived and shared — the retired prototype ingester still
        # leaves chunks keyed `doc_id` — and one foreign document must not cost
        # a tenant every answer it could have grounded. `chunk_by_id` stays
        # strict: there the caller named one chunk and a silent miss would
        # drop a citation the answer already made.
        chunks: list[IndexedChunk] = []
        for hit in hits:
            try:
                chunks.append(_chunk_from_hit(hit))
            except (ValueError, SearchIndexOperationError):
                logger.warning(
                    "skipped an unreadable chunk in the retrieval pool",
                    extra={"chunk_id": hit.get("_id"), "index": self._index_name},
                )
        return tuple(chunks)

    async def chunk_by_id(self, *, tenant_id: str, chunk_id: str) -> IndexedChunk | None:
        query = {
            "size": 1,
            "query": {
                "bool": {
                    "must": [
                        {"term": {"tenant_id": tenant_id}},
                        {"term": {"active": True}},
                        {"ids": {"values": [chunk_id]}},
                    ]
                }
            },
        }
        response = await self._request("POST", "_search", json.dumps(query), use_index=True)
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            return None
        return _chunk_from_hit(hits[0])

    async def ensure_mapping(self, dimensions: int) -> None:
        """Create the index with the chunk mapping when it does not exist yet."""
        mapping = {
            "mappings": {
                "properties": {
                    "tenant_id": {"type": "keyword"},
                    "domain": {"type": "keyword"},
                    "document_id": {"type": "keyword"},
                    "version_id": {"type": "keyword"},
                    "generation_id": {"type": "keyword"},
                    "title": {"type": "text"},
                    "section": {"type": "text"},
                    "text": {"type": "text"},
                    "embedding_model": {"type": "keyword"},
                    "active": {"type": "boolean"},
                    "created_at": {"type": "date"},
                    "embedding": {
                        "type": "dense_vector",
                        "dims": dimensions,
                        "index": True,
                        "similarity": "cosine",
                    },
                }
            }
        }
        response = await self._request(
            "PUT", f"{self._base_url}/{self._index_name}", json.dumps(mapping), allow_404=False
        )
        if "error" in response and "resource_already_exists" not in str(response.get("error")):
            raise SearchIndexOperationError("index mapping creation failed")

    async def _request(
        self, method: str, url: str, body: str, *, use_index: bool = False, allow_404: bool = True
    ) -> dict[str, Any]:
        """Issue one request.

        ``url`` is an absolute URL, or — when ``use_index`` is set — a suffix
        below the index that this method resolves. Passing an already-resolved
        URL *and* ``use_index`` concatenates the two, which Elasticsearch
        answers with a 400 that surfaces only as an unavailable retriever.
        """
        if use_index:
            url = self._url(url)
        try:
            return await self._resilience.run(
                lambda: self._raw_request(method, url, body, allow_404=allow_404)
            )
        except Exception as exc:
            raise SearchIndexOperationError("search index unreachable") from exc

    async def _raw_request(
        self, method: str, url: str, body: str, *, allow_404: bool
    ) -> dict[str, Any]:
        response = await self._client.request(method, url, content=body)
        if response.status_code == 404 and allow_404:
            return {}
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("search index returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("search index returned non-object JSON")
        return payload


class SearchIndexOperationError(Exception):
    """The retrieval index refused or lost a request.

    Deliberately carries no response body: bodies can contain index contents
    (`ADR-0010`). The ingestion handler maps this to a retryable safe code.
    """


class EmbeddingResult:
    """One batch of vectors plus the model and dimensions that produced them."""

    def __init__(self, *, model: str, dimensions: int, vectors: Sequence[Sequence[float]]) -> None:
        if dimensions < 1:
            raise ValueError("embedding dimensions must be positive")
        self.model = model
        self.dimensions = dimensions
        self.vectors: tuple[tuple[float, ...], ...] = tuple(tuple(v) for v in vectors)


class Embedder(Protocol):
    """Produces embeddings for chunk text.

    The model identifier is returned with every batch so the ingestion job can
    record which model a generation was embedded with — the value the integrity
    detector compares against what the index actually holds.
    """

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed one bounded batch of texts.

        Raises:
            EmbeddingUnavailableError: the provider failed. The message must
                not carry provider response bodies.
        """
        ...


class EmbeddingUnavailableError(Exception):
    """The embedding provider failed or refused the batch."""


class ScriptedEmbedder:
    """Hermetic fake: deterministic vectors and the recorded model name."""

    def __init__(self, *, model: str = "scripted-embedder.v1", dimensions: int = 4) -> None:
        self._model = model
        self._dimensions = dimensions
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        self.calls.append(tuple(texts))
        vectors = [_deterministic_vector(text, self._dimensions) for text in texts]
        return EmbeddingResult(model=self._model, dimensions=self._dimensions, vectors=vectors)


def _deterministic_vector(text: str, dimensions: int) -> tuple[float, ...]:
    import hashlib

    digest = hashlib.sha256(text.encode()).digest()
    total = sum(digest) or 1
    return tuple((digest[i % len(digest)] / total) for i in range(dimensions))


class EmbeddingServiceClient:
    """Client for the prototype embedding service's ``/embed`` contract.

    The service is replaced by a production provider behind this port later
    (`RAG-003`); the client keeps the ingestion job stable across that swap.
    """

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None,
        batch_size: int = 16,
        policy: ResiliencePolicy | None = None,
        metrics: MetricsReporter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._batch_size = max(1, min(batch_size, 128))
        resolved = policy or ResiliencePolicy()
        # httpx 0.28 has connect/read/write/pool phases and no total; the total
        # deadline is enforced by the resilient caller across the logical call.
        self._timeout = httpx.Timeout(
            connect=resolved.connect_timeout_seconds,
            read=resolved.read_timeout_seconds,
            write=resolved.write_timeout_seconds,
            pool=resolved.pool_timeout_seconds,
        )
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._headers = headers
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            headers=self._headers,
            limits=_HTTP_LIMITS,
            transport=transport,
        )
        self._resilience = AsyncResilientCaller(
            dependency=Dependency.EMBEDDING,
            policy=resolved,
            classify=_classify_http_error,
            metrics=metrics,
        )

    async def close(self) -> None:
        """Release the underlying connection pool."""
        await self._client.aclose()

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        batches: list[tuple[float, ...]] = []
        model = ""
        dimensions = 0
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            payload_model, payload_dimensions, embeddings = await self._embed_batch(batch)
            model = str(payload_model or model)
            dimensions = int(payload_dimensions or dimensions)
            batches.extend(tuple(float(value) for value in item) for item in embeddings)
        if not batches:
            raise EmbeddingUnavailableError("embedding provider returned no vectors")
        return EmbeddingResult(model=model, dimensions=dimensions, vectors=batches)

    async def _embed_batch(self, batch: Sequence[str]) -> tuple[str, int, list[Any]]:
        try:
            return await self._resilience.run(lambda: self._request_batch(batch))
        except Exception as exc:
            raise EmbeddingUnavailableError("embedding provider unreachable") from exc

    async def _request_batch(self, batch: Sequence[str]) -> tuple[str, int, list[Any]]:
        response = await self._client.post(f"{self._base_url}/embed", json={"texts": list(batch)})
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("embedding provider returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("embedding provider returned non-object JSON")
        embeddings = payload.get("embeddings", [])
        if not isinstance(embeddings, list):
            raise ValueError("embedding provider returned non-list embeddings")
        for item in embeddings:
            if not isinstance(item, Sequence) or isinstance(item, str):
                raise ValueError("embedding provider returned a malformed vector")
        return (
            str(payload.get("model", "")),
            int(payload.get("dimensions", 0)),
            embeddings,
        )
