"""The `FEAT-008` end-to-end surface: visitor feedback, the automatic queue,
the review decision with diagnosis disagreement, correction immutability, safe
promotion, and fix-closure linkage.

The verification the backlog demands runs here as one arc: a visitor thumbs
down an answer and it enters the queue; a detector-proven technical failure
enters without any thumbs-down; a reviewer takes the case, amends the
automatic diagnosis instead of overwriting it, writes a corrected answer that
never touches the trace, promotes the anonymized case only after the privacy
check, and the first evaluation report that passes the case closes it — while
the original diagnosis and answer survive every step.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterator, Mapping, Sequence
from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.api.tests.conftest import (
    BOOKING_TENANT,
    LEAD_TENANT,
    TEST_GATEWAY_TOKEN,
    ScriptedModel,
)
from services.api.tests.test_trace_record import _chunk, _published_version
from tenantchat.api.app import create_app
from tenantchat.api.evidence import RetrievalEvidenceSource
from tenantchat.api.index_integrity import InMemoryIndexIntegrityStore
from tenantchat.api.retrieval import HybridRetrieverConfig
from tenantchat.api.review import apply_eval_report, enqueue_automatic
from tenantchat.api.search import EmbeddingResult, InMemorySearchIndex
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
    InMemoryReviewQueueStore,
    InMemoryTraceAccessStore,
    InMemoryTurnFeedbackStore,
    InMemoryTurnRecordStore,
    TurnRecord,
)
from tenantchat.orchestration.checkpoints import InMemorySaver

TRACE_TENANT = BOOKING_TENANT
OTHER_TENANT = LEAD_TENANT
SUBJECT = "operator-7"

# A valid manifest shape; the content-free hash value itself is arbitrary but
# must be a 64-hex string for the store's CHECK constraint in PostgreSQL.
MANIFEST_A = "a" * 64
MANIFEST_B = "b" * 64


def _operator(headers: dict[str, str]) -> dict[str, str]:
    return {
        "X-TenantChat-Gateway-Token": TEST_GATEWAY_TOKEN,
        "X-Auth-Subject": SUBJECT,
        "X-Auth-Email": "operator@example.com",
        "X-Auth-Role": "support_agent",
    } | headers


def _csrf(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def _diagnosis(
    cause: str,
    *,
    status: str = "detected",
    confidence: str = "high",
    stage: str = "validation",
) -> dict[str, object]:
    return {
        "cause": cause,
        "stage": stage,
        "role": "primary",
        "status": status,
        "confidence": confidence,
        "evidence": [],
        "detector_version": "diagnosis@1",
    }


def _seeded_content(
    *,
    diagnoses: Sequence[Mapping[str, object]],
    query: str = "What are your hours?",
    evidence: Sequence[str] = ("clearview-hvac-2",),
    claims: Sequence[str] = ("clearview-hvac-2",),
    committed: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    return {
        "routing": {"rule": "answer", "intent": "general"},
        "retrieval": {
            "query": query,
            "sufficient": True,
            "retriever_version": "v1",
            "evidence": [
                {"source_id": source_id, "score": 0.9, "generation_id": "gen-1"}
                for source_id in evidence
            ],
        },
        "prompt": None,
        "model": {"name": "scripted", "usage": {}},
        "output": {
            "answer": "We are open daily from 7 AM to 7 PM.",
            "raw": "We are open daily from 7 AM to 7 PM.",
            "claims": list(claims),
        },
        "verdicts": {"citation_invalid": [], "refused_tools": [], "claims_invalid": []},
        "tools": {"tool_calls": [], "tool_results": [], "committed": list(committed)},
        "outcome": {"status": "answered", "rounds": 1, "failure": None},
        "diagnoses": [dict(diagnosis) for diagnosis in diagnoses],
    }


async def _seed_turn(
    turns: InMemoryTurnRecordStore,
    tenant_id: str,
    session_id: uuid.UUID,
    *,
    diagnoses: Sequence[Mapping[str, object]],
    manifest_hash: str = MANIFEST_A,
    query: str = "What are your hours?",
    evidence: Sequence[str] = ("clearview-hvac-2",),
    claims: Sequence[str] = ("clearview-hvac-2",),
    committed: Sequence[Mapping[str, object]] = (),
) -> TurnRecord:
    return await turns.record(
        tenant_id,
        session_id,
        content=_seeded_content(
            diagnoses=diagnoses,
            query=query,
            evidence=evidence,
            claims=claims,
            committed=committed,
        ),
        trace_id="trace-feedback-test",
        outcome="answered",
        component_manifest_hash=manifest_hash,
        diagnosis_causes=tuple(str(d.get("cause", "")) for d in diagnoses),
        diagnosis_statuses=tuple(str(d.get("status", "")) for d in diagnoses),
        turn_index=1,
    )


def _review_app(
    *,
    model: ScriptedModel | None = None,
    chunks: Sequence[object] = (),
    knowledge: InMemoryKnowledgeStore | None = None,
) -> tuple[
    TestClient,
    InMemoryTurnRecordStore,
    InMemoryTraceAccessStore,
    InMemoryAuditStore,
    InMemoryTurnFeedbackStore,
    InMemoryReviewQueueStore,
]:
    settings = Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=65536,
        docs_enabled=False,
        admin_gateway_token=TEST_GATEWAY_TOKEN,
        admin_csrf_secret="csrf-secret-for-review-tests",
        visitor_credential_signing_key="visitor-signing-key-for-review-tests-" + "x" * 16,
    )
    conversations = InMemoryConversationStore()
    consent = InMemoryConsentStore()
    turns = InMemoryTurnRecordStore()
    grants = InMemoryTraceAccessStore()
    audit = InMemoryAuditStore()
    feedback = InMemoryTurnFeedbackStore()
    reviews = InMemoryReviewQueueStore()
    membership = InMemoryMembershipStore()
    for tenant_id in (TRACE_TENANT, OTHER_TENANT):
        asyncio.run(membership.assign(tenant_id=tenant_id, subject=SUBJECT, role="support_agent"))
    index = InMemorySearchIndex()
    if chunks:
        asyncio.run(index.index_chunks(list(chunks)))  # type: ignore[arg-type]
    knowledge = knowledge or InMemoryKnowledgeStore()
    evidence = RetrievalEvidenceSource(
        index=index,
        embedder=_UniformEmbedder(),
        knowledge=knowledge,
        config=HybridRetrieverConfig(),
    )
    app = create_app(
        settings,
        booking_store=InMemoryBookingStore(),
        lead_store=InMemoryLeadStore(),
        conversation_store=conversations,
        handoff_store=InMemoryHandoffStore(),
        idempotency_store=InMemoryIdempotencyStore(),
        membership_store=membership,
        consent_store=consent,
        privacy_store=InMemoryPrivacyStore(
            conversations,
            InMemoryBookingStore(),
            InMemoryLeadStore(),
            InMemoryHandoffStore(),
            consent,
            turn_records=turns,
        ),
        audit_store=audit,
        turn_record_store=turns,
        trace_access_store=grants,
        feedback_store=feedback,
        review_store=reviews,
        chat_model=model,
        checkpointer=InMemorySaver(),
        knowledge_store=knowledge,
        generation_findings=InMemoryIndexIntegrityStore(),
        object_store=MemoryObjectStore(),
        search_index=index,
        evidence_source=evidence if model is not None else None,
    )
    asyncio.run(grants.grant(TRACE_TENANT, SUBJECT, granted_by="platform-admin-1"))
    return TestClient(app, raise_server_exceptions=False), turns, grants, audit, feedback, reviews


class _UniformEmbedder:
    """Every text embeds to the same vector; only the envelope is read here."""

    async def embed(self, texts: Sequence[str]) -> EmbeddingResult:
        vectors = [(1.0, 0.0, 0.0, 0.0)] * len(texts)
        return EmbeddingResult(model="scripted-embedder.v1", dimensions=4, vectors=vectors)


def _retrieval_evidence() -> RetrievalEvidenceSource:
    return RetrievalEvidenceSource(
        index=InMemorySearchIndex(),
        embedder=_UniformEmbedder(),
        knowledge=InMemoryKnowledgeStore(),
        config=HybridRetrieverConfig(),
    )


@pytest.fixture
def review_app() -> (
    Iterator[
        tuple[
            TestClient,
            InMemoryTurnRecordStore,
            InMemoryTraceAccessStore,
            InMemoryAuditStore,
            InMemoryTurnFeedbackStore,
            InMemoryReviewQueueStore,
        ]
    ]
):
    client, turns, grants, audit, feedback, reviews = _review_app()
    with client:
        yield client, turns, grants, audit, feedback, reviews


def _open_session(client: TestClient, tenant_id: str = TRACE_TENANT) -> dict[str, str]:
    response = client.post("/api/chat/session", json={"tenant_id": tenant_id})
    assert response.status_code == 201, response.text
    return {
        "X-Visitor-Credential": str(response.json()["credential"]),
        "session_id": str(response.json()["session"]["session_id"]),
    }


def _admin_headers(client: TestClient) -> dict[str, str]:
    headers = _operator({})
    headers["X-CSRF-Token"] = _csrf(client, headers)
    return headers


class TestFeedbackCapture:
    def test_feedback_requires_a_turn_from_the_credentialed_conversation(
        self, review_app: tuple[Any, ...]
    ) -> None:
        """Acceptance 1: a borrowed or guessed turn id cannot attach feedback
        to another conversation — and the refusal is the same 404 whether the
        turn never existed or belongs to someone else."""
        client, turns, _grants, _audit, feedback, _reviews = review_app
        alice = _open_session(client)
        bob = _open_session(client)
        turn = asyncio.run(
            turns.record(
                TRACE_TENANT,
                uuid.UUID(alice["session_id"]),
                content={"diagnoses": []},
            )
        )

        # A valid credential for a different conversation: the same 404 an
        # absent turn produces, so the endpoint cannot be probed.
        response = client.post(
            "/api/chat/feedback",
            headers={"X-Visitor-Credential": bob["X-Visitor-Credential"]},
            json={"turn_id": str(turn.turn_id), "rating": "down"},
        )
        assert response.status_code == 404, response.text
        assert response.json()["code"] == "not_found"
        # A forged credential is rejected before the turn is even looked up.
        response = client.post(
            "/api/chat/feedback",
            headers={"X-Visitor-Credential": "forged" + alice["X-Visitor-Credential"]},
            json={"turn_id": str(turn.turn_id), "rating": "down"},
        )
        assert response.status_code == 401, response.text
        assert asyncio.run(feedback.for_turn(TRACE_TENANT, turn.turn_id)) is None

    def test_a_thumbs_down_records_and_opens_a_user_feedback_case(
        self, review_app: tuple[Any, ...]
    ) -> None:
        client, turns, _grants, _audit, feedback, reviews = review_app
        visitor = _open_session(client)
        turn = asyncio.run(
            _seed_turn(
                turns,
                TRACE_TENANT,
                uuid.UUID(visitor["session_id"]),
                diagnoses=(_diagnosis("grounding_or_citation_error"),),
            )
        )

        response = client.post(
            "/api/chat/feedback",
            headers={"X-Visitor-Credential": visitor["X-Visitor-Credential"]},
            json={"turn_id": str(turn.turn_id), "rating": "down", "reason": "Wrong price"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["rating"] == "down"
        assert response.json()["reason"] == "Wrong price"

        recorded = asyncio.run(feedback.for_turn(TRACE_TENANT, turn.turn_id))
        assert recorded is not None and recorded.rating == "down"
        case = asyncio.run(reviews.for_turn(TRACE_TENANT, turn.turn_id))
        assert case is not None
        assert case.source == "user_feedback"
        assert case.status == "open"

    def test_a_thumbs_up_records_without_enqueueing(self, review_app: tuple[Any, ...]) -> None:
        client, turns, _grants, _audit, feedback, reviews = review_app
        visitor = _open_session(client)
        turn = asyncio.run(
            _seed_turn(turns, TRACE_TENANT, uuid.UUID(visitor["session_id"]), diagnoses=())
        )

        response = client.post(
            "/api/chat/feedback",
            headers={"X-Visitor-Credential": visitor["X-Visitor-Credential"]},
            json={"turn_id": str(turn.turn_id), "rating": "up"},
        )
        assert response.status_code == 200, response.text
        assert asyncio.run(reviews.for_turn(TRACE_TENANT, turn.turn_id)) is None

    def test_rerating_replaces_the_earlier_rating(self, review_app: tuple[Any, ...]) -> None:
        """One feedback row per turn: a second rating is an update, not a stack."""
        client, turns, _grants, _audit, feedback, _reviews = review_app
        visitor = _open_session(client)
        turn = asyncio.run(
            _seed_turn(turns, TRACE_TENANT, uuid.UUID(visitor["session_id"]), diagnoses=())
        )
        headers = {"X-Visitor-Credential": visitor["X-Visitor-Credential"]}

        client.post(
            "/api/chat/feedback",
            headers=headers,
            json={"turn_id": str(turn.turn_id), "rating": "down"},
        )
        response = client.post(
            "/api/chat/feedback",
            headers=headers,
            json={"turn_id": str(turn.turn_id), "rating": "up"},
        )
        assert response.status_code == 200, response.text
        recorded = asyncio.run(feedback.for_turn(TRACE_TENANT, turn.turn_id))
        assert recorded is not None and recorded.rating == "up"


class TestAutomaticEnqueue:
    def test_a_proven_technical_failure_enters_the_queue_without_a_thumbs_down(
        self, review_app: tuple[Any, ...]
    ) -> None:
        """Acceptance 3: the detector proved a provider failure, so the turn
        gains a queue case with no visitor feedback at all."""
        client, turns, _grants, _audit, _feedback, reviews = review_app
        visitor = _open_session(client)
        turn = asyncio.run(
            _seed_turn(
                turns,
                TRACE_TENANT,
                uuid.UUID(visitor["session_id"]),
                diagnoses=(_diagnosis("provider_failure", status="confirmed"),),
                manifest_hash=MANIFEST_A,
            )
        )
        # The exact call the chat surface makes after recording a turn.
        case = asyncio.run(enqueue_automatic(reviews, TRACE_TENANT, turn))

        assert case is not None
        assert case.source == "automatic"
        assert case.priority == 32  # 10*3 severity + 0 recurrence + 2 novel
        assert case.recurrence == 1
        assert case.novel_manifest is True

    def test_a_failing_model_turn_enqueues_end_to_end(self, review_app: tuple[Any, ...]) -> None:
        """A real chat turn whose model call fails records a provider-failure
        diagnosis and enters the queue — no reviewer and no thumbs-down."""
        del review_app
        knowledge = InMemoryKnowledgeStore()
        version = _published_version(knowledge, title="Clearview hours")
        chunk = _chunk(
            "clearview-hvac-2",
            "Clearview is open daily from 7 AM to 7 PM. Hours of operation "
            "are seven days a week.",
            document_id=version.document_id,
            version_id=version.version_id,
        )
        client, _turns, _grants, _audit, _feedback, reviews = _review_app(
            model=ScriptedModel([]), chunks=(chunk,), knowledge=knowledge
        )
        with client:
            visitor = _open_session(client)
            response = client.post(
                "/api/chat",
                headers={"X-Visitor-Credential": visitor["X-Visitor-Credential"]},
                json={"message": "What are your hours?"},
            )
            assert response.status_code == 200, response.text

        cases = asyncio.run(reviews.search(TRACE_TENANT, limit=10))
        assert len(cases) == 1
        assert cases[0].source == "automatic"
        assert cases[0].status == "open"
        # The failing turn handed off to a human — a committed business action —
        # so the committed weight sits on top of the novel provider-failure score.
        assert cases[0].priority == 35
        assert cases[0].committed_actions is True

    def test_a_recurring_manifest_outranks_its_first_appearance(
        self, review_app: tuple[Any, ...]
    ) -> None:
        client, turns, _grants, _audit, _feedback, reviews = review_app
        visitor = _open_session(client)
        first = asyncio.run(
            _seed_turn(
                turns,
                TRACE_TENANT,
                uuid.UUID(visitor["session_id"]),
                diagnoses=(_diagnosis("tool_error"),),
                manifest_hash=MANIFEST_A,
            )
        )
        second = asyncio.run(
            _seed_turn(
                turns,
                TRACE_TENANT,
                uuid.UUID(visitor["session_id"]),
                diagnoses=(_diagnosis("tool_error"),),
                manifest_hash=MANIFEST_A,
            )
        )
        asyncio.run(enqueue_automatic(reviews, TRACE_TENANT, first))
        asyncio.run(enqueue_automatic(reviews, TRACE_TENANT, second))

        first_case = asyncio.run(reviews.for_turn(TRACE_TENANT, first.turn_id))
        second_case = asyncio.run(reviews.for_turn(TRACE_TENANT, second.turn_id))
        assert first_case is not None and first_case.priority == 22  # novel
        assert second_case is not None
        assert second_case.priority == 25  # 10*2 severity + 5 recurrence, not novel
        assert second_case.recurrence == 2
        assert second_case.novel_manifest is False

    def test_a_committed_failure_outranks_an_equivalent_browse(
        self, review_app: tuple[Any, ...]
    ) -> None:
        client, turns, _grants, _audit, _feedback, reviews = review_app
        visitor = _open_session(client)
        turn = asyncio.run(
            _seed_turn(
                turns,
                TRACE_TENANT,
                uuid.UUID(visitor["session_id"]),
                diagnoses=(_diagnosis("provider_failure", status="confirmed"),),
                manifest_hash=MANIFEST_A,
                committed=({"action": "book", "reference": "BK-1"},),
            )
        )
        case = asyncio.run(enqueue_automatic(reviews, TRACE_TENANT, turn))
        assert case is not None

        assert case.committed_actions is True
        assert case.priority == 35  # the committed-action weight on top of 32

    def test_an_answered_turn_without_a_diagnosis_never_enqueues(
        self, review_app: tuple[Any, ...]
    ) -> None:
        client, turns, _grants, _audit, _feedback, reviews = review_app
        visitor = _open_session(client)
        turn = asyncio.run(
            _seed_turn(turns, TRACE_TENANT, uuid.UUID(visitor["session_id"]), diagnoses=())
        )
        assert asyncio.run(enqueue_automatic(reviews, TRACE_TENANT, turn)) is None
        assert asyncio.run(reviews.for_turn(TRACE_TENANT, turn.turn_id)) is None

    def test_a_suspected_cause_is_not_an_automatic_enqueue(
        self, review_app: tuple[Any, ...]
    ) -> None:
        client, turns, _grants, _audit, _feedback, reviews = review_app
        visitor = _open_session(client)
        turn = asyncio.run(
            _seed_turn(
                turns,
                TRACE_TENANT,
                uuid.UUID(visitor["session_id"]),
                diagnoses=(_diagnosis("provider_failure", status="suspected"),),
            )
        )
        assert asyncio.run(enqueue_automatic(reviews, TRACE_TENANT, turn)) is None


class TestReviewWorkflow:
    def _open_case(
        self,
        client: TestClient,
        turns: InMemoryTurnRecordStore,
        *,
        diagnoses: Sequence[Mapping[str, object]] = (
            _diagnosis("provider_failure", status="confirmed"),
            _diagnosis("tool_error"),
        ),
        manifest_hash: str = MANIFEST_A,
        query: str = "What are your hours?",
    ) -> uuid.UUID:
        visitor = _open_session(client)
        turn = asyncio.run(
            _seed_turn(
                turns,
                TRACE_TENANT,
                uuid.UUID(visitor["session_id"]),
                diagnoses=diagnoses,
                manifest_hash=manifest_hash,
                query=query,
            )
        )
        case = asyncio.run(
            enqueue_automatic(
                reviews_store_for(client),
                TRACE_TENANT,
                turn,
            )
        )
        assert case is not None
        return case.review_id


def reviews_store_for(client: TestClient) -> InMemoryReviewQueueStore:
    return cast(InMemoryReviewQueueStore, cast(FastAPI, client.app).state.review_store)


def test_a_reviewer_can_take_an_open_case(review_app: tuple[Any, ...]) -> None:
    client, turns, _grants, _audit, _feedback, _reviews = review_app
    review_id = TestReviewWorkflow()._open_case(client, turns)
    headers = _admin_headers(client)

    response = client.post(
        f"/api/admin/reviews/{review_id}/take?tenant_id={TRACE_TENANT}", headers=headers
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "in_review"


def test_a_second_reviewer_cannot_take_the_same_case(review_app: tuple[Any, ...]) -> None:
    client, turns, _grants, _audit, _feedback, _reviews = review_app
    review_id = TestReviewWorkflow()._open_case(client, turns)
    headers = _admin_headers(client)
    client.post(f"/api/admin/reviews/{review_id}/take?tenant_id={TRACE_TENANT}", headers=headers)
    assert (
        client.post(
            f"/api/admin/reviews/{review_id}/take?tenant_id={TRACE_TENANT}", headers=headers
        ).status_code
        == 409
    )


def test_the_submission_must_decide_every_automatic_diagnosis(review_app: tuple[Any, ...]) -> None:
    """Acceptance 4: covering only one of two automatic diagnoses is refused."""
    client, turns, _grants, _audit, _feedback, _reviews = review_app
    review_id = TestReviewWorkflow()._open_case(client, turns)
    headers = _admin_headers(client)
    response = client.post(
        f"/api/admin/reviews/{review_id}/review?tenant_id={TRACE_TENANT}",
        headers=headers,
        json={
            "tenant_id": TRACE_TENANT,
            "verdict": "confirmed",
            "status": "awaiting_fix",
            "diagnoses": [
                {
                    "automatic_index": 0,
                    "relationship": "confirms",
                    "cause": "provider_failure",
                }
            ],
        },
    )
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "validation_error"


def test_an_amendment_disagrees_without_overwriting_the_automatic_record(
    review_app: tuple[Any, ...],
) -> None:
    """Acceptance 4: the reviewer amends the detector's diagnosis; the
    detector's record inside the trace is untouched and both are served side
    by side, so the disagreement is visible rather than silently resolved."""
    client, turns, _grants, audit, _feedback, _reviews = review_app
    review_id = TestReviewWorkflow()._open_case(client, turns)
    headers = _admin_headers(client)

    response = client.post(
        f"/api/admin/reviews/{review_id}/review?tenant_id={TRACE_TENANT}",
        headers=headers,
        json={
            "tenant_id": TRACE_TENANT,
            "verdict": "amended",
            "status": "awaiting_fix",
            "note": "The provider call failed only after the retry budget was spent",
            "corrected_answer": "We are open daily from 8 AM to 6 PM.",
            "proposed_fix": "Raise the provider retry budget before escalation.",
            "diagnoses": [
                {
                    "automatic_index": 0,
                    "relationship": "confirms",
                    "cause": "provider_failure",
                },
                {
                    "automatic_index": 1,
                    "relationship": "amends",
                    "cause": "tool_error",
                    "stage": "tools",
                    "status": "confirmed",
                    "note": "The booking tool itself refused, not the provider",
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "awaiting_fix"
    assert response.json()["verdict"] == "amended"
    assert response.json()["corrected_answer"] == "We are open daily from 8 AM to 6 PM."

    detail = client.get(
        f"/api/admin/reviews/{review_id}?tenant_id={TRACE_TENANT}&reason=quality_review",
        headers=_operator({}),
    )
    assert detail.status_code == 200, detail.text
    body = detail.json()
    relationships = [row["relationship"] for row in body["diagnoses"]]
    assert relationships == ["confirms", "amends"]

    case = asyncio.run(reviews_store_for(client).get(TRACE_TENANT, review_id))
    assert case.corrected_answer == "We are open daily from 8 AM to 6 PM."

    # The original answer and evidence survive the review unchanged.
    turn = asyncio.run(turns.get(TRACE_TENANT, case.turn_id))
    assert turn.content["output"]["answer"] == "We are open daily from 7 AM to 7 PM."
    assert [d["cause"] for d in turn.content["diagnoses"]] == [
        "provider_failure",
        "tool_error",
    ]


def test_a_review_decision_is_audited_to_the_reviewer(review_app: tuple[Any, ...]) -> None:
    client, turns, _grants, audit, _feedback, _reviews = review_app
    review_id = TestReviewWorkflow()._open_case(
        client, turns, diagnoses=(_diagnosis("tool_error"),)
    )
    headers = _admin_headers(client)
    response = client.post(
        f"/api/admin/reviews/{review_id}/review?tenant_id={TRACE_TENANT}",
        headers=headers,
        json={
            "tenant_id": TRACE_TENANT,
            "verdict": "confirmed",
            "status": "awaiting_fix",
            "diagnoses": [
                {
                    "automatic_index": 0,
                    "relationship": "confirms",
                    "cause": "tool_error",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text

    events = asyncio.run(audit.for_tenant(TRACE_TENANT))
    decisions = [event for event in events if event.action == "review.decided"]
    assert len(decisions) == 1
    assert decisions[0].principal_id == SUBJECT
    assert decisions[0].resource_id == review_id
    assert decisions[0].details["verdict"] == "confirmed"
    assert decisions[0].details["has_corrected_answer"] is False
    assert decisions[0].request_id is not None


def test_a_reviewed_case_stays_visibly_open_until_an_eval_run_passes_it(
    review_app: tuple[Any, ...],
) -> None:
    """Acceptance 5: with no evaluation run yet, the reviewed case remains
    awaiting_fix — visibly open — and carries no closing reference."""
    client, turns, _grants, _audit, _feedback, _reviews = review_app
    review_id = TestReviewWorkflow()._open_case(
        client, turns, diagnoses=(_diagnosis("tool_error"),)
    )
    headers = _admin_headers(client)
    response = client.post(
        f"/api/admin/reviews/{review_id}/review?tenant_id={TRACE_TENANT}",
        headers=headers,
        json={
            "tenant_id": TRACE_TENANT,
            "verdict": "confirmed",
            "status": "awaiting_fix",
            "proposed_fix": "Narrow the tool allowlist.",
            "diagnoses": [
                {
                    "automatic_index": 0,
                    "relationship": "confirms",
                    "cause": "tool_error",
                }
            ],
        },
    )
    assert response.status_code == 200
    assert response.json()["closing_eval_run_id"] is None
    body = client.get(
        f"/api/admin/reviews/{review_id}?tenant_id={TRACE_TENANT}&reason=quality_review",
        headers=_operator({}),
    ).json()
    assert body["review"]["status"] == "awaiting_fix"


def test_promotion_applies_the_privacy_check(review_app: tuple[Any, ...]) -> None:
    """Acceptance 6: a case whose query carries contact data is refused at
    promotion — never silently redacted."""
    client, turns, _grants, _audit, _feedback, _reviews = review_app
    review_id = TestReviewWorkflow()._open_case(
        client, turns, diagnoses=(_diagnosis("tool_error"),), query="Call 555-222-1919"
    )
    headers = _admin_headers(client)
    _submit_confirmed(client, headers, review_id)

    response = client.post(
        f"/api/admin/reviews/{review_id}/promote?tenant_id={TRACE_TENANT}", headers=headers
    )
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "promotion_privacy_refused"
    case = asyncio.run(reviews_store_for(client).get(TRACE_TENANT, review_id))
    assert case.case_id is None


def test_promotion_creates_a_privacy_checked_projection(review_app: tuple[Any, ...]) -> None:
    client, turns, _grants, _audit, _feedback, _reviews = review_app
    review_id = TestReviewWorkflow()._open_case(
        client, turns, diagnoses=(_diagnosis("tool_error"),)
    )
    headers = _admin_headers(client)
    _submit_confirmed(client, headers, review_id)

    response = client.post(
        f"/api/admin/reviews/{review_id}/promote?tenant_id={TRACE_TENANT}", headers=headers
    )
    assert response.status_code == 200, response.text
    case_id = response.json()["case_id"]
    assert case_id == f"review-{review_id}"

    case = asyncio.run(reviews_store_for(client).get(TRACE_TENANT, review_id))
    assert case.case_id == case_id
    projections = asyncio.run(turns.projections_for_turn(TRACE_TENANT, case.turn_id))
    assert len(projections) == 1
    assert projections[0].kind == "eval_dataset"
    payload = projections[0].payload
    assert payload["id"] == case_id
    assert payload["tenant_id"] == TRACE_TENANT
    assert payload["query"] == "What are your hours?"
    assert payload["gold_chunk_ids"] == ["clearview-hvac-2"]


def test_the_first_passing_eval_run_closes_the_case(review_app: tuple[Any, ...]) -> None:
    """Acceptance 5: the first report that passes the promoted case closes it
    with the run reference; a later report cannot rewrite the reference."""
    client, turns, _grants, _audit, _feedback, reviews = review_app
    review_id = TestReviewWorkflow()._open_case(
        client, turns, diagnoses=(_diagnosis("tool_error"),)
    )
    headers = _admin_headers(client)
    _submit_confirmed(client, headers, review_id)
    promotion = client.post(
        f"/api/admin/reviews/{review_id}/promote?tenant_id={TRACE_TENANT}", headers=headers
    )
    assert promotion.status_code == 200, promotion.text
    case_id = promotion.json()["case_id"]

    passing_report = {
        "components": {"min_recall": 0.6, "min_citation_precision": 0.8},
        "cases": [
            {
                "case": case_id,
                "recall": 1.0,
                "citation_precision": 1.0,
                "abstain_correct": True,
                "cross_tenant_leaks": [],
            }
        ],
    }
    closed = asyncio.run(
        apply_eval_report(reviews, TRACE_TENANT, run_id="run-2026-08-06-1", report=passing_report)
    )
    assert closed == (str(review_id),)

    case = asyncio.run(reviews.get(TRACE_TENANT, review_id))
    assert case.status == "resolved"
    assert case.closing_eval_run_id == "run-2026-08-06-1"
    assert case.closing_eval_case_id == case_id

    later_report = {
        "components": {"min_recall": 0.6, "min_citation_precision": 0.8},
        "cases": [
            {
                "case": case_id,
                "recall": 0.0,
                "citation_precision": 0.0,
                "abstain_correct": False,
                "cross_tenant_leaks": [],
            }
        ],
    }
    asyncio.run(apply_eval_report(reviews, TRACE_TENANT, run_id="run-later", report=later_report))
    reopened = asyncio.run(reviews.get(TRACE_TENANT, review_id))
    assert reopened.closing_eval_run_id == "run-2026-08-06-1"
    assert reopened.status == "resolved"

    # The original diagnosis and answer still exist after closure.
    turn = asyncio.run(turns.get(TRACE_TENANT, case.turn_id))
    assert turn.content["output"]["answer"] == "We are open daily from 7 AM to 7 PM."


def test_a_report_that_does_not_cover_a_case_leaves_it_open(review_app: tuple[Any, ...]) -> None:
    client, turns, _grants, _audit, _feedback, reviews = review_app
    review_id = TestReviewWorkflow()._open_case(
        client, turns, diagnoses=(_diagnosis("tool_error"),)
    )
    headers = _admin_headers(client)
    _submit_confirmed(client, headers, review_id)
    client.post(f"/api/admin/reviews/{review_id}/promote?tenant_id={TRACE_TENANT}", headers=headers)

    unrelated = {
        "components": {"min_recall": 0.6, "min_citation_precision": 0.8},
        "cases": [
            {
                "case": "fixture-other",
                "recall": 1.0,
                "citation_precision": 1.0,
                "abstain_correct": True,
                "cross_tenant_leaks": [],
            }
        ],
    }
    closed = asyncio.run(
        apply_eval_report(reviews, TRACE_TENANT, run_id="run-unrelated", report=unrelated)
    )
    assert closed == ()
    case = asyncio.run(reviews.get(TRACE_TENANT, review_id))
    assert case.status == "awaiting_fix"


def test_a_rejected_case_cannot_be_promoted(review_app: tuple[Any, ...]) -> None:
    client, turns, _grants, _audit, _feedback, _reviews = review_app
    review_id = TestReviewWorkflow()._open_case(
        client, turns, diagnoses=(_diagnosis("tool_error"),)
    )
    headers = _admin_headers(client)
    response = client.post(
        f"/api/admin/reviews/{review_id}/review?tenant_id={TRACE_TENANT}",
        headers=headers,
        json={
            "tenant_id": TRACE_TENANT,
            "verdict": "rejected",
            "status": "rejected",
            "note": "The tool refused by design; nothing to fix.",
            "diagnoses": [
                {
                    "automatic_index": 0,
                    "relationship": "rejects",
                    "cause": "tool_error",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    promotion = client.post(
        f"/api/admin/reviews/{review_id}/promote?tenant_id={TRACE_TENANT}", headers=headers
    )
    assert promotion.status_code == 422
    assert promotion.json()["code"] == "validation_error"


class TestQueueSurface:
    def test_the_queue_list_is_content_free(self, review_app: tuple[Any, ...]) -> None:
        """A scanned queue must not carry feedback text or answers — the
        content stays behind the audited detail surface."""
        client, turns, _grants, _audit, _feedback, _reviews = review_app
        visitor = _open_session(client)
        turn = asyncio.run(
            _seed_turn(
                turns,
                TRACE_TENANT,
                uuid.UUID(visitor["session_id"]),
                diagnoses=(_diagnosis("provider_failure", status="confirmed"),),
            )
        )
        response = client.post(
            "/api/chat/feedback",
            headers={"X-Visitor-Credential": visitor["X-Visitor-Credential"]},
            json={
                "turn_id": str(turn.turn_id),
                "rating": "down",
                "reason": "The price was wrong for my ZIP code",
            },
        )
        assert response.status_code == 200

        listing = client.get(
            f"/api/admin/reviews?tenant_id={TRACE_TENANT}&reason=quality_review",
            headers=_operator({}),
        )
        assert listing.status_code == 200, listing.text
        text = listing.text
        assert "price was wrong" not in text
        assert "7 AM to 7 PM" not in text
        (row,) = listing.json()["reviews"]
        assert row["source"] == "user_feedback"
        assert row["diagnosis_causes"] == ["provider_failure"]

    def test_the_queue_is_tenant_scoped(self, review_app: tuple[Any, ...]) -> None:
        """An operator without a trace-read grant for another tenant is
        refused identically on every review surface — the queue cannot be
        scanned cross-tenant."""
        client, turns, _grants, _audit, _feedback, _reviews = review_app
        review_id = TestReviewWorkflow()._open_case(
            client, turns, diagnoses=(_diagnosis("tool_error"),)
        )
        for path in (
            f"/api/admin/reviews?tenant_id={OTHER_TENANT}&reason=quality_review",
            f"/api/admin/reviews/{review_id}?tenant_id={OTHER_TENANT}&reason=quality_review",
        ):
            response = client.get(path, headers=_operator({}))
            assert response.status_code == 403, response.text

    def test_the_review_surface_requires_the_trace_read_role(
        self, review_app: tuple[Any, ...]
    ) -> None:
        client, turns, grants, _audit, _feedback, _reviews = review_app
        review_id = TestReviewWorkflow()._open_case(
            client, turns, diagnoses=(_diagnosis("tool_error"),)
        )
        asyncio.run(grants.revoke(TRACE_TENANT, SUBJECT))
        detail = client.get(
            f"/api/admin/reviews/{review_id}?tenant_id={TRACE_TENANT}&reason=quality_review",
            headers=_operator({}),
        )
        assert detail.status_code == 403


def _submit_confirmed(client: TestClient, headers: dict[str, str], review_id: uuid.UUID) -> None:
    response = client.post(
        f"/api/admin/reviews/{review_id}/review?tenant_id={TRACE_TENANT}",
        headers=headers,
        json={
            "tenant_id": TRACE_TENANT,
            "verdict": "confirmed",
            "status": "awaiting_fix",
            "proposed_fix": "Fix the underlying tool.",
            "diagnoses": [
                {
                    "automatic_index": 0,
                    "relationship": "confirms",
                    "cause": "tool_error",
                }
            ],
        },
    )
    assert response.status_code == 200, response.text
