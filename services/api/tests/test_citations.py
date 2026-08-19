"""RAG-005 citation-integrity proofs: the five verdicts, abstention, and the
authorized source view.

The model sees exactly the evidence the adapter admitted to the prompt. These
tests assert the widget's side of that contract: a published citation resolves
to a source that was in the answer's context, a citation to anything else is
dropped from the public response and recorded for the inference plane, and a
question no approved material answers gets the deterministic refusal instead of
a guess.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from services.api.tests.conftest import BOOKING_TENANT, ScriptedModel
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
    InMemoryTurnRecordStore,
)
from tenantchat.core.knowledge import (
    ContentChecksum,
    DocumentVersion,
    KnowledgeDomain,
    SourceKind,
)
from tenantchat.core.ports import EvidenceBundle, EvidenceUnavailableError
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import ModelResponse, ToolCall

HOURS_QUESTION = "What are your hours of operation?"
HOURS_ANSWER = "We are open daily from 7 AM to 7 PM."


def _effective_at(version: DocumentVersion) -> datetime:
    assert version.effective_at is not None  # a published version always carries one
    return version.effective_at


def _section(content: dict[str, object], key: str) -> dict[str, object]:
    """One trace section as the record holds it."""
    value = content[key]
    assert isinstance(value, Mapping)
    return dict(value)


def _list(content: dict[str, object], key: str) -> list[dict[str, object]]:
    """One trace list of records as the record holds it."""
    value = content[key]
    assert isinstance(value, list)
    return [dict(item) for item in value if isinstance(item, Mapping)]


class _UniformEmbedder:
    """Every text embeds to the same vector, so lexical signal decides."""

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors = [(1.0, 0.0, 0.0, 0.0)] * len(texts)
        return EmbeddingResult(model="scripted-embedder.v1", dimensions=4, vectors=vectors)


class _FailingEvidenceSource:
    """An evidence port whose retrieval is down: the graph must abstain."""

    async def retrieve(self, *, tenant_id: str, query: str) -> EvidenceBundle:
        raise EvidenceUnavailableError("index unreachable")


def _settings() -> Settings:
    return Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=2048,
        docs_enabled=True,
        admin_gateway_token="gateway-token-for-tests",
        admin_csrf_secret="csrf-secret-for-tests",
        dev_auth=False,
    )


def _published_version(
    knowledge: InMemoryKnowledgeStore,
    *,
    title: str,
    source_name: str = "Clearview HVAC manual",
) -> DocumentVersion:
    """Stage, approve, publish, and index one version of a fresh document."""
    source = asyncio.run(
        knowledge.register_source(
            BOOKING_TENANT,
            domain=KnowledgeDomain.parse("hvac"),
            kind=SourceKind.MANUAL,
            display_name=source_name,
        )
    )
    return _publish(
        knowledge,
        source_id=source.source_id,
        external_key=f"{title}-doc",
        title=title,
        checksum_value=title.encode(),
    )


def _publish(
    knowledge: InMemoryKnowledgeStore,
    *,
    source_id: uuid.UUID,
    external_key: str,
    title: str,
    checksum_value: bytes,
) -> DocumentVersion:
    """Stage, approve, publish, and index one version of a document.

    A second call with the same ``external_key`` and different content stages
    a successor version that supersedes the first.
    """
    now = datetime.now(UTC)
    document = asyncio.run(
        knowledge.stage_version(
            BOOKING_TENANT,
            source_id=source_id,
            external_key=external_key,
            title=title,
            checksum=ContentChecksum.of(checksum_value),
            byte_size=len(checksum_value),
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
    return document.version(version.version_id)


def _chunk(
    chunk_id: str,
    text: str,
    *,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    tenant_id: str = BOOKING_TENANT,
) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=chunk_id,
        tenant_id=tenant_id,
        domain="hvac",
        document_id=document_id,
        version_id=version_id,
        generation_id=uuid.uuid4(),
        title="Hours and availability",
        section="Operating hours",
        text=text,
        embedding_model="scripted-embedder.v1",
        embedding=(1.0, 0.0, 0.0, 0.0),
    )


def _client(
    *,
    script: list[ModelResponse],
    chunks: Sequence[IndexedChunk] = (),
    knowledge: InMemoryKnowledgeStore | None = None,
    evidence: RetrievalEvidenceSource | _FailingEvidenceSource | None = None,
    config: HybridRetrieverConfig | None = None,
) -> tuple[TestClient, ScriptedModel, InMemoryKnowledgeStore, InMemoryTurnRecordStore]:
    """An app over seeded knowledge, with the RAG-005 evidence port wired."""
    model = ScriptedModel(script)
    if knowledge is None:
        knowledge = InMemoryKnowledgeStore()
    index = InMemorySearchIndex()
    asyncio.run(index.index_chunks(chunks))
    if evidence is None:
        evidence = RetrievalEvidenceSource(
            index=index,
            embedder=_UniformEmbedder(),
            knowledge=knowledge,
            config=config or HybridRetrieverConfig(),
        )
    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    turn_records = InMemoryTurnRecordStore()
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
        turn_record_store=turn_records,
        chat_model=model,
        checkpointer=InMemorySaver(),
        knowledge_store=knowledge,
        generation_findings=InMemoryIndexIntegrityStore(),
        object_store=MemoryObjectStore(),
        search_index=index,
        evidence_source=evidence,
    )
    return TestClient(app, raise_server_exceptions=False), model, knowledge, turn_records


def _open_session(client: TestClient) -> dict[str, str]:
    """Open a consented conversation and return the credential headers."""
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


def test_a_citation_in_the_answers_context_is_published_and_resolves() -> None:
    """The valid case: the model cites a source the prompt carried, and the
    widget can open the authorized view of exactly that passage."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    chunk = _chunk(
        "clearview-hvac-2",
        "Clearview is open daily from 7 AM to 7 PM. Hours of operation are " "seven days a week.",
        document_id=version.document_id,
        version_id=version.version_id,
    )
    client, _, _, _ = _client(
        script=[
            ModelResponse(
                content=f"{HOURS_ANSWER}. [evidence:{chunk.chunk_id}]",
                model_name="scripted",
            )
        ],
        chunks=(chunk,),
        knowledge=knowledge,
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == f"{HOURS_ANSWER}."
    assert body["citations"] == [
        {
            "source_id": "clearview-hvac-2",
            "title": "Hours and availability",
            "source_name": "Clearview HVAC manual",
            "location": "Operating hours",
            "revision": 1,
            "effective_at": _effective_at(version).isoformat().replace("+00:00", "Z"),
        }
    ]
    # The invalid-citation verdict and retrieval internals are inference-plane
    # data: the public response never carries them.
    assert "citation_invalid" not in body
    assert "retrieval" not in body

    source = client.get(f"/api/chat/sources/{chunk.chunk_id}", headers=headers)

    assert source.status_code == 200
    source_view = source.json()
    assert source_view == {
        "source_id": "clearview-hvac-2",
        "title": "Hours and availability",
        "source_name": "Clearview HVAC manual",
        "location": "Operating hours",
        "text": chunk.text,
        "revision": 1,
        "effective_at": _effective_at(version).isoformat().replace("+00:00", "Z"),
    }


def test_a_citation_missing_from_the_context_is_dropped_from_the_public_response() -> None:
    """The missing case: a real, retrievable source the model never saw. The
    citation is not published, the verdict goes to the turn record, and the
    source itself still resolves — it is merely not what this answer was
    grounded on."""
    knowledge = InMemoryKnowledgeStore()
    admitted = _published_version(knowledge, title="Clearview hours")
    unseen = _published_version(knowledge, title="Clearview winter policy")
    client, _, _, _ = _client(
        script=[
            ModelResponse(
                content=f"{HOURS_ANSWER}. [evidence:clearview-hvac-2]"
                "[evidence:clearview-winter]",
                model_name="scripted",
            )
        ],
        chunks=(
            _chunk(
                "clearview-hvac-2",
                "Clearview is open daily from 7 AM to 7 PM. Hours of operation "
                "are seven days a week.",
                document_id=admitted.document_id,
                version_id=admitted.version_id,
            ),
            _chunk(
                "clearview-winter",
                "Winter storm season closes driveways until noon.",
                document_id=unseen.document_id,
                version_id=unseen.version_id,
            ),
        ),
        knowledge=knowledge,
        config=HybridRetrieverConfig(k=1),
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == f"{HOURS_ANSWER}."
    assert [citation["source_id"] for citation in body["citations"]] == ["clearview-hvac-2"]
    # The unseen source is still answerable for a visitor: only the citation is
    # invalid, because the answer never had it in context.
    assert client.get("/api/chat/sources/clearview-winter", headers=headers).status_code == 200


def test_a_fabricated_citation_is_rejected_and_resolves_nowhere() -> None:
    """The fabricated case: a source id that exists nowhere. It is dropped from
    the answer, reported to the inference plane, and 404s as a source view."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    client, _, _, turn_records = _client(
        script=[ModelResponse(content=f"{HOURS_ANSWER}. [evidence:clearview-hvac-999]")],
        chunks=(
            _chunk(
                "clearview-hvac-2",
                "Clearview is open daily from 7 AM to 7 PM. Hours of operation "
                "are seven days a week.",
                document_id=version.document_id,
                version_id=version.version_id,
            ),
        ),
        knowledge=knowledge,
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == f"{HOURS_ANSWER}."
    assert body["citations"] == []
    assert client.get("/api/chat/sources/clearview-hvac-999", headers=headers).status_code == 404


def test_a_stale_source_cannot_be_cited() -> None:
    """The stale case: the index still holds the old version's chunk, but the
    system of record moved on. The adapter drops it before admission, so the
    model's citation of it is invalid and the source view 404s."""
    knowledge = InMemoryKnowledgeStore()
    source = asyncio.run(
        knowledge.register_source(
            BOOKING_TENANT,
            domain=KnowledgeDomain.parse("hvac"),
            kind=SourceKind.MANUAL,
            display_name="Clearview HVAC manual",
        )
    )
    previous = _publish(
        knowledge,
        source_id=source.source_id,
        external_key="hours",
        title="Clearview hours",
        checksum_value=b"v1",
    )
    current = _publish(
        knowledge,
        source_id=source.source_id,
        external_key="hours",
        title="Clearview hours",
        checksum_value=b"v2",
    )
    client, _, _, _ = _client(
        script=[
            ModelResponse(
                content=f"{HOURS_ANSWER}. [evidence:clearview-hvac-2]"
                "[evidence:clearview-hvac-stale]",
                model_name="scripted",
            )
        ],
        chunks=(
            _chunk(
                "clearview-hvac-2",
                "Clearview is open daily from 7 AM to 7 PM. Hours of operation "
                "are seven days a week.",
                document_id=current.document_id,
                version_id=current.version_id,
            ),
            # The superseded version's chunk is still active in the index: index
            # drift is exactly what the domain retrievability predicate is for.
            _chunk(
                "clearview-hvac-stale",
                "Clearview hours of operation cover every day of the week.",
                document_id=previous.document_id,
                version_id=previous.version_id,
            ),
        ),
        knowledge=knowledge,
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert [citation["source_id"] for citation in body["citations"]] == ["clearview-hvac-2"]
    assert client.get("/api/chat/sources/clearview-hvac-stale", headers=headers).status_code == 404


def test_another_tenants_source_cannot_be_cited_or_resolved() -> None:
    """The unauthorized case: a foreign chunk is never in this tenant's context,
    and its source view is refused for this visitor."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    client, _, _, _ = _client(
        script=[
            ModelResponse(
                content=f"{HOURS_ANSWER}. [evidence:apex-hvac-2]",
                model_name="scripted",
            )
        ],
        chunks=(
            _chunk(
                "clearview-hvac-2",
                "Clearview is open daily from 7 AM to 7 PM. Hours of operation "
                "are seven days a week.",
                document_id=version.document_id,
                version_id=version.version_id,
            ),
            _chunk(
                "apex-hvac-2",
                "Apex crews are on the road every morning.",
                document_id=uuid.uuid4(),
                version_id=uuid.uuid4(),
                tenant_id="apex",
            ),
        ),
        knowledge=knowledge,
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    assert response.json()["citations"] == []
    assert client.get("/api/chat/sources/apex-hvac-2", headers=headers).status_code == 404


def test_the_turn_record_carries_the_verified_citations_and_the_verdicts() -> None:
    """The inference-plane envelope: verified citations, invalid citations, and
    the retrieval manifest ride the `OBS-004` trace, not the public response."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    client, _, _, turn_records = _client(
        script=[
            ModelResponse(
                content=f"{HOURS_ANSWER}. [evidence:clearview-hvac-2]"
                "[evidence:clearview-hvac-999]",
                model_name="scripted",
            )
        ],
        chunks=(
            _chunk(
                "clearview-hvac-2",
                "Clearview is open daily from 7 AM to 7 PM. Hours of operation "
                "are seven days a week.",
                document_id=version.document_id,
                version_id=version.version_id,
            ),
        ),
        knowledge=knowledge,
    )
    headers = _open_session(client)
    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)
    session_id = response.json()["session_id"]

    records = asyncio.run(turn_records.for_session(BOOKING_TENANT, uuid.UUID(session_id), limit=10))

    assert len(records) == 1
    content = records[0].content
    assert _section(content, "prompt")["template_ref"] == "dispatch-system@4"
    assert _section(content, "verdicts")["citations"] == [
        {
            "source_id": "clearview-hvac-2",
            "title": "Hours and availability",
            "source_name": "Clearview HVAC manual",
            "location": "Operating hours",
            "revision": 1,
            "effective_at": _effective_at(version).isoformat(),
        }
    ]
    assert _section(content, "verdicts")["citation_invalid"] == ["clearview-hvac-999"]
    retrieval = _section(content, "retrieval")
    assert retrieval["sufficient"] is True
    assert retrieval["retriever_version"] == "v1"
    assert retrieval["reranker"] == "bigram-overlap"
    assert retrieval["min_evidence_score"] == 0.5
    (candidate,) = _list(retrieval, "candidates")
    assert candidate["source_id"] == "clearview-hvac-2"
    assert candidate["score"] == 1.0
    assert candidate["embedding_model"] == "scripted-embedder.v1"
    assert candidate["generation_id"] is not None
    assert content["outcome"] == {
        "status": "answered",
        "rounds": 1,
        "failure": None,
    }
    manifest = _section(content, "component_manifest")
    assert manifest["graph"] == "dispatch@3"
    assert manifest["prompt_template"] == {"ref": "dispatch-system@4"}
    assert manifest["routing_policy"] == "intent-routing@1"
    assert _list(content, "diagnoses") == [
        {
            "cause": "grounding_or_citation_error",
            "stage": "validation",
            "role": "primary",
            "status": "detected",
            "confidence": "high",
            "evidence": ["citation_invalid:clearview-hvac-999"],
            "detector_version": "diagnosis@1",
        }
    ]


def test_insufficient_evidence_answers_from_tenant_business_facts() -> None:
    """A question with no approved evidence may still be answered from the
    tenant's business facts (hours, phone, address), which are server-owned
    truth bound into the prompt and do not require retrieved citations."""
    client, model, _, _ = _client(
        script=[ModelResponse(content="We are open daily.", model_name="scripted")]
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    body = response.json()
    assert body["reply"] == "We are open daily."
    assert body["citations"] == []
    assert len(model.calls) == 1


def test_a_retrieval_failure_answers_from_tenant_business_facts() -> None:
    """An index outage does not stop the model from answering from the tenant's
    own business facts (hours, phone, address) which are bound into the prompt
    independently of retrieval."""
    client, model, _, _ = _client(
        script=[ModelResponse(content="We are open daily.", model_name="scripted")],
        evidence=_FailingEvidenceSource(),
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    assert response.json()["reply"] == "We are open daily."
    assert len(model.calls) == 1


def test_evidence_excluded_by_the_prompt_budget_answers_from_tenant_facts() -> None:
    """The verdict passed but the prompt budget dropped all evidence passages.
    The model may still answer from the tenant's own business facts, which are
    bound into the prompt independently of retrieved evidence."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    oversized = (
        "Hours of operation at Clearview are seven days a week. " * 400 + "Hours of operation."
    )
    client, model, _, _ = _client(
        script=[ModelResponse(content="We are open daily.", model_name="scripted")],
        chunks=(
            _chunk(
                "clearview-hvac-2",
                oversized,
                document_id=version.document_id,
                version_id=version.version_id,
            ),
        ),
        knowledge=knowledge,
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    assert response.json()["reply"] == "We are open daily."
    assert len(model.calls) == 1


def test_a_question_outside_the_tenant_facts_abstains_without_calling_the_model() -> None:
    """The trusted-configuration carve-out is scoped to questions the tenant's own
    configuration answers. A question the router matched nothing for has no
    server-owned fact behind it, so an empty evidence pool leaves the model
    with nothing to answer from and it must not be asked to try.
    """
    client, model, _, _ = _client(
        script=[ModelResponse(content="should never run", model_name="scripted")]
    )
    headers = _open_session(client)

    response = client.post(
        "/api/chat",
        json={"message": "What is included in the Care Plan membership?"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["reply"].startswith("I do not have approved material")
    assert model.calls == []


def test_a_question_outside_the_tenant_facts_abstains_when_the_budget_drops_its_evidence() -> None:
    """The retrieval verdict passed but the prompt budget admitted no passage.
    A turn with no tenant fact to fall back on must not answer from the empty
    context the model would actually receive.
    """
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview Care Plan")
    oversized = "The Care Plan membership includes an annual tune-up. " * 400
    client, model, _, _ = _client(
        script=[ModelResponse(content="should never run", model_name="scripted")],
        chunks=(
            _chunk(
                "clearview-care-plan-1",
                oversized,
                document_id=version.document_id,
                version_id=version.version_id,
            ),
        ),
        knowledge=knowledge,
    )
    headers = _open_session(client)

    response = client.post(
        "/api/chat",
        json={"message": "What is included in the Care Plan membership?"},
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json()["reply"].startswith("I do not have approved material")
    assert model.calls == []


def test_a_non_general_agent_keeps_answering_without_evidence() -> None:
    """Only the general-knowledge agent is gated by evidence: a booking turn
    answers from tool results and the workflow, exactly as before RAG-005."""
    client, model, _, _ = _client(
        script=[ModelResponse(content="Let me check availability.", model_name="scripted")],
        evidence=_FailingEvidenceSource(),
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": "Book HVAC"}, headers=headers)

    assert response.status_code == 200
    assert response.json()["reply"] == "Let me check availability."
    assert len(model.calls) == 1


def _service_area_script(zip_code: str, answer: str) -> list[ModelResponse]:
    """The model checks a ZIP with the tool, then states the result."""
    return [
        ModelResponse(
            content="",
            model_name="scripted",
            tool_calls=(
                ToolCall(
                    call_id="call-area-1",
                    name="check_service_area",
                    arguments={"zip": zip_code},
                ),
            ),
        ),
        ModelResponse(content=answer, model_name="scripted"),
    ]


def test_a_service_area_answer_its_own_tool_confirmed_is_published() -> None:
    """A tool-confirmed service-area answer must be published.

    No approved document records which ZIPs a tenant serves, so claim
    validation had nothing to match and withheld a true answer the graph had
    just computed. `97205` is in the clearview tenant's served set.
    """
    client, _model, _knowledge, _turns = _client(
        script=_service_area_script("97205", "Yes, we serve ZIP code 97205.")
    )
    with client:
        headers = _open_session(client)
        replied = client.post("/api/chat", json={"message": "Do you serve 97205?"}, headers=headers)

    assert replied.status_code == 200, replied.text
    assert "97205" in replied.json()["reply"]
    assert "cannot confirm" not in replied.json()["reply"]


def test_a_service_area_answer_contradicting_its_tool_is_still_refused() -> None:
    """The tool is the authority, not the model: `98999` is not served."""
    client, _model, _knowledge, _turns = _client(
        script=_service_area_script("98999", "Yes, we serve ZIP code 98999.")
    )
    with client:
        headers = _open_session(client)
        replied = client.post("/api/chat", json={"message": "Do you serve 98999?"}, headers=headers)

    assert replied.status_code == 200, replied.text
    assert "Yes, we serve ZIP code 98999." not in replied.json()["reply"]


def test_a_tool_answered_service_area_reply_cites_no_document() -> None:
    """An answer grounded by a tool must not cite an unrelated document.

    The financing document's lead-capture paragraph mentions "ZIP code", which
    is enough for the model to cite it beside "yes, we serve 97205" — and enough
    to pass a relevance score, since that pairing outscores citations that are
    genuinely earned. Provenance has to name the tool that decided it.
    """
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview financing options")
    # The live chunk the model reached for: it mentions "ZIP code" only to say
    # which details a lead-capture form collects.
    chunk = _chunk(
        "clearview-financing-5",
        "If a customer wants to discuss financing, collect their name, contact "
        "information, service type, address or ZIP code, and a short project summary.",
        document_id=version.document_id,
        version_id=version.version_id,
    )
    client, _model, _knowledge, _turns = _client(
        script=[
            ModelResponse(
                content="",
                model_name="scripted",
                tool_calls=(
                    ToolCall(
                        call_id="call-area-2",
                        name="check_service_area",
                        arguments={"zip": "97205"},
                    ),
                ),
            ),
            ModelResponse(
                content=f"Yes, we serve ZIP code 97205. [evidence:{chunk.chunk_id}]",
                model_name="scripted",
            ),
        ],
        chunks=(chunk,),
        knowledge=knowledge,
    )
    with client:
        headers = _open_session(client)
        replied = client.post("/api/chat", json={"message": "Do you serve 97205?"}, headers=headers)

    assert replied.status_code == 200, replied.text
    body = replied.json()
    assert "97205" in body["reply"], "the true answer must still be published"
    assert body["citations"] == [], f"a tool-answered reply cited {body['citations']}"
