"""Retrieval queries may only name fields the index actually holds.

The live cluster reported `retriever_version: "unavailable"` on every turn
because `active_chunks` sorted on `chunk_id`. That is the document ``_id``, and
:meth:`IndexedChunk.to_document` deliberately does not repeat it as a field, so
Elasticsearch rejected the entire search with "No mapping found for [chunk_id]
in order to sort on". Retrieval degraded to no evidence, and every grounded
answer lost its citations while looking like a genuine no-match.

The old index happened to carry a stale dynamically-mapped `chunk_id`, so the
fault only appeared once the index was recreated from the adapter's own
mapping. These tests hold the query shape to the mapping instead of to whatever
a long-lived index accumulated.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest

from tenantchat.api.search import ElasticsearchSearchIndex, IndexedChunk

INDEX_NAME = "chunks"

_CHUNK = IndexedChunk(
    chunk_id="c-1",
    tenant_id="t1",
    domain="general",
    document_id=uuid.uuid4(),
    version_id=uuid.uuid4(),
    generation_id=uuid.uuid4(),
    title="Title",
    section="Section",
    text="Body",
    embedding_model="model@1",
    embedding=(0.1, 0.2),
)


def _mapped_fields() -> set[str]:
    """The fields the adapter's own create-index mapping declares."""
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            captured.update(json.loads(request.content))
        return httpx.Response(200, json={})

    index = _index(handler)

    async def invoke() -> None:
        try:
            await index.ensure_mapping(2)
        finally:
            await index.close()

    asyncio.run(invoke())
    return set(captured["mappings"]["properties"])


def _index(handler: Any) -> ElasticsearchSearchIndex:
    return ElasticsearchSearchIndex(
        base_url="http://search:9200",
        username="elastic",
        password="pw",
        index_name=INDEX_NAME,
        transport=httpx.MockTransport(handler),
    )


def _captured_search(call: str) -> dict[str, Any]:
    """The search body one read method sends."""
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/_search"):
            sent.update(json.loads(request.content))
        return httpx.Response(200, json={"hits": {"hits": [], "total": {"value": 0}}})

    index = _index(handler)

    async def invoke() -> None:
        try:
            if call == "active_chunks":
                await index.active_chunks(tenant_id="t1")
            else:
                await index.generation_chunks(tenant_id="t1", generation_id=uuid.uuid4())
        finally:
            await index.close()

    asyncio.run(invoke())
    return sent


def test_the_stored_document_does_not_repeat_the_chunk_id() -> None:
    """The premise the query shape depends on: chunk_id is the _id, not a field."""
    assert "chunk_id" not in _CHUNK.to_document()


@pytest.mark.parametrize("call", ["active_chunks", "generation_chunks"])
def test_pool_reads_do_not_sort_on_an_unmapped_field(call: str) -> None:
    """A sort on a field the mapping lacks fails the whole search, not the sort."""
    body = _captured_search(call)
    mapped = _mapped_fields()
    for clause in body.get("sort", []):
        field = next(iter(clause)) if isinstance(clause, dict) else str(clause)
        assert field in mapped or field.startswith("_"), (
            f"{call} sorts on {field!r}, which the index mapping does not declare. "
            "Elasticsearch rejects the entire search and retrieval reports itself unavailable."
        )


@pytest.mark.parametrize("call", ["active_chunks", "generation_chunks"])
def test_pool_reads_query_only_mapped_fields(call: str) -> None:
    """Same failure mode through a term clause instead of a sort."""
    body = _captured_search(call)
    mapped = _mapped_fields()
    for clause in body["query"]["bool"]["must"]:
        field = next(iter(clause["term"]))
        assert field in mapped, f"{call} filters on unmapped field {field!r}"


def test_the_retrieval_pool_is_ordered_by_chunk_id() -> None:
    """Determinism moved into Python; losing it would make replay unstable."""
    hits = [
        {"_id": "c-3", "_source": _CHUNK.to_document()},
        {"_id": "c-1", "_source": _CHUNK.to_document()},
        {"_id": "c-2", "_source": _CHUNK.to_document()},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"hits": {"hits": hits, "total": {"value": len(hits)}}})

    index = _index(handler)

    async def invoke() -> tuple[str, ...]:
        try:
            chunks = await index.active_chunks(tenant_id="t1")
            return tuple(chunk.chunk_id for chunk in chunks)
        finally:
            await index.close()

    assert asyncio.run(invoke()) == ("c-1", "c-2", "c-3")
