"""The operator console surface.

Every route here reads or writes another person's conversation, so all of them
require an identity the gateway established and a role this service re-checks —
see :mod:`tenantchat.api.identity`. None of them is reachable through CORS: the
allowlist in the composition root covers the widget origins, and an operator's
browser talks to this API same-origin through the gateway.

**These routes are not yet tenant-scoped.** `SEC-001` adds the membership check
that binds an operator to the tenants they work for; until it lands, any
authenticated operator can read any tenant's conversations, and the deployment
boundary is what keeps that acceptable.
"""

from __future__ import annotations

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from tenantchat.api.dependencies import Configuration, Conversations, Registry, get_settings
from tenantchat.api.identity import AdminIdentity, csrf_token, require_role, verify_csrf
from tenantchat.api.schemas import (
    AdminSessionsResponse,
    ChatSessionResponse,
    ChatSessionSummary,
    CsrfTokenResponse,
    StaffMessageRequest,
    StaffMessageResponse,
    TranscriptMessage,
)
from tenantchat.api.store import MessageRole

router = APIRouter(tags=["admin"])

logger = logging.getLogger(__name__)

_read_access = require_role("viewer")
_reply_access = require_role("support_agent")


def _authorized_reply(request: Request) -> AdminIdentity:
    """Admit an operator who may reply, and only through a same-origin request.

    The role and the CSRF token answer different questions. The role says this
    operator is allowed to speak to customers; the token says this particular
    request came from the console rather than from a page that merely knows the
    operator's browser holds a session.

    Raises:
        UnauthenticatedError: no usable operator identity.
        ForbiddenError: the operator may not send staff replies.
        CsrfValidationError: the double-submit token is absent or wrong.
    """
    identity = _reply_access(request)
    verify_csrf(request, identity, get_settings(request))
    return identity


Reader = Annotated[AdminIdentity, Depends(_read_access)]
Replier = Annotated[AdminIdentity, Depends(_authorized_reply)]
TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]
PageSize = Annotated[int, Query(ge=1, le=200)]


@router.get("/api/admin/csrf-token", response_model=CsrfTokenResponse)
def issue_csrf_token(identity: Reader, settings: Configuration) -> CsrfTokenResponse:
    """Mint the token the console must echo on state-changing requests.

    Readable by any authenticated operator: the token authorizes nothing on its
    own, and one derived for a viewer is useless without the role a write also
    requires.

    Raises:
        CsrfValidationError: no CSRF secret is configured, so no acceptable
            token exists.
    """
    return CsrfTokenResponse(csrf_token=csrf_token(identity, settings))


@router.get("/api/admin/chats", response_model=AdminSessionsResponse)
async def list_chats(
    identity: Reader,
    tenant_id: TenantIdQuery,
    registry: Registry,
    conversations: Conversations,
    limit: PageSize = 50,
) -> AdminSessionsResponse:
    """Conversations for one tenant, most recently active first.

    Summaries only. Listing is how an operator finds work, and a list endpoint
    that returned transcripts would put every customer's words into a response
    nobody asked for.

    Raises:
        NotFoundError: no such tenant.
    """
    registry.get(tenant_id)
    records = await conversations.for_tenant(tenant_id, limit=limit)
    return AdminSessionsResponse(
        sessions=[ChatSessionSummary.of(record) for record in records], limit=limit
    )


@router.get("/api/admin/chats/{session_id}", response_model=ChatSessionResponse)
async def read_chat(
    identity: Reader,
    session_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    conversations: Conversations,
) -> ChatSessionResponse:
    """One conversation and its full transcript.

    Raises:
        NotFoundError: no such conversation, or it belongs to another tenant.
    """
    record = await conversations.get(tenant_id, session_id)
    messages = await conversations.transcript(tenant_id, session_id)
    return ChatSessionResponse(
        session=ChatSessionSummary.of(record),
        messages=[TranscriptMessage.of(message) for message in messages],
    )


@router.post(
    "/api/admin/chats/{session_id}/messages",
    response_model=StaffMessageResponse,
    status_code=status.HTTP_201_CREATED,
)
async def send_staff_message(
    identity: Replier,
    session_id: uuid.UUID,
    payload: StaffMessageRequest,
    conversations: Conversations,
) -> StaffMessageResponse:
    """Say something to the customer as a person.

    Stored with the ``staff`` role, distinct from ``assistant``: a reply a human
    wrote and one a model produced carry different weight for the customer
    reading them and for anyone auditing what was promised.

    The message does not enter the model's view of the conversation. Feeding
    staff replies back into the agent's transcript is `FEAT-004`, which owns the
    handoff lifecycle and the question of whether the assistant should resume at
    all once a person has taken over.

    Raises:
        NotFoundError: no such conversation, or it belongs to another tenant.
    """
    record = await conversations.append(
        payload.tenant_id,
        session_id,
        role=MessageRole.STAFF,
        content=payload.content,
        metadata={"operator_subject": identity.subject},
    )
    logger.info(
        "staff reply recorded",
        extra={
            "subject": identity.subject,
            "tenant_id": payload.tenant_id,
            "session_id": str(session_id),
        },
    )
    return StaffMessageResponse(message=TranscriptMessage.of(record))
