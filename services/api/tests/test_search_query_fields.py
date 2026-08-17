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


# Every read or bulk-mutation this adapter issues against a tenant's chunks.
# A new one belongs here: the mapping contract below is only as wide as this
# list, and a query nobody exercises is exactly how BUG-020's sibling shipped.
_QUERY_CALLS = (
    "active_chunk_count",
    "active_embedding_models",
    "active_version_ids",
    "active_chunks",
    "chunk_by_id",
    "has_active_chunks_for_generation",
    "generation_chunks",
    "deactivate_stale_chunks",
    "delete_generation_chunks",
)


async def _invoke(index: ElasticsearchSearchIndex, call: str) -> None:
    generation_id, version_id, document_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    if call == "active_chunk_count":
        await index.active_chunk_count(tenant_id="t1", version_id=version_id)
    elif call == "active_embedding_models":
        await index.active_embedding_models(tenant_id="t1", version_id=version_id)
    elif call == "active_version_ids":
        await index.active_version_ids(tenant_id="t1", document_id=document_id)
    elif call == "active_chunks":
        await index.active_chunks(tenant_id="t1")
    elif call == "chunk_by_id":
        await index.chunk_by_id(tenant_id="t1", chunk_id="c-1")
    elif call == "has_active_chunks_for_generation":
        await index.has_active_chunks_for_generation(tenant_id="t1", generation_id=generation_id)
    elif call == "generation_chunks":
        await index.generation_chunks(tenant_id="t1", generation_id=generation_id)
    elif call == "deactivate_stale_chunks":
        await index.deactivate_stale_chunks(
            tenant_id="t1", document_id=document_id, keep_generation_id=generation_id
        )
    else:
        await index.delete_generation_chunks(tenant_id="t1", generation_id=generation_id)


def _captured_search(call: str) -> dict[str, Any]:
    """The request body one read or bulk-mutation sends."""
    sent: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(("/_search", "/_update_by_query", "/_delete_by_query")):
            sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "hits": {"hits": [], "total": {"value": 0}},
                "aggregations": {"models": {"buckets": []}, "versions": {"buckets": []}},
                "updated": 0,
                "deleted": 0,
            },
        )

    index = _index(handler)

    async def invoke() -> None:
        try:
            await _invoke(index, call)
        finally:
            await index.close()

    asyncio.run(invoke())
    return sent


def _referenced_fields(body: Any) -> set[str]:
    """Every index field a query body names, wherever it names it.

    Walks the whole body rather than the clauses one method happens to use, so
    a filter moved into `filter`, `should`, or a new aggregation is still held
    to the mapping.
    """
    found: set[str] = set()
    if isinstance(body, dict):
        for key, value in body.items():
            if key in {"term", "terms", "match", "range"} and isinstance(value, dict):
                # `{"terms": {"field": "x", "size": 10}}` is an aggregation and
                # names its field by value; `{"terms": {"x": [...]}}` is a query
                # clause and names it by key.
                if "field" in value:
                    found |= _referenced_fields(value)
                else:
                    found.update(name for name in value if name != "boost")
            elif key == "field" and isinstance(value, str):
                found.add(value)
            elif key == "sort":
                for clause in value if isinstance(value, list) else []:
                    if isinstance(clause, dict):
                        found.update(clause)
                    elif isinstance(clause, str):
                        found.add(clause)
            else:
                found |= _referenced_fields(value)
    elif isinstance(body, list):
        for item in body:
            found |= _referenced_fields(item)
    return found


def test_the_stored_document_does_not_repeat_the_chunk_id() -> None:
    """The premise the query shape depends on: chunk_id is the _id, not a field."""
    assert "chunk_id" not in _CHUNK.to_document()


@pytest.mark.parametrize("call", _QUERY_CALLS)
def test_every_query_names_only_mapped_fields(call: str) -> None:
    """A field the mapping lacks fails the whole request, not just that clause.

    Sorting on the unmapped `chunk_id` took retrieval down completely: every
    turn recorded `retriever_version: "unavailable"` with no evidence and no
    citations, which reads exactly like a genuine no-match. Holding each query
    to the adapter's own mapping is what makes that a build failure instead of
    an outage a long-lived index happens to mask.
    """
    mapped = _mapped_fields()
    unmapped = {
        field
        for field in _referenced_fields(_captured_search(call))
        if field not in mapped and not field.startswith("_")
    }
    assert not unmapped, (
        f"{call} references {sorted(unmapped)}, which the index mapping does not declare. "
        "Elasticsearch rejects the entire request."
    )


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
