"""The visitor conversation surface.

Three things happen here and nowhere else in this package: a conversation is
opened, a turn is run, and a proposed booking is answered. The assistant's
behavior — which tools it may call, when it stops to ask, what it commits — lives
in the agent runtime behind
:class:`~tenantchat.core.ports.ConversationRuntime`, and every effect it causes
crosses an idempotent domain service. This module owns the part the runtime must
not: which conversation the caller is allowed to write to, and what is written
down.

**The store is the record, the checkpoint is not.** Each message is appended to
the conversation store before and after the runtime runs, so deleting every
checkpoint — which `ADR-0001` requires be survivable — costs the resume point and
no transcript.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from tenantchat.api.dependencies import ComposedRuntime, Conversations, Registry, Runtime
from tenantchat.api.schemas import (
    BookingConfirmationRequest,
    ChatRequest,
    ChatSessionRequest,
    ChatSessionResponse,
    ChatSessionSummary,
    ChatTurnResponse,
    PendingConfirmation,
    TranscriptMessage,
)
from tenantchat.api.store import ConversationStore, MessageRole
from tenantchat.core.errors import ConflictError
from tenantchat.core.ports import AssistantTurn

router = APIRouter(tags=["chat"])

TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]


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


@router.post(
    "/api/chat/session",
    response_model=ChatSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def open_session(
    payload: ChatSessionRequest,
    registry: Registry,
    conversations: Conversations,
) -> ChatSessionResponse:
    """Open a conversation and return the ID every later turn quotes.

    The ID is server-issued. A visitor-chosen one can be guessed, replayed from
    another browser, or collided with deliberately, and `DATA-002` therefore
    refuses to let a client-supplied label select a transcript.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(payload.tenant_id)
    record = await conversations.create(payload.tenant_id)
    return ChatSessionResponse(session=ChatSessionSummary.of(record), messages=[])


@router.get("/api/chat/session/{session_id}", response_model=ChatSessionResponse)
async def read_session(
    session_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    conversations: Conversations,
    runtime: ComposedRuntime,
) -> ChatSessionResponse:
    """Everything said in one conversation, plus anything it is waiting on.

    Answers from the store rather than the checkpoint, so a returning visitor
    sees the same transcript after a deployment that discarded in-flight runs.
    The pending question is the one thing only the runtime knows.

    Raises:
        NotFoundError: no such conversation, or it belongs to another tenant.
    """
    record = await conversations.get(tenant_id, session_id)
    messages = await conversations.transcript(tenant_id, session_id)
    pending = None if runtime is None else await runtime.pending(tenant_id, str(session_id))

    return ChatSessionResponse(
        session=ChatSessionSummary.of(record),
        messages=[TranscriptMessage.of(message) for message in messages],
        pending=None if pending is None else PendingConfirmation.of(pending),
    )


@router.post("/api/chat", response_model=ChatTurnResponse)
async def send_message(
    payload: ChatRequest,
    registry: Registry,
    conversations: Conversations,
    runtime: Runtime,
) -> ChatTurnResponse:
    """Answer one visitor turn.

    The visitor's message is stored before the runtime is asked anything, so a
    model outage or a lost worker leaves a conversation that is missing a reply
    rather than one that is missing the question.

    Raises:
        NotFoundError: no such tenant, conversation, or the conversation belongs
            to another tenant.
        ChatUnavailableError: this deployment composed no agent runtime.
    """
    registry.get(payload.tenant_id)
    await conversations.get(payload.tenant_id, payload.session_id)
    await conversations.append(
        payload.tenant_id,
        payload.session_id,
        role=MessageRole.VISITOR,
        content=payload.message,
    )

    turn = await runtime.send(payload.tenant_id, str(payload.session_id), payload.message)
    await _record_answer(conversations, payload.tenant_id, payload.session_id, turn)

    return ChatTurnResponse.of(payload.session_id, turn)


@router.post("/api/chat/confirmation", response_model=ChatTurnResponse)
async def confirm_booking(
    payload: BookingConfirmationRequest,
    registry: Registry,
    conversations: Conversations,
    runtime: Runtime,
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
    registry.get(payload.tenant_id)
    await conversations.get(payload.tenant_id, payload.session_id)

    session_key = str(payload.session_id)
    if await runtime.pending(payload.tenant_id, session_key) is None:
        raise ConflictError(detail=f"conversation {session_key} is awaiting no confirmation")

    turn = await runtime.resume(
        payload.tenant_id, session_key, approved=payload.decision == "approved"
    )
    await _record_answer(conversations, payload.tenant_id, payload.session_id, turn)

    return ChatTurnResponse.of(payload.session_id, turn)
