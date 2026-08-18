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
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from tenantchat.api.jobs import JobEvent, JobRecord
from tenantchat.api.store import (
    AuditEvent,
    BookingRecord,
    ConsentRecord,
    ConversationRecord,
    HandoffRecord,
    LeadRecord,
    MessageRecord,
    PrivacyRequestRecord,
    ReviewCase,
    ReviewDiagnosis,
    TenantMembership,
    TraceAccessGrant,
    TurnFeedback,
    TurnRecord,
    TurnRecordProjection,
)
from tenantchat.core.citations import Citation
from tenantchat.core.indexing import IndexGeneration, IndexIntegrityFinding
from tenantchat.core.knowledge import (
    DocumentVersion,
    KnowledgeDocument,
    KnowledgeSource,
)
from tenantchat.core.ports import AssistantTurn
from tenantchat.core.tenant import PublicTenantView

# Generous outer bounds. The domain applies the meaningful limits.
_TENANT_ID = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
# A citation's source identifier is an index chunk id: bounded like an
# Elasticsearch document id, which is what it is.
_SOURCE_ID = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
# A single chat turn. Required and non-blank, because an empty turn is a request
# the assistant cannot answer and a checkpoint the runtime should not write.
_MESSAGE = Field(min_length=1, max_length=4000)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
    proactive_lead_capture: bool
    # The exact sentence the consent grant is recorded under. The widget must
    # render this value rather than composing its own, so the statement shown
    # and the statement persisted cannot diverge on a tenant override.
    contact_consent_statement: str
    site_headline: str
    site_description: str

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
            proactive_lead_capture=view.proactive_lead_capture,
            contact_consent_statement=view.contact_consent_statement,
            site_headline=view.site_headline,
            site_description=view.site_description,
        )


class TenantsResponse(BaseModel):
    tenants: dict[str, TenantSummary]


class AvailabilityResponse(BaseModel):
    service: str
    slots: list[str]


class HealthResponse(BaseModel):
    status: str


class ChatSessionRequest(_Request):
    tenant_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
        validation_alias=AliasChoices("tenant_id", "tenantId"),
    )


class ChatRequest(_Request):
    """One visitor turn.

    The conversation is named by the ``X-Visitor-Credential`` header, not by
    fields in the body: the credential is server-issued, bound to exactly one
    tenant and session, and unguessable without the signing key (SEC-002). A
    body field cannot move a turn between tenants.
    """

    message: str = _MESSAGE


class BookingConfirmationRequest(_Request):
    """The customer's answer to a booking the assistant proposed.

    The conversation is named by the credential header, like ``ChatRequest``.
    """

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
    booked or captured rather than what they believe they said. A booking
    confirmation carries the booked slot and address; a lead confirmation
    carries the contact and summary instead.
    """

    awaiting: str
    service: str
    slot: str = ""
    customer_name: str
    address: str = ""
    contact: str = ""
    summary: str = ""

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
            contact=str(pending.get("contact", "")),
            summary=str(pending.get("summary", "")),
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


class CitationSummary(BaseModel):
    """One source a claim in the answer was grounded in, as a client may see it.

    This is the curated projection of :class:`~tenantchat.core.citations.Citation`
    — verified by the graph against the exact evidence context, tenant-scoped by
    the retrieval adapter, and free of any storage or operator detail. The raw
    citation markers and the invalid-citation verdict are inference-plane data
    and never appear here.
    """

    source_id: str
    title: str
    source_name: str
    location: str
    revision: int
    effective_at: datetime

    @classmethod
    def of(cls, citation: Citation) -> CitationSummary:
        return cls(
            source_id=citation.source_id,
            title=citation.title,
            source_name=citation.source_name,
            location=citation.location,
            revision=citation.revision,
            effective_at=citation.effective_at,
        )


class SourceViewResponse(BaseModel):
    """The authorized view a citation resolves to (`RAG-005`).

    The passage itself plus the version-window metadata that pins what was
    answered from; served only when the citation's chunk belongs to the caller's
    tenant and is still retrievable.
    """

    source_id: str
    title: str
    source_name: str
    location: str
    text: str
    revision: int
    effective_at: datetime


class ChatTurnResponse(BaseModel):
    """What one turn produced, and what it committed on the way.

    ``pending`` and ``reply`` are alternatives: a turn that stopped to ask
    something has no answer yet, and the conversation continues at
    ``POST /api/chat/confirmation``.

    ``turn_id`` is the inference-plane record the turn earned, echoed so the
    widget can attach feedback to exactly the turn it shows (`FEAT-008`).

    ``credential`` is a freshly reissued visitor token: it names the same
    tenant and session the caller presented and replaces it, so an active
    conversation never lets its credential expire (SEC-002).
    """

    session_id: uuid.UUID
    turn_id: uuid.UUID | None
    reply: str
    pending: PendingConfirmation | None
    committed: list[CommittedActionSummary]
    citations: list[CitationSummary]
    provenance: TurnProvenance
    credential: str

    @classmethod
    def of(
        cls,
        session_id: uuid.UUID,
        turn: AssistantTurn,
        credential: str,
        *,
        turn_id: uuid.UUID | None = None,
    ) -> ChatTurnResponse:
        return cls(
            session_id=session_id,
            turn_id=turn_id,
            reply=turn.answer,
            pending=None if turn.pending is None else PendingConfirmation.of(turn.pending),
            committed=[
                CommittedActionSummary(
                    action=effect.action, reference=effect.reference, replayed=effect.replayed
                )
                for effect in turn.committed
            ],
            citations=[CitationSummary.of(citation) for citation in turn.citations],
            provenance=TurnProvenance(
                model_name=turn.model_name,
                graph_version=turn.graph_version,
                prompt_version=turn.prompt_version,
            ),
            credential=credential,
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


class VisitorSessionResponse(ChatSessionResponse):
    """A conversation as the visitor reads it, plus a fresh credential.

    Distinct from ``ChatSessionResponse`` on purpose: the credential names the
    conversation, so only the visitor routes may return one. The admin routes
    share the parent model and never carry a credential field (SEC-002).
    """

    credential: str


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


class AdminHandoff(BaseModel):
    """One handoff row as an authorized operator reads it.

    The queue lists every open handoff with the escalation reason and summary —
    the ticket's own content, on the authenticated tenant-scoped surface — and
    the assignment state that decides who may work it. The assigned principal
    is staff-facing accountability data, never anything the visitor sees.
    """

    handoff_id: str
    tenant_id: str
    session_id: str
    status: str
    reason: str
    summary: str
    assigned_principal_id: str | None
    requested_at: datetime
    assigned_at: datetime | None
    released_at: datetime | None
    resolved_at: datetime | None
    resolved_by_principal_id: str | None

    @classmethod
    def of(cls, record: HandoffRecord) -> AdminHandoff:
        return cls(
            handoff_id=record.handoff_id,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            status=record.status,
            reason=record.reason.value,
            summary=record.summary,
            assigned_principal_id=record.assigned_principal_id,
            requested_at=record.created_at,
            assigned_at=record.assigned_at,
            released_at=record.released_at,
            resolved_at=record.resolved_at,
            resolved_by_principal_id=record.resolved_by_principal_id,
        )


class AdminHandoffsResponse(BaseModel):
    """The tenant's open handoff queue, oldest first.

    ``operator_subject`` is the viewing operator's own pseudonymous id, so the
    console can tell which rows it holds without any staff member's identity
    being derivable from the queue alone.
    """

    handoffs: list[AdminHandoff]
    operator_subject: str
    # Echoed so a caller can tell a full queue from the end of the list.
    limit: int


class HandoffActionResponse(BaseModel):
    """One handoff after a staff ownership transition."""

    handoff: AdminHandoff


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


class ConsentRequest(_Request):
    """A visitor recording consent for contact-bearing purposes.

    ``purposes`` is closed and bounded: only the two the platform knows how to
    state, and at most both of them. The statement is not taken from the
    visitor — the server derives it from the tenant's policy, so the recorded
    text is always the one the tenant published.

    The conversation is named by the ``X-Visitor-Credential`` header, like every
    other visitor route (SEC-002). A consent grant is the gate that lets contact
    details be stored, so a body-named session would let anyone who guesses a
    session UUID grant consent on that visitor's behalf.
    """

    purposes: list[Literal["booking", "follow_up"]] = Field(min_length=1, max_length=2)


class ConsentResponse(BaseModel):
    """What the session now holds, echoed back so the widget can show it."""

    purposes: list[str]
    statement: str
    granted_at: datetime


class ContactQuery(_Request):
    """An operator naming a subject for export or erasure."""

    tenant_id: str = _TENANT_ID
    # Wider than the domain's contact bound on purpose: the operator pastes
    # from a CRM and the domain parser applies the real limits.
    contact: str = Field(min_length=3, max_length=320)


class SessionExportItem(BaseModel):
    session_id: uuid.UUID
    status: str
    outcome: str
    started_at: datetime
    last_activity_at: datetime

    @classmethod
    def of(cls, record: ConversationRecord) -> SessionExportItem:
        return cls(
            session_id=record.session_id,
            status=record.status,
            outcome=record.outcome,
            started_at=record.started_at,
            last_activity_at=record.last_activity_at,
        )


class LeadExportItem(BaseModel):
    lead_id: str
    created_at: datetime
    customer_name: str
    contact: str
    service: str
    summary: str
    address_or_zip: str
    urgency: str

    @classmethod
    def of(cls, record: LeadRecord) -> LeadExportItem:
        return cls(
            lead_id=record.lead_id,
            created_at=record.created_at,
            customer_name=record.customer_name,
            contact=record.contact.value,
            service=record.service,
            summary=record.summary,
            address_or_zip=record.address_or_zip,
            urgency=record.urgency.value,
        )


class BookingExportItem(BaseModel):
    booking_id: str
    created_at: datetime
    customer_name: str
    contact: str
    address: str
    service: str
    slot: str

    @classmethod
    def of(cls, record: BookingRecord) -> BookingExportItem:
        return cls(
            booking_id=record.booking_id,
            created_at=record.created_at,
            customer_name=record.customer_name,
            contact=record.contact.value,
            address=record.address,
            service=record.service_name,
            slot=record.slot,
        )


class HandoffExportItem(BaseModel):
    handoff_id: str
    created_at: datetime
    reason: str
    summary: str

    @classmethod
    def of(cls, record: HandoffRecord) -> HandoffExportItem:
        return cls(
            handoff_id=record.handoff_id,
            created_at=record.created_at,
            reason=record.reason.value,
            summary=record.summary,
        )


class ConsentExportItem(BaseModel):
    purpose: str
    status: str
    statement: str
    granted_at: datetime
    withdrawn_at: datetime | None

    @classmethod
    def of(cls, record: ConsentRecord) -> ConsentExportItem:
        return cls(
            purpose=record.purpose.value,
            status=record.status.value,
            statement=record.statement,
            granted_at=record.granted_at,
            withdrawn_at=record.withdrawn_at,
        )


class TurnRecordExportItem(BaseModel):
    """One inference-plane record, exported because it holds the subject's data.

    ``content`` is the full opaque object `OBS-004` will populate — prompt,
    retrieved evidence, model output, verdicts. An export that omitted it would
    omit the subject's words; an export that truncated it would look complete.
    """

    turn_id: uuid.UUID
    session_id: uuid.UUID
    trace_id: str | None
    recorded_at: datetime
    content: dict[str, object]

    @classmethod
    def of(cls, record: TurnRecord) -> TurnRecordExportItem:
        return cls(
            turn_id=record.turn_id,
            session_id=record.session_id,
            trace_id=record.trace_id,
            recorded_at=record.recorded_at,
            content=record.content,
        )


class TurnRecordProjectionExportItem(BaseModel):
    """A derived dataset row pinned to an exported turn record.

    ``payload`` is the derived artifact itself — for `FEAT-008` the anonymized
    evaluation case — so an export that carries the projection carries the
    full projection, not a row that only names it.
    """

    projection_id: uuid.UUID
    turn_record_id: uuid.UUID
    kind: str
    created_at: datetime
    payload: dict[str, object]

    @classmethod
    def of(cls, record: TurnRecordProjection) -> TurnRecordProjectionExportItem:
        return cls(
            projection_id=record.projection_id,
            turn_record_id=record.turn_record_id,
            kind=record.kind,
            created_at=record.created_at,
            payload=record.payload,
        )


class PrivacyExportResponse(BaseModel):
    """Everything the platform holds about one subject.

    Deliberately complete: an export that omitted a table would be an export
    that looks done. The response is not tenant-scoped beyond the requested
    tenant — the operator asked for one tenant's record of a subject.
    """

    tenant_id: str
    contact_kind: str
    contact_value: str
    requested_by: str
    generated_at: datetime
    sessions: list[SessionExportItem]
    messages: list[TranscriptMessage]
    leads: list[LeadExportItem]
    bookings: list[BookingExportItem]
    handoffs: list[HandoffExportItem]
    consent: list[ConsentExportItem]
    turn_records: list[TurnRecordExportItem]
    projections: list[TurnRecordProjectionExportItem]


class DeletionRequestResponse(BaseModel):
    """One row of the erasure queue.

    The contact is echoed in canonical form: the operator who filed the
    request needs to know it was filed against the right number.
    """

    request_id: uuid.UUID
    tenant_id: str
    status: str
    contact_kind: str
    contact_value: str
    requested_by: str
    requested_at: datetime
    processed_at: datetime | None

    @classmethod
    def of(cls, record: PrivacyRequestRecord) -> DeletionRequestResponse:
        return cls(
            request_id=record.request_id,
            tenant_id=record.tenant_id,
            status=record.status,
            contact_kind=record.contact_kind,
            contact_value=record.contact_value,
            requested_by=record.requested_by,
            requested_at=record.requested_at,
            processed_at=record.processed_at,
        )


class DeletionRequestsResponse(BaseModel):
    requests: list[DeletionRequestResponse]


class TraceAccessRequest(_Request):
    """A platform administrator naming an operator for the trace-read role."""

    tenant_id: str = _TENANT_ID
    subject: str = Field(min_length=1, max_length=200)


class TraceAccessResponse(BaseModel):
    """One tenant-scoped trace-read grant, as it was recorded."""

    tenant_id: str
    subject: str
    granted_at: datetime
    granted_by: str

    @classmethod
    def of(cls, grant: TraceAccessGrant) -> TraceAccessResponse:
        return cls(
            tenant_id=grant.tenant_id,
            subject=grant.principal_subject,
            granted_at=grant.granted_at,
            granted_by=grant.granted_by,
        )


class TraceAccessesResponse(BaseModel):
    grants: list[TraceAccessResponse]


class AdminAuditEvent(BaseModel):
    """One content-free accountability row as the console renders it.

    The bounded projection only: identifiers, enums, and the server-stamped
    timestamp. The raw ``details`` dict never crosses this surface, so a
    content field that ever reaches the audit store cannot leak through the
    console (`ADR-0010`).
    """

    action: str
    actor_type: str
    principal: str | None
    tenant_id: str
    request_id: str | None
    trace_id: str | None
    resource_type: str
    resource_id: str | None
    occurred_at: datetime
    permission: str

    @classmethod
    def of(cls, event: AuditEvent, *, permission: str) -> AdminAuditEvent:
        trace_id = event.details.get("trace_id")
        return cls(
            action=event.action,
            actor_type=event.actor_type.value,
            principal=event.principal_id,
            tenant_id=event.tenant_id,
            request_id=event.request_id,
            trace_id=trace_id if isinstance(trace_id, str) and trace_id else None,
            resource_type=event.resource_type,
            resource_id=str(event.resource_id) if event.resource_id is not None else None,
            occurred_at=event.occurred_at,
            permission=permission,
        )


class AdminAuditResponse(BaseModel):
    events: list[AdminAuditEvent]
    limit: int


class AdminMembershipRole(BaseModel):
    """One live role assignment, with the assignment that produced it.

    ``granted_by`` comes from the ``membership_assigned`` audit row, never
    invented by the console; a membership that predates the audit trail shows
    its created timestamp and no issuer.
    """

    tenant_id: str
    subject: str
    role: str
    granted_by: str | None
    granted_at: datetime
    updated_at: datetime


class AdminTraceGrant(BaseModel):
    """One live PRIV-002 trace-read grant, deliberately a separate control.

    A grant and a role authorize different surfaces, so the console renders
    them as distinct tables and a viewer cannot read one as the other.
    ``expires_at`` is null today: the grant store has no expiry, which
    `FEAT-016` records as a schema gap rather than inventing one.
    """

    tenant_id: str
    subject: str
    granted_by: str
    granted_at: datetime
    expires_at: datetime | None = None


class AdminPermissionsResponse(BaseModel):
    roles: list[AdminMembershipRole]
    grants: list[AdminTraceGrant]


class TraceReadResponse(BaseModel):
    """One turn record as the trace viewer reads it, with its projections.

    The response carries the full content: this is the one surface where the
    inference plane is readable, gated by the dedicated role and audited to an
    actor, turn, and reason on every read.
    """

    turn_id: uuid.UUID
    tenant_id: str
    session_id: uuid.UUID
    trace_id: str | None
    recorded_at: datetime
    content: dict[str, object]
    projections: list[TurnRecordProjectionExportItem]

    @classmethod
    def of(
        cls,
        record: TurnRecord,
        projections: tuple[TurnRecordProjection, ...] = (),
    ) -> TraceReadResponse:
        return cls(
            turn_id=record.turn_id,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            trace_id=record.trace_id,
            recorded_at=record.recorded_at,
            content=record.content,
            projections=[TurnRecordProjectionExportItem.of(item) for item in projections],
        )


class TraceSearchResponse(BaseModel):
    """The attribution surface (`OBS-004`): records matching content-free filters.

    Only the content-free projection is repeated here — outcome, manifest hash,
    causes, turn index — so a search result is a queryable index entry, not a
    second copy of the inference plane. The record itself is fetched through the
    audited single-read route.
    """

    turn_id: uuid.UUID
    session_id: uuid.UUID
    trace_id: str | None
    recorded_at: datetime
    outcome: str
    component_manifest_hash: str
    diagnosis_causes: list[str]
    diagnosis_statuses: list[str]
    turn_index: int
    trace_schema_version: str
    source_generation_ids: list[str]

    @classmethod
    def of(cls, record: TurnRecord) -> TraceSearchResponse:
        return cls(
            turn_id=record.turn_id,
            session_id=record.session_id,
            trace_id=record.trace_id,
            recorded_at=record.recorded_at,
            outcome=record.outcome,
            component_manifest_hash=record.component_manifest_hash,
            diagnosis_causes=list(record.diagnosis_causes),
            diagnosis_statuses=list(record.diagnosis_statuses),
            turn_index=record.turn_index,
            trace_schema_version=record.trace_schema_version,
            source_generation_ids=[str(item) for item in record.source_generation_ids],
        )


class TraceSearchResponsePage(BaseModel):
    records: list[TraceSearchResponse]

    @classmethod
    def of(cls, records: tuple[TurnRecord, ...]) -> TraceSearchResponsePage:
        return cls(records=[TraceSearchResponse.of(record) for record in records])


class ComponentVersionSnapshot(BaseModel):
    """One manifest component as the turn pinned it and as this deployment
    serves it now — versions only, never content."""

    name: str
    stored: str
    current: str
    changed: bool


class ReplayOutput(BaseModel):
    """One side of a replay comparison: the prompt hash it was built from, the
    model that produced it, and the raw output."""

    content_hash: str
    model_name: str
    output_raw: str


class TraceReplayResponse(BaseModel):
    """The result of one safe replay, original and replayed side by side.

    The replay is the stored prompt sent through the *current* model with no
    tools attached, so nothing domain-effectful can be touched. Output text is
    content and this response is governed exactly like
    :class:`TraceReadResponse`: the dedicated trace-read role, audited to an
    actor, turn, and reason. ``stochastic`` is true by contract: a single
    replayed trial is an observation, never a proof.
    """

    turn_id: uuid.UUID
    recorded_at: datetime
    manifest_hash: str
    current_manifest_hash: str | None
    manifest_changed: bool
    stochastic: bool
    components: list[ComponentVersionSnapshot]
    original: ReplayOutput
    replayed: ReplayOutput
    elapsed_seconds: float = 0.0


class ReplayTrialsRequest(_Request):
    """Bounded repeated trials of the stored prompt through the current model.

    ``trials`` is capped at 5: an unbounded replay loop against a live model
    is a footgun, and a handful of trials is enough to show a stochastic
    behavior difference without pretending to be a statistical test.
    """

    trials: int = Field(ge=1, le=5, default=3)


class ReplayTrialResult(BaseModel):
    """One trial in a repeated-trial aggregate: the same prompt, same model, same
    constraints — different output by the model's own stochasticity."""

    trial_index: int
    content_hash: str
    model_name: str
    output_raw: str


class TraceReplayTrialsResponse(BaseModel):
    """The aggregate result of N bounded repeated trials.

    Every trial ran the same stored prompt through the *current* model with no
    tools. The aggregate is reported as an explicit stochastic observation:
    multiple trials show variance, but they do not constitute a statistical
    proof. ``constant`` names what was held constant (prompt, evidence, model)
    and ``variable`` names what was allowed to vary (the model's output).
    """

    turn_id: uuid.UUID
    recorded_at: datetime
    manifest_hash: str
    current_manifest_hash: str | None
    manifest_changed: bool
    stochastic: bool  # Always True for model calls
    components: list[ComponentVersionSnapshot]
    original: ReplayOutput
    trials: list[ReplayTrialResult]
    trial_count: int
    constant: str = "prompt_and_evidence"
    variable: str = "model_output"
    elapsed_seconds: float = 0.0


class GoldEvidenceSubstitution(BaseModel):
    """One gold chunk to substitute into the replay's evidence section."""

    source_id: str = Field(pattern=r"^[A-Za-z0-9._:-]{1,200}$")
    text: str = Field(min_length=1, max_length=8192)


class ReplayRetrievalRequest(_Request):
    """Generation-pinned retrieval replay, optionally with gold substitution.

    Without gold evidence, the retained generation is reranked using the stored
    resolved query. With gold evidence, those passages replace that result. A
    missing generation refuses rather than silently using current data.
    """

    gold_evidence: list[GoldEvidenceSubstitution] | None = None


class TraceReplayRetrievalResponse(BaseModel):
    """The result of a retrieval replay with an explicit generation check.

    ``generation_available`` is the reproducibility gate: ``True`` when the
    stored generation still has active chunks in the index, ``False`` when it
    does not (the route returns a 400, not this shape). ``generation_id`` is
    the stored generation the check ran against. ``gold_evidence_count`` is
    the number of gold chunks that were substituted (0 when none were provided).
    """

    turn_id: uuid.UUID
    recorded_at: datetime
    manifest_hash: str
    current_manifest_hash: str | None
    manifest_changed: bool
    stochastic: bool
    components: list[ComponentVersionSnapshot]
    original: ReplayOutput
    replayed: ReplayOutput
    generation_available: bool
    generation_id: str | None
    gold_evidence_count: int = 0
    constant: str = "query_retriever_and_index_generation"
    variable: str = "model_output"
    elapsed_seconds: float = 0.0


class ReplayTemplateRequest(_Request):
    """Template-version-pinned replay: hold model and evidence constant, pin the
    prompt template to a specific version to isolate a prompt regression.

    ``template_version`` names the retained template rendered with the stored
    bindings; ``None`` renders the original version as a reproducibility check.
    """

    template_version: int | None = Field(default=None, ge=1)


class TraceReplayTemplateResponse(BaseModel):
    """The result of a template-version-pinned replay.

    ``template_ref`` names the exact template version the replay was rendered
    with. ``template_matches_current`` is ``True`` when the pinned version is
    the same as the deployment's current version, ``False`` when it is not
    (which is the whole point of this surface).
    """

    turn_id: uuid.UUID
    recorded_at: datetime
    manifest_hash: str
    current_manifest_hash: str | None
    manifest_changed: bool
    stochastic: bool
    components: list[ComponentVersionSnapshot]
    original: ReplayOutput
    replayed: ReplayOutput
    template_ref: str
    template_matches_current: bool
    constant: str = "replay_model_evidence_history_and_bindings"
    variable: str = "prompt_template_and_model_output"
    elapsed_seconds: float = 0.0


class GoldEvidenceItem(BaseModel):
    """One reviewer-labelled chunk a Gate B case is anchored to."""

    source_id: str
    text: str


class GoldCaseResponse(BaseModel):
    """One eval fixture case, served so the explorer can overlay it on a turn.

    The gold evidence is synthetic evaluation content, not visitor data, but it
    is still evidence-like text, so it is served only under the trace-read role
    and audited — the same governance the inference plane itself gets.
    """

    case_id: str
    tenant_id: str
    scenario: str | None
    query: str
    gold_chunks: list[GoldEvidenceItem]


class GoldCasesResponse(BaseModel):
    cases: list[GoldCaseResponse]


class JobControlRequest(_Request):
    """Tenant binding for an operator mutation; the URL names the job only."""

    tenant_id: str = _TENANT_ID


class AdminJob(BaseModel):
    """Safe job metadata. Payloads may contain PII and never reach this plane."""

    job_id: uuid.UUID
    tenant_id: str
    kind: str
    status: str
    attempt_count: int
    max_attempts: int
    replay_count: int
    available_at: datetime
    lease_expires_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def of(cls, record: JobRecord) -> AdminJob:
        return cls(
            job_id=record.job_id,
            tenant_id=record.tenant_id,
            kind=record.kind.value,
            status=record.status.value,
            attempt_count=record.attempt_count,
            max_attempts=record.max_attempts,
            replay_count=record.replay_count,
            available_at=record.available_at,
            lease_expires_at=record.lease_expires_at,
            last_error_code=record.last_error_code,
            created_at=record.created_at,
            updated_at=record.updated_at,
            completed_at=record.completed_at,
        )


class AdminJobEvent(BaseModel):
    event_id: int
    event: str
    actor_type: str
    actor_id: str | None
    request_id: str | None
    details: dict[str, object]
    occurred_at: datetime

    @classmethod
    def of(cls, record: JobEvent) -> AdminJobEvent:
        return cls(
            event_id=record.event_id,
            event=record.event.value,
            actor_type=record.actor_type,
            actor_id=record.actor_id,
            request_id=record.request_id,
            details=record.details,
            occurred_at=record.occurred_at,
        )


class AdminJobsResponse(BaseModel):
    jobs: list[AdminJob]
    limit: int


class AdminJobDetailResponse(BaseModel):
    job: AdminJob
    events: list[AdminJobEvent]


class UploadedVersionResponse(BaseModel):
    """One staged draft, as the uploading operator reads it.

    Identifiers and approval state only: the stored content lives in object
    storage and never crosses this surface.
    """

    document_id: uuid.UUID
    version_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    external_key: str
    revision: int
    state: str
    indexing_state: str
    checksum: str
    byte_size: int
    media_type: str
    visibility: str

    @classmethod
    def of(cls, document: KnowledgeDocument, version: DocumentVersion) -> UploadedVersionResponse:
        return cls(
            document_id=document.document_id,
            version_id=version.version_id,
            source_id=document.source.source_id,
            source_name=document.source.display_name,
            external_key=document.external_key,
            revision=version.revision,
            state=version.state.value,
            indexing_state=version.indexing_state.value,
            checksum=version.checksum.value,
            byte_size=version.byte_size,
            media_type=version.media_type,
            visibility=version.visibility.value,
        )


class IndexFindingSummary(BaseModel):
    """One bounded index-integrity finding, safe for operator surfaces.

    Carries the fault code, the tenant-qualified source version, and the index
    generation involved. ``detail`` is bounded by the detector's construction
    and is published as-is; document content cannot reach it.
    """

    code: str
    tenant_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    generation_id: uuid.UUID | None
    detected_at: datetime
    detail: dict[str, object]

    @classmethod
    def of(cls, finding: IndexIntegrityFinding) -> IndexFindingSummary:
        return cls(
            code=finding.code.value,
            tenant_id=finding.tenant_id,
            document_id=finding.document_id,
            version_id=finding.version_id,
            generation_id=finding.generation_id,
            detected_at=finding.detected_at,
            detail=dict(finding.detail),
        )


class IndexFindingsResponse(BaseModel):
    findings: list[IndexFindingSummary]
    limit: int


class QuarantinedVersionSummary(BaseModel):
    """One quarantined version awaiting review, content-free by construction.

    Identifiers and states only: the text that triggered the quarantine lives
    in object storage, never on this surface.
    """

    version_id: uuid.UUID
    document_id: uuid.UUID
    source_id: uuid.UUID
    source_name: str
    external_key: str
    title: str
    revision: int
    state: str
    visibility: str

    @classmethod
    def of(cls, document: KnowledgeDocument, version: DocumentVersion) -> QuarantinedVersionSummary:
        return cls(
            version_id=version.version_id,
            document_id=document.document_id,
            source_id=document.source.source_id,
            source_name=document.source.display_name,
            external_key=document.external_key,
            title=document.title,
            revision=version.revision,
            state=version.state.value,
            visibility=version.visibility.value,
        )


class QuarantineListResponse(BaseModel):
    versions: list[QuarantinedVersionSummary]
    limit: int


class QuarantineReviewRequest(BaseModel):
    approved: bool
    reviewed_by: str = Field(min_length=1, max_length=120)
    model_config = ConfigDict(extra="forbid")


class QuarantineReviewResponse(BaseModel):
    version_id: uuid.UUID
    document_id: uuid.UUID
    state: str
    safety_state: str

    @classmethod
    def of(cls, document: KnowledgeDocument, version: DocumentVersion) -> QuarantineReviewResponse:
        return cls(
            version_id=version.version_id,
            document_id=document.document_id,
            state=version.state.value,
            safety_state=version.safety_state.value,
        )


class KnowledgeSourceCreateRequest(_Request):
    """Create a source under the caller's tenant.

    Idempotent on ``(tenant, domain, display_name)``: re-running onboarding
    returns the existing source rather than splitting one body of content.
    """

    tenant_id: str = _TENANT_ID
    domain: str = Field(min_length=2, max_length=63, pattern=r"^[a-z][a-z0-9-]{1,62}$")
    kind: Literal["upload", "url", "manual"] = "upload"
    display_name: str = Field(min_length=1, max_length=200)


class KnowledgeSourceResponse(BaseModel):
    source_id: uuid.UUID
    tenant_id: str
    domain: str
    kind: str
    display_name: str
    enabled: bool

    @classmethod
    def of(cls, source: KnowledgeSource) -> KnowledgeSourceResponse:
        return cls(
            source_id=source.source_id,
            tenant_id=source.tenant_id,
            domain=source.domain.value,
            kind=source.kind.value,
            display_name=source.display_name,
            enabled=source.enabled,
        )


class KnowledgeVersionSummary(BaseModel):
    """One version as the operator reads it, with its index generation merged.

    Content-free by construction: identifiers, states, counts, and timestamps
    only. The chunk count and embedding model come from the persisted
    generation record (`RAG-002`), so an operator can tell "indexed, and here
    is what the index says it holds" from "indexing never finished" without
    touching infrastructure logs.
    """

    version_id: uuid.UUID
    revision: int
    state: str
    indexing_state: str
    safety_state: str
    visibility: str
    checksum: str
    byte_size: int
    media_type: str
    approved_at: datetime | None
    published_at: datetime | None
    superseded_at: datetime | None
    indexed_at: datetime | None
    effective_at: datetime | None
    expires_at: datetime | None
    index_error_code: str | None
    generation_status: str | None
    chunk_count: int
    embedding_model: str | None

    @classmethod
    def of(
        cls,
        version: DocumentVersion,
        generation: IndexGeneration | None,
    ) -> KnowledgeVersionSummary:
        return cls(
            version_id=version.version_id,
            revision=version.revision,
            state=version.state.value,
            indexing_state=version.indexing_state.value,
            safety_state=version.safety_state.value,
            visibility=version.visibility.value,
            checksum=version.checksum.value,
            byte_size=version.byte_size,
            media_type=version.media_type,
            approved_at=version.approved_at,
            published_at=version.published_at,
            superseded_at=version.superseded_at,
            indexed_at=version.indexed_at,
            effective_at=version.effective_at,
            expires_at=version.expires_at,
            index_error_code=version.index_error_code,
            generation_status=generation.status.value if generation is not None else None,
            chunk_count=generation.chunk_count if generation is not None else 0,
            embedding_model=generation.embedding_model if generation is not None else None,
        )


class KnowledgeDocumentSummary(BaseModel):
    document_id: uuid.UUID
    source_id: uuid.UUID
    external_key: str
    title: str
    deleted: bool
    versions: list[KnowledgeVersionSummary]

    @classmethod
    def of(
        cls,
        document: KnowledgeDocument,
        generations: Mapping[uuid.UUID, IndexGeneration],
    ) -> KnowledgeDocumentSummary:
        return cls(
            document_id=document.document_id,
            source_id=document.source.source_id,
            external_key=document.external_key,
            title=document.title,
            deleted=document.deleted,
            versions=[
                KnowledgeVersionSummary.of(version, generations.get(version.version_id))
                for version in document.versions
            ],
        )


class KnowledgeSourceSummary(BaseModel):
    source_id: uuid.UUID
    tenant_id: str
    domain: str
    kind: str
    display_name: str
    enabled: bool
    documents: list[KnowledgeDocumentSummary]

    @classmethod
    def of(
        cls,
        source: KnowledgeSource,
        documents: Sequence[KnowledgeDocument],
        generations: Mapping[uuid.UUID, IndexGeneration],
    ) -> KnowledgeSourceSummary:
        return cls(
            source_id=source.source_id,
            tenant_id=source.tenant_id,
            domain=source.domain.value,
            kind=source.kind.value,
            display_name=source.display_name,
            enabled=source.enabled,
            documents=[
                KnowledgeDocumentSummary.of(document, generations) for document in documents
            ],
        )


class KnowledgeResponse(BaseModel):
    sources: list[KnowledgeSourceSummary]
    limit: int


class KnowledgeDocumentDetailResponse(BaseModel):
    document: KnowledgeDocumentSummary

    @classmethod
    def of(
        cls,
        document: KnowledgeDocument,
        generations: Mapping[uuid.UUID, IndexGeneration],
    ) -> KnowledgeDocumentDetailResponse:
        return cls(document=KnowledgeDocumentSummary.of(document, generations))


class KnowledgeVersionActionRequest(_Request):
    """Tenant binding for a version mutation; the URL names the version."""

    tenant_id: str = _TENANT_ID
    effective_at: datetime | None = None
    expires_at: datetime | None = None


class KnowledgeSourceEnabledRequest(_Request):
    tenant_id: str = _TENANT_ID
    enabled: bool


class KnowledgeDeleteRequest(_Request):
    tenant_id: str = _TENANT_ID


class KnowledgeVersionActionResponse(BaseModel):
    """One version after a lifecycle mutation, plus the ingestion job a
    publish or reindex enqueued (``None`` when the action enqueued none)."""

    version: KnowledgeVersionSummary
    job: AdminJob | None

    @classmethod
    def of(
        cls,
        document: KnowledgeDocument,
        version: DocumentVersion,
        generations: Mapping[uuid.UUID, IndexGeneration],
        *,
        job: JobRecord | None = None,
    ) -> KnowledgeVersionActionResponse:
        return cls(
            version=KnowledgeVersionSummary.of(version, generations.get(version.version_id)),
            job=AdminJob.of(job) if job is not None else None,
        )


class KnowledgePreviewBlock(BaseModel):
    location: str
    text: str


class KnowledgePreviewResponse(BaseModel):
    """A bounded preview of one version's parsed content.

    The operator's own document, rendered as the pipeline will split it: the
    parser that would run, the chunk count it would produce, and a bounded
    window of its source blocks — never the raw file, which stays in object
    storage.
    """

    version_id: uuid.UUID
    document_id: uuid.UUID
    title: str
    media_type: str
    parser_version: str
    chunk_count: int
    blocks: list[KnowledgePreviewBlock]


class KnowledgeFindingSummary(BaseModel):
    """One bounded index-integrity finding linked to the affected source
    version, for the admin console.

    The fault fields are exactly the detector's (content-free by construction);
    the source metadata makes the console render which source version a fault
    names without a second lookup.
    """

    code: str
    tenant_id: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    generation_id: uuid.UUID | None
    detected_at: datetime
    detail: dict[str, object]
    source_name: str | None = None
    document_title: str | None = None
    revision: int | None = None

    @classmethod
    def of(cls, finding: IndexIntegrityFinding) -> KnowledgeFindingSummary:
        return cls(
            code=finding.code.value,
            tenant_id=finding.tenant_id,
            document_id=finding.document_id,
            version_id=finding.version_id,
            generation_id=finding.generation_id,
            detected_at=finding.detected_at,
            detail=dict(finding.detail),
        )


class KnowledgeFindingsResponse(BaseModel):
    findings: list[KnowledgeFindingSummary]
    limit: int


class FeedbackRequest(_Request):
    """One visitor's rating of one turn record.

    The turn is named by its record id, which the turn response now echoes;
    the server verifies the record belongs to the credential's tenant *and*
    session before anything is written, so a forged or borrowed id cannot
    attach feedback to a conversation the visitor never took part in
    (acceptance 1). ``reason`` is optional and bounded.
    """

    turn_id: uuid.UUID
    rating: Literal["up", "down"]
    reason: str | None = Field(default=None, min_length=1, max_length=1000)


class FeedbackResponse(BaseModel):
    """The recorded rating, as the widget's next state reads it."""

    turn_id: uuid.UUID
    rating: str
    reason: str | None
    created_at: datetime

    @classmethod
    def of(cls, record: TurnFeedback) -> FeedbackResponse:
        return cls(
            turn_id=record.turn_id,
            rating=record.rating,
            reason=record.reason,
            created_at=record.created_at,
        )


class ReviewSummaryResponse(BaseModel):
    """One content-free queue entry, as the list surface shows it.

    The diagnosis causes and statuses are the turn record's content-free
    columns, so a reviewer can scan the queue without a single content read;
    the feedback reason and the turn itself stay behind the audited detail
    surface.
    """

    review_id: uuid.UUID
    turn_id: uuid.UUID
    session_id: uuid.UUID | None
    recorded_at: datetime | None
    outcome: str
    source: str
    status: str
    priority: int
    recurrence: int
    manifest_hash: str
    committed_actions: bool
    novel_manifest: bool
    case_id: str | None
    verdict: str | None
    diagnosis_causes: list[str]
    diagnosis_statuses: list[str]
    closing_eval_run_id: str | None
    closing_eval_case_id: str | None
    created_at: datetime
    turn_index: int = 0

    @classmethod
    def of(cls, case: ReviewCase, turn: TurnRecord) -> ReviewSummaryResponse:
        return cls(
            review_id=case.review_id,
            turn_id=case.turn_id,
            session_id=turn.session_id,
            recorded_at=turn.recorded_at,
            outcome=turn.outcome,
            source=case.source,
            status=case.status,
            priority=case.priority,
            recurrence=case.recurrence,
            manifest_hash=case.manifest_hash,
            committed_actions=case.committed_actions,
            novel_manifest=case.novel_manifest,
            case_id=case.case_id,
            verdict=case.verdict,
            diagnosis_causes=list(turn.diagnosis_causes),
            diagnosis_statuses=list(turn.diagnosis_statuses),
            closing_eval_run_id=case.closing_eval_run_id,
            closing_eval_case_id=case.closing_eval_case_id,
            created_at=case.created_at,
            turn_index=turn.turn_index,
        )


class ReviewPageResponse(BaseModel):
    reviews: list[ReviewSummaryResponse]

    @classmethod
    def of(
        cls, cases: tuple[ReviewCase, ...], turns: Mapping[uuid.UUID, TurnRecord]
    ) -> ReviewPageResponse:
        return cls(reviews=[ReviewSummaryResponse.of(case, turns[case.turn_id]) for case in cases])


class ReviewDiagnosisDecisionRequest(_Request):
    """One reviewer decision about one automatic diagnosis (or a new one).

    ``automatic_index`` is required for ``confirms``/``rejects``/``amends``
    and forbidden for ``adds``; the amended replacement fields are required
    for an ``amends`` row, validated by the domain service against the turn's
    actual automatic diagnosis list.
    """

    automatic_index: int | None = None
    relationship: Literal["confirms", "rejects", "amends", "adds"]
    cause: str = Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")
    stage: str = Field(default="outcome", min_length=1, max_length=64)
    role: Literal["primary", "contributing"] = "primary"
    status: Literal["detected", "suspected", "confirmed", "inconclusive"] = "confirmed"
    confidence: Literal["low", "medium", "high"] = "medium"
    evidence: list[str] = Field(default_factory=list, max_length=32)
    note: str | None = Field(default=None, min_length=1, max_length=2000)


class ReviewSubmitRequest(_Request):
    """The reviewer's full decision for one queue entry.

    ``status`` is the destination: ``awaiting_fix`` when the reviewer
    documented a fix and the case stays visibly open until an evaluation run
    passes it, ``rejected`` when the reviewer dismissed the problem. The
    corrected answer is stored beside the turn — the original trace is never
    rewritten (acceptance 2).
    """

    tenant_id: str = _TENANT_ID
    verdict: Literal["confirmed", "rejected", "amended"]
    status: Literal["awaiting_fix", "rejected"]
    note: str | None = Field(default=None, min_length=1, max_length=2000)
    corrected_answer: str | None = Field(default=None, min_length=1, max_length=4000)
    proposed_fix: str | None = Field(default=None, min_length=1, max_length=2000)
    diagnoses: list[ReviewDiagnosisDecisionRequest] = Field(default_factory=list)


class ReviewDiagnosisResponse(BaseModel):
    """One reviewer-authored diagnosis row, with its relationship to the
    detector's record made explicit — the disagreement is stored, never
    silently overwritten (acceptance 4)."""

    diagnosis_id: uuid.UUID
    review_id: uuid.UUID
    relationship: str
    automatic_index: int | None
    cause: str
    stage: str
    role: str
    status: str
    confidence: str
    evidence: list[str]
    note: str | None
    created_at: datetime

    @classmethod
    def of(cls, record: ReviewDiagnosis) -> ReviewDiagnosisResponse:
        return cls(
            diagnosis_id=record.diagnosis_id,
            review_id=record.review_id,
            relationship=record.relationship,
            automatic_index=record.automatic_index,
            cause=record.cause,
            stage=record.stage,
            role=record.role,
            status=record.status,
            confidence=record.confidence,
            evidence=list(record.evidence),
            note=record.note,
            created_at=record.created_at,
        )


class ReviewDetailResponse(BaseModel):
    """One queue entry with everything a reviewer needs, content-bearing.

    The feedback reason and the reviewer's own records are visitor and staff
    content, so this surface sits under the same dedicated trace-read role and
    audit rules as the turn record itself; the turn's content is fetched
    through the existing single-read route.
    """

    review: ReviewSummaryResponse
    feedback: FeedbackResponse | None
    reviewer_subject: str | None
    reviewed_at: datetime | None
    verdict_note: str | None
    corrected_answer: str | None
    proposed_fix: str | None
    closing_eval_passed_at: datetime | None
    diagnoses: list[ReviewDiagnosisResponse]

    @classmethod
    def of(
        cls,
        case: ReviewCase,
        turn: TurnRecord,
        *,
        feedback: TurnFeedback | None,
        diagnoses: tuple[ReviewDiagnosis, ...],
    ) -> ReviewDetailResponse:
        return cls(
            review=ReviewSummaryResponse.of(case, turn),
            feedback=None if feedback is None else FeedbackResponse.of(feedback),
            reviewer_subject=case.reviewer_subject,
            reviewed_at=case.reviewed_at,
            verdict_note=case.verdict_note,
            corrected_answer=case.corrected_answer,
            proposed_fix=case.proposed_fix,
            closing_eval_passed_at=case.closing_eval_passed_at,
            diagnoses=[ReviewDiagnosisResponse.of(record) for record in diagnoses],
        )


class ReviewDecisionResponse(BaseModel):
    """The queue entry after a take, submit, or promote mutation."""

    review_id: uuid.UUID
    turn_id: uuid.UUID
    status: str
    verdict: str | None
    case_id: str | None
    closing_eval_run_id: str | None
    closing_eval_case_id: str | None
    closing_eval_passed_at: datetime | None
    corrected_answer: str | None

    @classmethod
    def of(cls, case: ReviewCase) -> ReviewDecisionResponse:
        return cls(
            review_id=case.review_id,
            turn_id=case.turn_id,
            status=case.status,
            verdict=case.verdict,
            case_id=case.case_id,
            closing_eval_run_id=case.closing_eval_run_id,
            closing_eval_case_id=case.closing_eval_case_id,
            closing_eval_passed_at=case.closing_eval_passed_at,
            corrected_answer=case.corrected_answer,
        )
