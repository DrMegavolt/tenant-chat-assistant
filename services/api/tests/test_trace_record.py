"""The `OBS-004` turn record as the visitor's session drives it: one governed
envelope per turn, with the component manifest, the idempotency keys, the
reconstruction contract, and the failure attribution.

These drive the real chat route end to end, so the trace's claims about content
freedom, idempotency keys, and failure attribution are checked against what the
record store actually keeps — not against a hand-built checkpoint.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from services.api.tests.conftest import BOOKING_TENANT, ScriptedModel, booking_call
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
    TurnRecord,
)
from tenantchat.core.knowledge import (
    ContentChecksum,
    DocumentVersion,
    KnowledgeDomain,
    SourceKind,
)
from tenantchat.core.ports import EvidenceBundle, EvidenceUnavailableError
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.model import ModelResponse
from tenantchat.orchestration.trace import reconstruct_prompt

HOURS_QUESTION = "What are your hours of operation?"
HOURS_ANSWER = "We are open daily from 7 AM to 7 PM."


def _settings() -> Settings:
    return Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=2048,
        docs_enabled=True,
        admin_gateway_token="gateway-token-for-tests",
        admin_csrf_secret="csrf-secret-for-tests",
        dev_auth=False,
    )


class _UniformEmbedder:
    """Every text embeds to the same vector, so lexical signal decides."""

    async def ready(self) -> None:
        return None

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors = [(1.0, 0.0, 0.0, 0.0)] * len(texts)
        return EmbeddingResult(model="scripted-embedder.v1", dimensions=4, vectors=vectors)


class _FailingEvidenceSource:
    """An evidence port whose retrieval is down: the graph must abstain."""

    async def retrieve(self, *, tenant_id: str, query: str) -> EvidenceBundle:
        raise EvidenceUnavailableError("index unreachable")


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
    """Stage, approve, publish, and index one version of a document."""
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
) -> IndexedChunk:
    return IndexedChunk(
        chunk_id=chunk_id,
        tenant_id=BOOKING_TENANT,
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
) -> tuple[TestClient, ScriptedModel, InMemoryTurnRecordStore]:
    """An app over seeded knowledge, with the `OBS-004` record store wired."""
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
    return TestClient(app, raise_server_exceptions=False), model, turn_records


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


def _turn_records(store: InMemoryTurnRecordStore, session_id: str) -> tuple[TurnRecord, ...]:
    return asyncio.run(store.for_session(BOOKING_TENANT, uuid.UUID(session_id), limit=10))


def _section(content: Mapping[str, object], key: str) -> dict[str, object]:
    """One trace section as the record holds it."""
    value = content[key]
    assert isinstance(value, Mapping)
    return dict(value)


def _list(content: Mapping[str, object], key: str) -> list[dict[str, object]]:
    """One trace list of records as the record holds it."""
    value = content[key]
    assert isinstance(value, list)
    return [dict(item) for item in value if isinstance(item, Mapping)]


def test_the_turn_record_pins_every_component_and_a_content_free_hash() -> None:
    """One answered turn: manifest values, the hash, and the raw output it owns."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    client, _, records = _client(
        script=[
            ModelResponse(
                content=f"{HOURS_ANSWER}. [evidence:clearview-hvac-2]",
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
    assert response.status_code == 200

    (record,) = _turn_records(records, response.json()["session_id"])
    content = record.content
    assert content["outcome"] == {"status": "answered", "rounds": 1, "failure": None}
    assert _section(content, "model")["name"] == "scripted"
    assert _section(content, "output")["raw"] == f"{HOURS_ANSWER}. [evidence:clearview-hvac-2]"
    assert _section(content, "output")["claims"] == ["clearview-hvac-2"]
    assert re.fullmatch(r"[0-9a-f]{64}", str(content["manifest_hash"]))
    manifest = _section(content, "component_manifest")
    assert manifest["graph"] == "dispatch@3"
    assert manifest["prompt_template"] == {"ref": "dispatch-system@4"}
    assert manifest["routing_policy"] == "intent-routing@1"
    assert manifest["tools"] == "tools@1"
    assert manifest["retriever"] == {
        "version": "v1",
        "reranker": "bigram-overlap",
        "min_evidence_score": 0.5,
        "embedding_model": "scripted-embedder.v1",
        "generation_id": _section(content, "retrieval")["generation_id"],
        "parameters": _section(content, "retrieval")["parameters"],
        "filters": _section(content, "retrieval")["filters"],
        "budget": _section(content, "retrieval")["budget"],
    }
    # The derived columns agree with the envelope: this is the query surface.
    assert record.outcome == "answered"
    assert record.component_manifest_hash == content["manifest_hash"]
    assert record.diagnosis_causes == ()
    assert record.turn_index == 1
    assert record.trace_schema_version == "3"
    # A post-`OBS-006` turn records the executed graph that actually ran.
    executed = _section(content, "executed_graph")
    assert [node["name"] for node in _list(executed, "nodes")] == [
        "route",
        "model",
        "finalize",
    ]


def test_the_manifest_hash_is_content_free_but_component_sensitive() -> None:
    """Same components and any wording, same hash; another model, another hash."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    chunk = _chunk(
        "clearview-hvac-2",
        "Clearview is open daily from 7 AM to 7 PM. Hours of operation " "are seven days a week.",
        document_id=version.document_id,
        version_id=version.version_id,
    )

    def answer(model_name: str, wording: str) -> str:
        client, _, records = _client(
            script=[ModelResponse(content=wording, model_name=model_name)],
            chunks=(chunk,),
            knowledge=knowledge,
        )
        headers = _open_session(client)
        response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)
        (record,) = _turn_records(records, response.json()["session_id"])
        return str(record.content["manifest_hash"])

    assert answer("scripted", "We are open daily.") == answer("scripted", "Yes, daily.")
    assert answer("scripted", "We are open daily.") != answer("other-model", "We are open daily.")


def test_a_turn_is_reconstructible_from_its_record_alone() -> None:
    """The reconstruction contract end to end: from a stored record, rebuild the
    exact prompt the provider received (re-deriving the content hash), replay it
    into the same model, and get the same answer — no checkpoint involved."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    client, _, records = _client(
        script=[
            ModelResponse(
                content=f"{HOURS_ANSWER}. [evidence:clearview-hvac-2]",
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
    (record,) = _turn_records(records, response.json()["session_id"])

    stored_prompt = _section(record.content, "prompt")
    rebuilt = reconstruct_prompt(stored_prompt)
    assert rebuilt.content_hash == stored_prompt["content_hash"]
    assert rebuilt.template_ref == "dispatch-system@4"
    assert "Clearview is open daily" in rebuilt.messages[0].content

    replay_model = ScriptedModel(
        [
            ModelResponse(
                content=f"{HOURS_ANSWER}. [evidence:clearview-hvac-2]", model_name="scripted"
            )
        ]
    )
    replayed = asyncio.run(replay_model.complete(rebuilt, tools=()))

    assert replayed.content == _section(record.content, "output")["raw"]
    assert _section(record.content, "output")["answer"] == f"{HOURS_ANSWER}."


def test_an_escalation_is_recorded_without_a_model_call_or_a_diagnosis() -> None:
    """A visitor-requested handoff is an outcome, not a failure to attribute."""
    client, model, records = _client(
        script=[ModelResponse(content="should never run", model_name="scripted")]
    )
    headers = _open_session(client)

    response = client.post(
        "/api/chat", json={"message": "I need to speak to a person"}, headers=headers
    )

    assert response.status_code == 200
    assert model.calls == []
    (record,) = _turn_records(records, response.json()["session_id"])
    content = record.content
    assert content["outcome"] == {"status": "escalated", "rounds": 0, "failure": "customer_request"}
    assert content["diagnoses"] == []
    assert record.diagnosis_causes == ()


def test_an_insufficient_retrieval_answers_from_tenant_business_facts() -> None:
    """When retrieval is insufficient, the model runs and may answer from the
    tenant's own business facts (hours, phone, address) bound into the prompt.
    The trace records the retrieval miss and the evidence-sufficiency verdict."""
    client, model, records = _client(
        script=[ModelResponse(content="We are open daily.", model_name="scripted")]
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    assert len(model.calls) == 1
    (record,) = _turn_records(records, response.json()["session_id"])
    content = record.content
    assert _section(content, "outcome")["status"] == "answered"
    assert _section(content, "model")["name"] == "scripted"
    assert record.outcome == "answered"
    assert _section(content, "retrieval")["sufficient"] is False


def test_a_down_index_is_recorded_as_an_ingestion_or_index_error() -> None:
    """An unavailable retriever is distinguishable from an empty one in the record."""
    client, _, records = _client(
        script=[ModelResponse(content="should never run", model_name="scripted")],
        evidence=_FailingEvidenceSource(),
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    (record,) = _turn_records(records, response.json()["session_id"])
    content = record.content
    assert _section(content, "retrieval")["retriever_version"] == "unavailable"
    assert _list(content, "diagnoses")[0]["cause"] == "ingestion_or_index_error"
    assert _list(content, "diagnoses")[0]["confidence"] == "high"
    assert record.diagnosis_causes == ("ingestion_or_index_error",)


def test_weak_evidence_that_passed_the_verdict_is_attributable_from_the_record() -> None:
    """A wrong-but-grounded answer is attributable, not hidden: the trace shows
    the weak candidates and the verdict that let them through, and no anomaly
    diagnosis is invented for a verdict within policy."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    client, _, records = _client(
        script=[
            ModelResponse(
                content="We are open daily. [evidence:clearview-hvac-2]",
                model_name="scripted",
            )
        ],
        chunks=(
            # Only "hours" overlaps the query's two terms, so the lexical
            # score is 0.5 — just above the lowered boundary.
            _chunk(
                "clearview-hvac-2",
                "Hours are a thing every business has.",
                document_id=version.document_id,
                version_id=version.version_id,
            ),
        ),
        knowledge=knowledge,
        config=HybridRetrieverConfig(min_evidence_score=0.45, k=1),
    )
    headers = _open_session(client)

    response = client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)

    assert response.status_code == 200
    (record,) = _turn_records(records, response.json()["session_id"])
    retrieval = _section(record.content, "retrieval")
    (candidate,) = _list(retrieval, "candidates")
    assert candidate["source_id"] == "clearview-hvac-2"
    assert candidate["score"] == 0.5
    assert retrieval["sufficient"] is True
    assert _section(record.content, "outcome")["status"] == "answered"
    assert record.content["diagnoses"] == []


def test_a_booking_commit_records_the_idempotency_key_in_the_trace() -> None:
    """The trace names the side effect and the key that makes it replay-safe."""
    client, model, records = _client(
        script=[
            ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
            ModelResponse(content="You are booked for Monday at 2pm.", model_name="scripted"),
        ]
    )
    headers = _open_session(client)
    proposed = client.post("/api/chat", json={"message": "Book HVAC"}, headers=headers)
    assert proposed.status_code == 200
    session_id = proposed.json()["session_id"]

    confirmed = client.post(
        "/api/chat/confirmation", json={"decision": "approved"}, headers=headers
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["committed"][0]["replayed"] is False

    proposal, confirmation = _turn_records(records, session_id)
    assert _section(proposal.content, "outcome")["status"] == "paused"
    assert proposal.outcome == "paused"
    assert _list(_section(proposal.content, "tools"), "tool_calls")[0]["name"] == "book_appointment"
    confirmation_trace = confirmation.content
    assert _section(confirmation_trace, "outcome") == {
        "status": "answered",
        "rounds": 2,
        "failure": None,
    }
    committed = _list(_section(confirmation_trace, "tools"), "committed")
    assert [action["action"] for action in committed] == ["book_appointment"]
    assert committed[0]["replayed"] is False
    assert str(committed[0]["reference"]).startswith("BK-")
    assert committed[0]["idempotency_key"] != ""


def test_a_declined_booking_commits_nothing_in_the_trace() -> None:
    """No commit, no idempotency key: the record proves the visitor said no."""
    client, _, records = _client(
        script=[
            ModelResponse(content="", tool_calls=(booking_call(),), model_name="scripted"),
            ModelResponse(content="No problem, nothing is booked.", model_name="scripted"),
        ]
    )
    headers = _open_session(client)
    proposed = client.post("/api/chat", json={"message": "Book HVAC"}, headers=headers)
    assert proposed.status_code == 200
    session_id = proposed.json()["session_id"]

    declined = client.post("/api/chat/confirmation", json={"decision": "declined"}, headers=headers)
    assert declined.status_code == 200

    proposal, confirmation = _turn_records(records, session_id)
    assert _section(proposal.content, "outcome")["status"] == "paused"
    assert _section(confirmation.content, "outcome")["status"] == "answered"
    assert _list(_section(confirmation.content, "tools"), "committed") == []
    assert confirmation.content["diagnoses"] == []


def test_no_trace_content_reaches_the_operational_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The `ADR-0010` split in practice: a turn whose answer and evidence carry
    content produces log lines that carry none of it."""
    knowledge = InMemoryKnowledgeStore()
    version = _published_version(knowledge, title="Clearview hours")
    app_client, _, _ = _client(
        script=[
            ModelResponse(
                content=f"{HOURS_ANSWER}. [evidence:clearview-hvac-2]",
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
    with caplog.at_level(logging.INFO):
        headers = _open_session(app_client)
        response = app_client.post("/api/chat", json={"message": HOURS_QUESTION}, headers=headers)
        assert response.status_code == 200

    joined = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (
        HOURS_QUESTION,
        HOURS_ANSWER,
        "Clearview is open daily",
        "[evidence:clearview-hvac-2]",
        "hours of operation",
    ):
        assert secret not in joined
