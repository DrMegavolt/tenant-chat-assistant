"""Request and response models.

Every field is bounded. The bounds here are a cheap outer gate that rejects
absurd input before it reaches domain parsing — they are not the business rule,
which lives in ``tenantchat.core.commands`` and applies to callers that never
touch HTTP. Where the two overlap, the domain's limit is the tighter one and the
one that decides.

``extra="forbid"`` on every request model: a typo in a field name fails loudly
rather than being silently dropped and treated as absent.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from tenantchat.api.store import (
    BookingRecord,
    ConversationRecord,
    LeadRecord,
    MessageRecord,
    TenantMembership,
)
from tenantchat.core.ports import AssistantTurn
from tenantchat.core.tenant import PublicTenantView

# Generous outer bounds. The domain applies the meaningful limits.
_TENANT_ID = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
# A correlation label, never an identity. It is client-supplied, so it must not
# be used to authorize a read or to bind a record to a visitor — `SEC-002`
# replaces it with a server-issued session token that can carry that weight.
# DATA-002 uses it only to group write-only action records inside one tenant;
# it never authorizes a read or selects a transcript, which keeps the weak value
# from becoming an identity boundary.
_SESSION_ID = Field(default="", max_length=128)
_SHORT_TEXT = Field(default="", max_length=512)
_LONG_TEXT = Field(default="", max_length=4096)
# A single chat turn. Required and non-blank, because an empty turn is a request
# the assistant cannot answer and a checkpoint the runtime should not write.
_MESSAGE = Field(min_length=1, max_length=4000)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BookingRequest(_Request):
    tenant_id: str = _TENANT_ID
    session_id: str = _SESSION_ID
    service: str = _SHORT_TEXT
    slot: str = _SHORT_TEXT
    customer_name: str = _SHORT_TEXT
    address: str = _SHORT_TEXT
    # Named for what the customer may supply, since either kind is accepted and
    # the domain decides which it is.
    contact: str = _SHORT_TEXT


class LeadRequest(_Request):
    tenant_id: str = _TENANT_ID
    session_id: str = _SESSION_ID
    customer_name: str = _SHORT_TEXT
    contact: str = _SHORT_TEXT
    service: str = _SHORT_TEXT
    summary: str = _LONG_TEXT
    address_or_zip: str = _SHORT_TEXT
    urgency: str = _SHORT_TEXT


class BookingResponse(BaseModel):
    """Confirmation of an accepted booking.

    The contact is echoed in its canonical form rather than as typed, so the
    customer sees the number the business will actually dial.
    """

    booking_id: str
    service: str
    slot: str
    customer_name: str
    contact: str
    address: str
    created_at: datetime

    @classmethod
    def of(cls, record: BookingRecord) -> BookingResponse:
        return cls(
            booking_id=record.booking_id,
            service=record.service_name,
            slot=record.slot,
            customer_name=record.customer_name,
            contact=record.contact.display,
            address=record.address,
            created_at=record.created_at,
        )


class LeadResponse(BaseModel):
    lead_id: str
    service: str
    summary: str
    urgency: str
    created_at: datetime

    @classmethod
    def of(cls, record: LeadRecord) -> LeadResponse:
        return cls(
            lead_id=record.lead_id,
            service=record.service,
            summary=record.summary,
            urgency=record.urgency.value,
            created_at=record.created_at,
        )


class TenantSummary(BaseModel):
    """One tenant as the embeddable widget sees it.

    Built only from :class:`~tenantchat.core.tenant.PublicTenantView`. Adding a
    private field to ``TenantPolicy`` cannot leak through here, because the type
    this reads from does not have it.
    """

    tenant_id: str
    name: str
    assistant_name: str
    tagline: str
    phone: str
    address: str
    hours: str
    services: list[str]
    quick_actions: list[str]
    booking_enabled: bool
    lead_capture_enabled: bool

    @classmethod
    def of(cls, view: PublicTenantView) -> TenantSummary:
        return cls(
            tenant_id=view.tenant_id,
            name=view.name,
            assistant_name=view.assistant_name,
            tagline=view.tagline,
            phone=view.phone,
            address=view.address,
            hours=view.hours,
            services=list(view.services),
            quick_actions=list(view.quick_actions),
            booking_enabled=view.booking_enabled,
            lead_capture_enabled=view.lead_capture_enabled,
        )


class TenantsResponse(BaseModel):
    tenants: dict[str, TenantSummary]


class AvailabilityResponse(BaseModel):
    service: str
    slots: list[str]


class HealthResponse(BaseModel):
    status: str


class ChatSessionRequest(_Request):
    tenant_id: str = _TENANT_ID


class ChatRequest(_Request):
    """One visitor turn.

    ``session_id`` is the server-issued conversation ID from
    ``POST /api/chat/session``, not a label the visitor invented. It is
    unguessable, which is what lets it name a conversation at all; `SEC-002`
    replaces it with a signed visitor credential that can also carry an
    expiry and survive being copied out of a URL.
    """

    tenant_id: str = _TENANT_ID
    session_id: uuid.UUID
    message: str = _MESSAGE


class BookingConfirmationRequest(_Request):
    """The customer's answer to a booking the assistant proposed."""

    tenant_id: str = _TENANT_ID
    session_id: uuid.UUID
    # A closed set rather than a boolean: an omitted or misspelled field on a
    # boolean would read as "declined", and a silently declined booking looks to
    # the customer exactly like an assistant that ignored them.
    decision: Literal["approved", "declined"]


class StaffMessageRequest(_Request):
    tenant_id: str = _TENANT_ID
    content: str = _MESSAGE


class PendingConfirmation(BaseModel):
    """A question the assistant stopped to ask before committing anything.

    Echoes the details back so the customer confirms what will actually be
    booked rather than what they believe they said.
    """

    awaiting: str
    service: str
    slot: str
    customer_name: str
    address: str

    @classmethod
    def of(cls, pending: Mapping[str, object]) -> PendingConfirmation:
        """Project a runtime interrupt payload, ignoring anything unrecognized.

        The payload is owned by the graph, so this reads the fields it publishes
        and drops the rest. A field the graph adds is not published by accident.
        """
        return cls(
            awaiting=str(pending.get("awaiting", "")),
            service=str(pending.get("service", "")),
            slot=str(pending.get("slot", "")),
            customer_name=str(pending.get("customer_name", "")),
            address=str(pending.get("address", "")),
        )


class CommittedActionSummary(BaseModel):
    """One thing this conversation has actually caused.

    ``replayed`` distinguishes "just booked" from "already booked, and this was
    a retry", which is the difference between a confirmation worth showing and a
    duplicate the customer would read as a second appointment.
    """

    action: str
    reference: str
    replayed: bool


class TurnProvenance(BaseModel):
    """The component versions this answer is attributable to.

    Returned on every turn so that `OBS-004` can pin an answer to the graph,
    prompt, and model that produced it without reaching into the runtime.
    """

    # `model_` is Pydantic's reserved prefix; the field is named for the thing it
    # reports rather than renamed to avoid the collision.
    model_config = ConfigDict(protected_namespaces=())

    model_name: str
    graph_version: str
    prompt_version: str


class ChatTurnResponse(BaseModel):
    """What one turn produced, and what it committed on the way.

    ``pending`` and ``reply`` are alternatives: a turn that stopped to ask
    something has no answer yet, and the conversation continues at
    ``POST /api/chat/confirmation``.
    """

    session_id: uuid.UUID
    reply: str
    pending: PendingConfirmation | None
    committed: list[CommittedActionSummary]
    provenance: TurnProvenance

    @classmethod
    def of(cls, session_id: uuid.UUID, turn: AssistantTurn) -> ChatTurnResponse:
        return cls(
            session_id=session_id,
            reply=turn.answer,
            pending=None if turn.pending is None else PendingConfirmation.of(turn.pending),
            committed=[
                CommittedActionSummary(
                    action=effect.action, reference=effect.reference, replayed=effect.replayed
                )
                for effect in turn.committed
            ],
            provenance=TurnProvenance(
                model_name=turn.model_name,
                graph_version=turn.graph_version,
                prompt_version=turn.prompt_version,
            ),
        )


class TranscriptMessage(BaseModel):
    """One stored message, as either the visitor or an operator reads it."""

    message_id: uuid.UUID
    role: str
    content: str
    created_at: datetime

    @classmethod
    def of(cls, record: MessageRecord) -> TranscriptMessage:
        return cls(
            message_id=record.message_id,
            role=record.role.value,
            content=record.content,
            created_at=record.created_at,
        )


class ChatSessionSummary(BaseModel):
    """A conversation without its contents."""

    session_id: uuid.UUID
    tenant_id: str
    status: str
    outcome: str
    started_at: datetime
    last_activity_at: datetime

    @classmethod
    def of(cls, record: ConversationRecord) -> ChatSessionSummary:
        return cls(
            session_id=record.session_id,
            tenant_id=record.tenant_id,
            status=record.status,
            outcome=record.outcome,
            started_at=record.started_at,
            last_activity_at=record.last_activity_at,
        )


class ChatSessionResponse(BaseModel):
    """A conversation and everything said in it."""

    session: ChatSessionSummary
    messages: list[TranscriptMessage]
    pending: PendingConfirmation | None = None


class AdminSessionsResponse(BaseModel):
    sessions: list[ChatSessionSummary]
    # Echoed so a caller can tell a full page from the end of the list.
    limit: int


class AdminTenantSummary(BaseModel):
    """One tenant an operator is allowed to work, with the role they hold."""

    tenant_id: str
    name: str
    role: str


class AdminTenantsResponse(BaseModel):
    tenants: list[AdminTenantSummary]


class AdminLead(BaseModel):
    """A captured lead as an authorized operator reads it.

    Deliberately richer than the visitor-facing :class:`LeadResponse`: the
    contact value is PII, and it is shown only on this authenticated,
    tenant-scoped surface.
    """

    lead_id: str
    tenant_id: str
    session_id: str
    customer_name: str
    contact: str
    service: str
    service_slug: str | None
    summary: str
    address_or_zip: str
    urgency: str
    created_at: datetime

    @classmethod
    def of(cls, record: LeadRecord) -> AdminLead:
        return cls(
            lead_id=record.lead_id,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            customer_name=record.customer_name,
            contact=record.contact.display,
            service=record.service,
            service_slug=record.service_slug,
            summary=record.summary,
            address_or_zip=record.address_or_zip,
            urgency=record.urgency.value,
            created_at=record.created_at,
        )


class AdminLeadsResponse(BaseModel):
    leads: list[AdminLead]


class AdminBooking(BaseModel):
    booking_id: str
    tenant_id: str
    session_id: str
    customer_name: str
    contact: str
    service: str
    slot: str
    address: str
    created_at: datetime

    @classmethod
    def of(cls, record: BookingRecord) -> AdminBooking:
        return cls(
            booking_id=record.booking_id,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            customer_name=record.customer_name,
            contact=record.contact.display,
            service=record.service_name,
            slot=record.slot,
            address=record.address,
            created_at=record.created_at,
        )


class AdminBookingsResponse(BaseModel):
    bookings: list[AdminBooking]


class MembershipRequest(_Request):
    """Per-tenant role assignment (SEC-001).

    ``role`` is a closed set of the three tenant roles. ``platform_admin`` is
    deliberately unassignable: it spans tenants and is decided by the identity
    provider's groups, so a tenant-scoped record cannot confer it.
    """

    tenant_id: str = _TENANT_ID
    subject: str = Field(min_length=1, max_length=200)
    role: Literal["viewer", "support_agent", "tenant_admin"]


class MembershipResponse(BaseModel):
    tenant_id: str
    subject: str
    role: str

    @classmethod
    def of(cls, record: TenantMembership) -> MembershipResponse:
        return cls(
            tenant_id=record.tenant_id,
            subject=record.principal_subject,
            role=record.role,
        )


class StaffMessageResponse(BaseModel):
    message: TranscriptMessage


class CsrfTokenResponse(BaseModel):
    csrf_token: str
