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

import uuid
from collections.abc import Callable
from datetime import datetime

from fastapi import APIRouter, status

from tenantchat.api.dependencies import (
    ComposedRuntime,
    Configuration,
    Conversations,
    Registry,
    Runtime,
)
from tenantchat.api.schemas import (
    BookingConfirmationRequest,
    ChatRequest,
    ChatSessionRequest,
    ChatSessionSummary,
    ChatTurnResponse,
    PendingConfirmation,
    TranscriptMessage,
    VisitorSessionResponse,
)
from tenantchat.api.store import ConversationStore, MessageRole
from tenantchat.api.visitor import VisitorClock, VisitorIdentity, VisitorSigner, issue
from tenantchat.core.errors import ConflictError
from tenantchat.core.ports import AssistantTurn
from tenantchat.core.visitor_session import VisitorCredentialSigner

router = APIRouter(tags=["chat"])


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
    The pending question is the one thing only the runtime knows.

    The session is read under the credential's tenant and session — nothing in
    the query string names it, so the transcript cannot be linked from a URL.

    Raises:
        NotFoundError: no such conversation, or it belongs to another tenant.
    """
    tenant_id, session_id = claims.tenant_id, claims.session_id
    registry.get(tenant_id)
    record = await conversations.get(tenant_id, session_id)
    messages = await conversations.transcript(tenant_id, session_id)
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

    turn = await runtime.send(tenant_id, str(session_id), payload.message)
    await _record_answer(conversations, tenant_id, session_id, turn)

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


@router.post("/api/chat/confirmation", response_model=ChatTurnResponse)
async def confirm_booking(
    payload: BookingConfirmationRequest,
    claims: VisitorIdentity,
    registry: Registry,
    conversations: Conversations,
    runtime: Runtime,
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

    turn = await runtime.resume(tenant_id, session_key, approved=payload.decision == "approved")
    await _record_answer(conversations, tenant_id, session_id, turn)

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
