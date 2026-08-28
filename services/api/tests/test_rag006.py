"""RAG-006 end-to-end proofs: conversation-aware retrieval on the chat path.

The graph resolves a follow-up against authorized conversation state before it
retrieves (`tenantchat.core.planning`), so a deictic turn that would abstain on
its own words ("What about it?") retrieves the context the earlier exchange
established, and a topic switch or correction drops that context and starts
clean. The resolved query rides the evidence manifest in the checkpoint, so
the turn is reconstructible.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from services.api.tests.conftest import BOOKING_TENANT
from tenantchat.api.app import create_app
from tenantchat.api.evidence import RetrievalEvidenceSource
from tenantchat.api.index_integrity import InMemoryIndexIntegrityStore
from tenantchat.api.retrieval import HybridRetrieverConfig
from tenantchat.api.search import EmbeddingResult, IndexedChunk, InMemorySearchIndex
from tenantchat.api.settings import Settings
from tenantchat.api.storage import MemoryObjectStore
from tenantchat.api.store import (
    InMemoryAuditStore,
    InMemoryBookingStore,
    InMemoryConsentStore,
    InMemoryConversationStore,
    InMemoryHandoffStore,
    InMemoryIdempotencyStore,
    InMemoryKnowledgeStore,
    InMemoryLeadStore,
    InMemoryMembershipStore,
    InMemoryPrivacyStore,
)
from tenantchat.core.knowledge import (
    ContentChecksum,
    KnowledgeDomain,
    SourceKind,
)
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import AssembledPrompt, ModelResponse, ToolSpec

CARE_PLAN = "The Care Plan covers two HVAC tune-ups per year and a 10 percent discount on repairs."
HOURS = "Clearview's business hours are daily from 7 AM to 7 PM."


class _UniformEmbedder:
    """Every text embeds to the same vector, so lexical signal decides."""

    async def ready(self) -> None:
        return None

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors = [(1.0, 0.0, 0.0, 0.0)] * len(texts)
        return EmbeddingResult(model="scripted-embedder.v1", dimensions=4, vectors=vectors)


class _ScriptedModel:
    """Replays a fixed list of responses, then repeats the last one."""

    def __init__(self, script: list[ModelResponse]) -> None:
        self.script = script
        self.calls: list[AssembledPrompt] = []

    async def complete(
        self, prompt: AssembledPrompt, *, tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        self.calls.append(prompt)
        return self.script[min(len(self.calls) - 1, len(self.script) - 1)]


def _settings() -> Settings:
    return Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=2048,
        docs_enabled=True,
        admin_gateway_token="gateway-token-for-tests",
        admin_csrf_secret="csrf-secret-for-tests",
        dev_auth=False,
    )


def _publish(
    knowledge: InMemoryKnowledgeStore,
    *,
    external_key: str,
    title: str,
    text: str,
) -> tuple[uuid.UUID, uuid.UUID]:
    """Stage, approve, publish, and index one chunk of a fresh document."""
    source = asyncio.run(
        knowledge.register_source(
            BOOKING_TENANT,
            domain=KnowledgeDomain.parse("hvac"),
            kind=SourceKind.MANUAL,
            display_name="Clearview HVAC manual",
        )
    )
    now = datetime.now(UTC)
    document = asyncio.run(
        knowledge.stage_version(
            BOOKING_TENANT,
            source_id=source.source_id,
            external_key=external_key,
            title=title,
            checksum=ContentChecksum.of(text.encode()),
            byte_size=len(text),
            media_type="text/markdown",
            storage_key=f"{external_key}.md",
        )
    )
    version = document.versions[-1]
    asyncio.run(
        knowledge.approve(BOOKING_TENANT, version.version_id, approved_by="operator-7", at=now)
    )
    document = asyncio.run(
        knowledge.publish(BOOKING_TENANT, version.version_id, at=now, effective_at=now)
    )
    asyncio.run(knowledge.record_indexed(BOOKING_TENANT, version.version_id, at=now))
    published = document.version(version.version_id)
    return published.document_id, published.version_id


def _chunk(
    chunk_id: str,
    text: str,
    *,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=chunk_id,
        tenant_id=BOOKING_TENANT,
        domain="hvac",
        document_id=document_id,
        version_id=version_id,
        generation_id=uuid.uuid4(),
        title="Clearview knowledge",
        section="Manual",
        text=text,
        embedding_model="scripted-embedder.v1",
        embedding=(1.0, 0.0, 0.0, 0.0),
    )


def _knowledge(
    chunks: Sequence[tuple[str, str]],
) -> tuple[InMemoryKnowledgeStore, list[IndexedChunk]]:
    """Publish each chunk through a fresh system of record and index it."""
    knowledge = InMemoryKnowledgeStore()
    built: list[IndexedChunk] = []
    for chunk_id, text in chunks:
        document_id, version_id = _publish(
            knowledge,
            external_key=chunk_id,
            title=chunk_id,
            text=text,
        )
        built.append(_chunk(chunk_id, text, document_id=document_id, version_id=version_id))
    return knowledge, built


def _client(
    *,
    script: list[ModelResponse],
    chunks: Sequence[IndexedChunk],
    knowledge: InMemoryKnowledgeStore | None = None,
) -> tuple[TestClient, _ScriptedModel]:
    """An app over the seeded chunks, with the RAG-005 evidence port wired."""
    if knowledge is None:
        knowledge = InMemoryKnowledgeStore()
    index = InMemorySearchIndex()
    asyncio.run(index.index_chunks(chunks))
    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    model = _ScriptedModel(script)
    app = create_app(
        _settings(),
        booking_store=InMemoryBookingStore(),
        lead_store=InMemoryLeadStore(),
        conversation_store=conversations,
        handoff_store=InMemoryHandoffStore(),
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=InMemoryMembershipStore(),
        consent_store=consent,
        privacy_store=InMemoryPrivacyStore(
            conversations,
            InMemoryBookingStore(),
            InMemoryLeadStore(),
            InMemoryHandoffStore(),
            consent,
        ),
        audit_store=InMemoryAuditStore(),
        chat_model=model,
        checkpointer=InMemorySaver(),
        knowledge_store=knowledge,
        generation_findings=InMemoryIndexIntegrityStore(),
        object_store=MemoryObjectStore(),
        search_index=index,
        evidence_source=RetrievalEvidenceSource(
            index=index,
            embedder=_UniformEmbedder(),
            knowledge=knowledge,
            config=HybridRetrieverConfig(),
        ),
    )
    return TestClient(app, raise_server_exceptions=False), model


def _open_session(client: TestClient) -> dict[str, str]:
    opened = client.post("/api/chat/session", json={"tenant_id": BOOKING_TENANT})
    assert opened.status_code == 201, opened.text
    headers = {"X-Visitor-Credential": opened.json()["credential"]}
    granted = client.post(
        "/api/chat/consent",
        json={"purposes": ["booking", "follow_up"]},
        headers=headers,
    )
    assert granted.status_code == 200, granted.text
    return headers


def test_a_deictic_follow_up_resolves_against_the_prior_turn() -> None:
    """A bare "What about it?" after the Care Plan exchange retrieves the plan —
    the carryover of the earlier turn's service is what reaches the boundary —
    and the same message with no prior turn to resolve against abstains."""
    knowledge, chunks = _knowledge([("cv-care-plan", CARE_PLAN), ("cv-hours", HOURS)])
    client, model = _client(
        knowledge=knowledge,
        script=[
            ModelResponse(content="Clearview's Care Plan covers HVAC.", model_name="scripted"),
            ModelResponse(
                content="The Care Plan covers two tune-ups per year. [evidence:cv-care-plan]",
                model_name="scripted",
            ),
        ],
        chunks=chunks,
    )
    headers = _open_session(client)

    first = client.post("/api/chat", json={"message": "What is the Care Plan?"}, headers=headers)
    assert first.status_code == 200
    assert first.json()["reply"] == "Clearview's Care Plan covers HVAC."

    second = client.post("/api/chat", json={"message": "What about it?"}, headers=headers)
    assert second.status_code == 200
    body = second.json()
    assert body["reply"] == "The Care Plan covers two tune-ups per year."
    assert [citation["source_id"] for citation in body["citations"]] == ["cv-care-plan"]

    # The same deictic message with no conversation to resolve against abstains:
    # its own words match nothing, so nothing untrusted was laundered in.
    fresh_client, fresh_model = _client(
        script=[ModelResponse(content="should never run", model_name="scripted")],
        chunks=chunks,
    )
    fresh_headers = _open_session(fresh_client)
    control = fresh_client.post(
        "/api/chat", json={"message": "What about it?"}, headers=fresh_headers
    )
    assert control.status_code == 200
    assert control.json()["reply"].startswith("I do not have approved material")
    assert fresh_model.calls == []


def test_a_topic_switch_drops_the_carried_context() -> None:
    """After discussing the Care Plan, "What are your hours?" is a fresh topic:
    the resolved query is the message alone and the hours passage is retrieved."""
    knowledge, chunks = _knowledge([("cv-care-plan", CARE_PLAN), ("cv-hours", HOURS)])
    client, _ = _client(
        knowledge=knowledge,
        script=[
            ModelResponse(content="Clearview's Care Plan covers HVAC.", model_name="scripted"),
            ModelResponse(
                content="We are open daily from 7 AM to 7 PM. [evidence:cv-hours]",
                model_name="scripted",
            ),
        ],
        chunks=chunks,
    )
    headers = _open_session(client)

    first = client.post("/api/chat", json={"message": "What is the Care Plan?"}, headers=headers)
    assert first.status_code == 200

    second = client.post("/api/chat", json={"message": "What are your hours?"}, headers=headers)
    assert second.status_code == 200
    body = second.json()
    assert body["reply"] == "We are open daily from 7 AM to 7 PM."
    # The Care Plan passage was not carried into the hours question, so it
    # could not have been cited.
    assert [citation["source_id"] for citation in body["citations"]] == ["cv-hours"]


def test_a_correction_resets_the_retrieval_context() -> None:
    """ "I meant the hours." corrects the topic: retrieval starts over
    instead of continuing the Care Plan exchange."""
    knowledge, chunks = _knowledge([("cv-care-plan", CARE_PLAN), ("cv-hours", HOURS)])
    client, _ = _client(
        knowledge=knowledge,
        script=[
            ModelResponse(content="Clearview's Care Plan covers HVAC.", model_name="scripted"),
            ModelResponse(
                content="We are open daily from 7 AM to 7 PM. [evidence:cv-hours]",
                model_name="scripted",
            ),
        ],
        chunks=chunks,
    )
    headers = _open_session(client)

    first = client.post("/api/chat", json={"message": "What is the Care Plan?"}, headers=headers)
    assert first.status_code == 200

    second = client.post("/api/chat", json={"message": "I meant the hours."}, headers=headers)
    assert second.status_code == 200
    assert [citation["source_id"] for citation in second.json()["citations"]] == ["cv-hours"]
