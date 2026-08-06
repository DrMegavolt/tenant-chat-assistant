"""The visitor conversation surface.

Three things happen here and nowhere else in this package: a conversation is
opened, a turn is run, and a proposed booking is answered. The assistant's
behavior — which tools it may call, when it stops to ask, what it commits — lives
in the agent runtime behind
:class:`~tenantchat.core.ports.ConversationRuntime`, and every effect it causes
crosses an idempotent domain service. This module owns the part the runtime must
not: which conversation the caller is allowed to write to, and what is written
down.

**Identity is the credential, not the request body.** Every route except
``POST /api/chat/session`` reads the tenant and session from the verified
``X-Visitor-Credential`` header (SEC-002), so a forged or edited body field can
never move a conversation between tenants. ``POST /api/chat/session`` issues
the first credential for a tenant-chosen session; every later response reissues
it, so an active conversation never expires.

**The store is the record, the checkpoint is not.** Each message is appended to
the conversation store before and after the runtime runs, so deleting every
checkpoint — which `ADR-0001` requires be survivable — costs the resume point and
no transcript.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, status

from tenantchat.api.correlation import trace_id as current_trace_id
from tenantchat.api.dependencies import (
    ComposedRuntime,
    Configuration,
    Consent,
    Conversations,
    Knowledge,
    Registry,
    Runtime,
    SearchIndexes,
    TurnRecords,
)
from tenantchat.api.metrics import METRICS
from tenantchat.api.schemas import (
    BookingConfirmationRequest,
    ChatRequest,
    ChatSessionRequest,
    ChatSessionSummary,
    ChatTurnResponse,
    ConsentRequest,
    ConsentResponse,
    PendingConfirmation,
    SourceViewResponse,
    TranscriptMessage,
    VisitorSessionResponse,
)
from tenantchat.api.store import ConversationStore, MessageRole, TurnRecordStore
from tenantchat.api.visitor import VisitorClock, VisitorIdentity, VisitorSigner, issue
from tenantchat.core.errors import ConflictError, NotFoundError
from tenantchat.core.knowledge import RetrievalAudience, RetrievalContext
from tenantchat.core.metrics import (
    CitationVerdict,
    MetricName,
    Operation,
    TurnOutcome,
)
from tenantchat.core.ports import AssistantTurn
from tenantchat.core.privacy import ConsentPurpose, consent_statement
from tenantchat.core.visitor_session import VisitorCredentialSigner

router = APIRouter(tags=["chat"])

logger = logging.getLogger(__name__)


def _record_metrics(duration: float, turn: AssistantTurn) -> None:
    """The operational metrics one completed or paused turn produced.

    Latency, the outcome class, and the citation-validation verdicts only:
    the answer and its content stay in the inference plane (`ADR-0010`). The
    trace exemplar comes from the correlation context, so a spike drills
    through to the one turn that caused it.
    """
    METRICS.observe(
        MetricName.TURN_LATENCY,
        duration,
        labels={"operation": Operation.TURN.value},
    )
    if turn.is_paused:
        METRICS.observe(
            MetricName.TURN_OUTCOMES,
            1,
            labels={"outcome": TurnOutcome.PAUSED.value},
        )
    METRICS.observe(
        MetricName.CITATION_VALIDATION,
        float(len(turn.citations)),
        labels={"verdict": CitationVerdict.VALID.value},
    )
    METRICS.observe(
        MetricName.CITATION_VALIDATION,
        float(len(turn.citation_invalid)),
        labels={"verdict": CitationVerdict.INVALID.value},
    )


def _log_turn(turn: AssistantTurn) -> None:
    """The operational record of one completed turn.

    Versions and the bounded action enum only (`ADR-0010`): the answer, the
    pending question, and the model's reasoning are content and belong to the
    inference plane. The correlation context supplies the request ID, trace ID,
    and tenant pseudonym.
    """
    logger.info(
        "chat turn completed",
        extra={
            "graph_version": turn.graph_version,
            "prompt_version": turn.prompt_version,
            "committed_actions": [effect.action for effect in turn.committed],
        },
    )


async def _record_answer(
    conversations: ConversationStore,
    tenant_id: str,
    session_id: uuid.UUID,
    turn: AssistantTurn,
) -> None:
    """Append the assistant's answer, if this turn produced one.

    A paused turn has not answered anything yet, and writing its empty answer
    would leave a blank assistant message in the transcript the customer is
    looking at while they decide.

    The metadata is version and reference data only. Prompts, retrieved
    evidence, and model reasoning are content, and `ADR-0010` keeps those in the
    inference plane rather than beside the business record.
    """
    if turn.is_paused or not turn.answer:
        return
    await conversations.append(
        tenant_id,
        session_id,
        role=MessageRole.ASSISTANT,
        content=turn.answer,
        model_name=turn.model_name or None,
        metadata={
            "graph_version": turn.graph_version,
            "prompt_version": turn.prompt_version,
            "committed": [
                {"action": effect.action, "reference": effect.reference}
                for effect in turn.committed
            ],
        },
    )


def _turn_record_content(turn: AssistantTurn) -> dict[str, object]:
    """The inference-plane envelope for one completed turn (`OBS-004`).

    The trace is the whole envelope: the router decision with every candidate,
    the retrieval that ran and the candidates it admitted, the assembled
    prompt reference and content hash, model usage, the raw output and its
    parsed claims, the citation verdicts, the `RAG-007` enforcement records
    (refused tool codes and unsupported claims), the tool effects with their
    idempotency keys, the outcome, the component manifest with its content-free
    hash, and the auto-detected diagnoses. Everything here is content or
    content metadata and belongs to the trace plane; the public response
    curates from :attr:`AssistantTurn.citations` and never sees the verdicts.
    """
    return dict(turn.trace) if turn.trace is not None else {}


async def _record_turn(
    turns: TurnRecordStore,
    tenant_id: str,
    session_id: uuid.UUID,
    turn: AssistantTurn,
) -> None:
    """Persist the turn's inference-plane envelope (`OBS-004`).

    Every conversation turn earns a record, paused ones included: a proposed
    booking that is still awaiting the customer's yes is the most attributable
    thing a turn can be, and its trace names the tool call that proposed it.

    The metadata columns are copied out of the trace's own fields — never
    re-derived from content — so the derived columns can disagree with the
    envelope only if the runtime and the writer disagree about where the trace
    lives.
    """
    trace = turn.trace or {}
    outcome = trace.get("outcome")
    raw_index = trace.get("turn_index")
    diagnoses = trace.get("diagnoses")
    await turns.record(
        tenant_id,
        session_id,
        content=_turn_record_content(turn),
        trace_id=current_trace_id(),
        outcome=str(outcome.get("status", "unknown")) if isinstance(outcome, dict) else "unknown",
        component_manifest_hash=str(trace.get("manifest_hash", "")),
        diagnosis_causes=tuple(
            str(diagnosis.get("cause", ""))
            for diagnosis in diagnoses
            if isinstance(diagnosis, dict) and diagnosis.get("cause")
        )
        if isinstance(diagnoses, list)
        else (),
        turn_index=(
            raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else 0
        ),
        trace_schema_version=str(trace.get("schema_version", "1")),
    )


def _fresh_credential(
    signer: VisitorCredentialSigner,
    clock: Callable[[], datetime],
    ttl_seconds: int,
    *,
    tenant_id: str,
    session_id: uuid.UUID,
) -> str:
    """A renewed token for the conversation the caller already authenticated.

    The credential is reissued on every response, so the token a visitor holds
    is valid for at most one conversational exchange and a leaked token cannot
    outlive the visitor's own use (SEC-002).
    """
    return issue(signer, clock, ttl_seconds, tenant_id=tenant_id, session_id=session_id)


@router.post(
    "/api/chat/session",
    response_model=VisitorSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_session(
    payload: ChatSessionRequest,
    registry: Registry,
    conversations: Conversations,
    signer: VisitorSigner,
    clock: VisitorClock,
    settings: Configuration,
) -> VisitorSessionResponse:
    """Open a conversation and return the credential every later call quotes.

    The credential is server-issued and names both the tenant and the session:
    it is the only identity the other chat routes accept. The unguessable
    session UUID alone is not the credential — it is a session *name* inside a
    token only this server can sign (SEC-002).

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(payload.tenant_id)
    record = await conversations.create(payload.tenant_id)
    token = _fresh_credential(
        signer,
        clock,
        settings.visitor_credential_ttl_seconds,
        tenant_id=payload.tenant_id,
        session_id=record.session_id,
    )
    return VisitorSessionResponse(
        session=ChatSessionSummary.of(record), messages=[], credential=token
    )


@router.get("/api/chat/session", response_model=VisitorSessionResponse)
async def read_session(
    claims: VisitorIdentity,
    registry: Registry,
    conversations: Conversations,
    runtime: ComposedRuntime,
    signer: VisitorSigner,
    clock: VisitorClock,
    settings: Configuration,
) -> VisitorSessionResponse:
    """Everything said in the conversation the credential names, plus what it
    is waiting on.

    Answers from the store rather than the checkpoint, so a returning visitor
    sees the same transcript after a deployment that discarded in-flight runs.
    The pending question is the one thing only the runtime knows. The transcript
    is truncated to the most recent messages: the widget renders the tail of a
    conversation anyway, and an unauthenticated session read must not be able
    to grow without bound (`SEC-003`).

    The session is read under the credential's tenant and session — nothing in
    the query string names it, so the transcript cannot be linked from a URL.

    Raises:
        NotFoundError: no such conversation, or it belongs to another tenant.
    """
    tenant_id, session_id = claims.tenant_id, claims.session_id
    registry.get(tenant_id)
    record = await conversations.get(tenant_id, session_id)
    messages = (await conversations.transcript(tenant_id, session_id))[
        -settings.max_history_messages :
    ]
    pending = None if runtime is None else await runtime.pending(tenant_id, str(session_id))

    return VisitorSessionResponse(
        session=ChatSessionSummary.of(record),
        messages=[TranscriptMessage.of(message) for message in messages],
        pending=None if pending is None else PendingConfirmation.of(pending),
        credential=_fresh_credential(
            signer,
            clock,
            settings.visitor_credential_ttl_seconds,
            tenant_id=tenant_id,
            session_id=session_id,
        ),
    )


@router.post("/api/chat", response_model=ChatTurnResponse)
async def send_message(
    payload: ChatRequest,
    claims: VisitorIdentity,
    registry: Registry,
    conversations: Conversations,
    runtime: Runtime,
    turn_records: TurnRecords,
    signer: VisitorSigner,
    clock: VisitorClock,
    settings: Configuration,
) -> ChatTurnResponse:
    """Answer one visitor turn in the conversation the credential names.

    The visitor's message is stored before the runtime is asked anything, so a
    model outage or a lost worker leaves a conversation that is missing a reply
    rather than one that is missing the question.

    Raises:
        NotFoundError: no such tenant, conversation, or the conversation belongs
            to another tenant.
        ChatUnavailableError: this deployment composed no agent runtime.
    """
    tenant_id, session_id = claims.tenant_id, claims.session_id
    registry.get(tenant_id)
    await conversations.get(tenant_id, session_id)
    await conversations.append(
        tenant_id,
        session_id,
        role=MessageRole.VISITOR,
        content=payload.message,
    )

    started = time.monotonic()
    turn = await runtime.send(tenant_id, str(session_id), payload.message)
    _record_metrics(time.monotonic() - started, turn)
    await _record_answer(conversations, tenant_id, session_id, turn)
    await _record_turn(turn_records, tenant_id, session_id, turn)
    _log_turn(turn)

    return ChatTurnResponse.of(
        session_id,
        turn,
        _fresh_credential(
            signer,
            clock,
            settings.visitor_credential_ttl_seconds,
            tenant_id=tenant_id,
            session_id=session_id,
        ),
    )


@router.post("/api/chat/consent", response_model=ConsentResponse)
async def record_consent(
    payload: ConsentRequest,
    claims: VisitorIdentity,
    registry: Registry,
    conversations: Conversations,
    consent: Consent,
) -> ConsentResponse:
    """Record that a visitor agreed to the purposes they are about to use.

    The gateway every contact-bearing action passes: `PRIV-001` refuses to
    store a booking or a lead until this endpoint has a grant for the session.
    The statement is the tenant's, derived from its policy — a visitor cannot
    agree to text the server would not also record.

    Re-recording the same purposes is idempotent; recording a purpose the
    tenant's own actions never require is permitted, because a visitor may
    agree to more than the minimum.

    Raises:
        NotFoundError: no such tenant, conversation, or the conversation
            belongs to another tenant.
    """
    tenant_id, session_id = claims.tenant_id, claims.session_id
    policy = registry.get(tenant_id).policy
    await conversations.get(tenant_id, session_id)
    purposes = [ConsentPurpose(purpose) for purpose in payload.purposes]
    statement = consent_statement(policy.name, purposes)
    recorded = await consent.record(
        tenant_id,
        str(session_id),
        purposes=purposes,
        statement=statement,
    )
    return ConsentResponse(
        purposes=[record.purpose.value for record in recorded],
        statement=statement,
        granted_at=max(record.granted_at for record in recorded),
    )


@router.post("/api/chat/confirmation", response_model=ChatTurnResponse)
async def confirm_booking(
    payload: BookingConfirmationRequest,
    claims: VisitorIdentity,
    registry: Registry,
    conversations: Conversations,
    runtime: Runtime,
    turn_records: TurnRecords,
    signer: VisitorSigner,
    clock: VisitorClock,
    settings: Configuration,
) -> ChatTurnResponse:
    """Answer the booking the assistant proposed, and finish the turn.

    Refusing when nothing is pending is the point of the check: resuming a
    conversation that already finished would run the graph forward again and
    append a second answer to a customer who asked one question.

    Raises:
        NotFoundError: no such tenant, conversation, or the conversation belongs
            to another tenant.
        ConflictError: this conversation is not waiting on a confirmation.
        ChatUnavailableError: this deployment composed no agent runtime.
    """
    tenant_id, session_id = claims.tenant_id, claims.session_id
    registry.get(tenant_id)
    await conversations.get(tenant_id, session_id)

    session_key = str(session_id)
    if await runtime.pending(tenant_id, session_key) is None:
        raise ConflictError(detail=f"conversation {session_key} is awaiting no confirmation")

    started = time.monotonic()
    turn = await runtime.resume(tenant_id, session_key, approved=payload.decision == "approved")
    _record_metrics(time.monotonic() - started, turn)
    await _record_answer(conversations, tenant_id, session_id, turn)
    await _record_turn(turn_records, tenant_id, session_id, turn)
    _log_turn(turn)

    return ChatTurnResponse.of(
        session_id,
        turn,
        _fresh_credential(
            signer,
            clock,
            settings.visitor_credential_ttl_seconds,
            tenant_id=tenant_id,
            session_id=session_id,
        ),
    )


@router.get("/api/chat/sources/{source_id}", response_model=SourceViewResponse)
async def read_source(
    source_id: str,
    claims: VisitorIdentity,
    registry: Registry,
    index: SearchIndexes,
    knowledge: Knowledge,
    clock: VisitorClock,
) -> SourceViewResponse:
    """Resolve a citation to the authorized view of its source (`RAG-005`).

    The citation a widget renders addresses a chunk by its index id. This route
    serves the passage and the version-window metadata — and nothing else:
    the chunk must belong to the credential's tenant and still be retrievable
    for a visitor, and every reason it is not (absent, inactive, expired,
    superseded, another tenant's) is the same 404, so a citation cannot be
    used to probe what a tenant has.

    Raises:
        NotFoundError: no such tenant, or the source is not answerable for it.
    """
    tenant_id = claims.tenant_id
    registry.get(tenant_id)
    if index is None:
        raise NotFoundError(detail=f"source {source_id} is not answerable")
    chunk = await index.chunk_by_id(tenant_id=tenant_id, chunk_id=source_id)
    if chunk is None:
        raise NotFoundError(detail=f"source {source_id} is not answerable")
    document = await knowledge.document_for_version(tenant_id, chunk.version_id)
    retrievable = document.retrievable_version(
        RetrievalContext(
            tenant_id=tenant_id,
            domain=document.domain,
            audience=RetrievalAudience.VISITOR,
            moment=clock(),
        )
    )
    if retrievable is None or retrievable.version_id != chunk.version_id:
        raise NotFoundError(detail=f"source {source_id} is not answerable")
    view = document.public_view(retrievable)
    return SourceViewResponse(
        source_id=chunk.chunk_id,
        title=chunk.title,
        source_name=view.source_name,
        location=chunk.section,
        text=chunk.text,
        revision=view.revision,
        effective_at=view.effective_at,
    )
