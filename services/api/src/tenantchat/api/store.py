"""Persistence contracts and explicit in-memory test doubles.

The contracts expose server-issued conversation IDs and append-only message
operations. There is intentionally no method that accepts or replaces a
transcript. PostgreSQL implementations live in :mod:`tenantchat.api.persistence`;
the fakes in this module are injected only by hermetic tests.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Protocol

from tenantchat.core.commands import (
    BookingCommand,
    HandoffCommand,
    HandoffReason,
    LeadCommand,
    LeadUrgency,
)
from tenantchat.core.contact import Contact, ContactKind
from tenantchat.core.errors import (
    ConflictError,
    HandoffOwnershipError,
    HandoffTransitionError,
    NotFoundError,
    ReviewTransitionError,
    SlotUnavailableError,
)
from tenantchat.core.handoffs import (
    ACCEPTABLE_STATUSES,
    RELEASABLE_STATUSES,
    RESOLVABLE_STATUSES,
    HandoffStatus,
)
from tenantchat.core.knowledge import (
    ContentChecksum,
    DocumentVersion,
    KnowledgeDocument,
    KnowledgeDomain,
    KnowledgeSource,
    RetrievalContext,
    SourceKind,
    Visibility,
)
from tenantchat.core.lifecycle import IndexingState, VersionState
from tenantchat.core.ports import IdempotencyKey
from tenantchat.core.privacy import (
    ANONYMIZED_NAME,
    ConsentGrant,
    ConsentPurpose,
    ConsentStatus,
    DataClass,
    RetentionPolicy,
)
from tenantchat.core.routing import (
    IntentCandidate,
    IntentName,
    RoutingDecision,
    RoutingOutcome,
    RoutingRule,
)
from tenantchat.core.safety import SafetyState
from tenantchat.core.workflows import (
    ToolResult,
    WorkflowState,
    WorkflowStatus,
    WorkflowTransition,
    transition_workflow,
)


def _reference(prefix: str) -> str:
    # The full UUID hex, matching the "HO-<uuid.hex>" form the Postgres stores
    # build, so the queue's public identifier is the same shape in every store.
    return f"{prefix}-{uuid.uuid4().hex.upper()}"


# How long an idempotency claim lives, mirroring the PostgreSQL store's
# RETENTION: the fake must expire crashed claims on the same clock the real
# one does, or a hermetic test proves a recovery the database would refuse.
IDEMPOTENCY_RETENTION: Final = timedelta(days=7)


class MessageRole(StrEnum):
    VISITOR = "visitor"
    ASSISTANT = "assistant"
    STAFF = "staff"
    SYSTEM = "system"
    TOOL = "tool"


class AuditActorType(StrEnum):
    """Who caused an audit event. ``visitor`` is absent: visitor actions are
    recorded against their own tables and `SEC-002` scopes them to a session."""

    STAFF = "staff"
    SERVICE = "service"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class ConversationRecord:
    session_id: uuid.UUID
    tenant_id: str
    status: str
    outcome: str
    version: int
    started_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None


@dataclass(frozen=True, slots=True)
class MessageRecord:
    message_id: uuid.UUID
    tenant_id: str
    session_id: uuid.UUID
    sequence_number: int
    role: MessageRole
    content: str
    model_name: str | None
    metadata: dict[str, object]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class BookingRecord:
    booking_id: str
    tenant_id: str
    session_id: str
    customer_name: str
    contact: Contact
    address: str
    service_slug: str
    service_name: str
    slot: str
    slot_id: str
    slot_start: datetime
    slot_end: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class HandoffRecord:
    """One handoff row, with the full staff-ownership state (`FEAT-004`).

    ``status`` is the closed ``handoff_status`` vocabulary; the assignment
    fields are ``None`` together (the schema enforces the pairing), and a
    release clears them while stamping ``released_at``.
    """

    handoff_id: str
    tenant_id: str
    session_id: str
    reason: HandoffReason
    summary: str
    created_at: datetime
    status: str = "requested"
    assigned_principal_id: str | None = None
    assigned_at: datetime | None = None
    released_at: datetime | None = None
    resolved_at: datetime | None = None
    resolved_by_principal_id: str | None = None


@dataclass(frozen=True, slots=True)
class LeadRecord:
    lead_id: str
    tenant_id: str
    session_id: str
    customer_name: str
    contact: Contact
    service: str
    service_slug: str | None
    summary: str
    address_or_zip: str
    urgency: LeadUrgency
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TenantMembership:
    """One operator's role inside one tenant (SEC-001 per-tenant RBAC)."""

    tenant_id: str
    principal_subject: str
    role: str
    created_at: datetime
    updated_at: datetime


AUDIT_ACTIONS: Final = frozenset(
    {
        "audit.read",
        "handoff.accepted",
        "handoff.released",
        "handoff.resolved",
        "knowledge.document_deleted",
        "knowledge.quarantine",
        "knowledge.quarantine_review",
        "knowledge.source_created",
        "knowledge.source_enabled",
        "knowledge.version_approved",
        "knowledge.version_expired",
        "knowledge.version_published",
        "knowledge.version_reindexed",
        "jobs.read",
        "membership_assigned",
        "membership_revoked",
        "permissions.read",
        "privacy.deletion_requested",
        "privacy.erased",
        "privacy.export",
        "privacy.retention_purged",
        "review.decided",
        "review.promoted",
        "review.read",
        "review.search",
        "review.taken",
        "staff_reply_sent",
        "trace.gold_read",
        "trace.read",
        "trace.read_refused",
        "trace.replay",
        "trace.replay_retrieval",
        "trace.replay_template",
        "trace.replay_trials",
        "trace.search",
        "trace_access.granted",
        "trace_access.revoked",
    }
)
"""Every action an audit record may carry.

The operator's action filter offers exactly this set, so an event type that
reaches the table but not the filter is a test failure rather
than something an operator discovers mid-incident. `tests/test_audit_taxonomy.py`
holds both ends to it: the routers may emit nothing outside it, and the admin
console's `AUDIT_ACTIONS` must list all of it.
"""


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """One accountability record: who, what, where, and which request caused it.

    ``details`` is server-authored context (IDs, roles, version numbers) only.
    Message content and customer contact details belong to the business tables
    and to `PRIV-001`'s retention rules, never duplicated into this log.
    """

    tenant_id: str
    actor_type: AuditActorType
    principal_id: str | None
    action: str
    resource_type: str
    resource_id: uuid.UUID | None
    request_id: str | None
    details: dict[str, object]
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class ConversationStore(Protocol):
    async def create(self, tenant_id: str) -> ConversationRecord: ...

    async def get(self, tenant_id: str, session_id: uuid.UUID) -> ConversationRecord: ...

    async def append(
        self,
        tenant_id: str,
        session_id: uuid.UUID,
        *,
        role: MessageRole,
        content: str,
        model_name: str | None = None,
        metadata: dict[str, object] | None = None,
        audit_event: AuditEvent | None = None,
    ) -> MessageRecord:
        """Append one message.

        ``audit_event``, when given, is persisted in the same transaction as
        the message (R-39): the staff reply and the row that vouches for it
        commit together or not at all. The row names the message it vouches
        for: the store records the persisted id under ``message_id`` in the
        event's details, so the event can be built before the id exists.
        """

    async def transcript(
        self, tenant_id: str, session_id: uuid.UUID
    ) -> tuple[MessageRecord, ...]: ...

    async def for_tenant(self, tenant_id: str, *, limit: int) -> tuple[ConversationRecord, ...]:
        """Conversations that have something to read, most recently active first.

        A conversation with no messages is excluded. It is either one that was
        opened and abandoned before the visitor typed, or one of the write-only
        rows a booking or lead correlates against, and neither is a transcript
        an operator can act on.
        """
        ...

    async def message_counts(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        """Message counts per conversation, for the queue's stat strip."""
        ...

    async def last_messages(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, MessageRecord]:
        """Each conversation's most recent message, for the queue preview.

        Missing sessions are absent from the result; the preview is a
        convenience over the transcript, never a second system of record.
        """
        ...


@dataclass(frozen=True, slots=True)
class BookingAttempt:
    """The identity of one intended booking: its idempotency key and fingerprint.

    Everything needed to distinguish "this is the same attempt again" from "this
    is a new attempt" in one value, so the reservation can claim the key and
    commit the booking in the same transaction.
    """

    tenant_id: str
    scope: str
    key: IdempotencyKey
    request_hash: str


@dataclass(frozen=True, slots=True)
class BookingOutcome:
    """What a booking attempt produced: the committed record, and whether it was a retry."""

    record: BookingRecord
    replayed: bool


class BookingStore(Protocol):
    async def confirm(
        self,
        command: BookingCommand,
        *,
        session_id: str,
        attempt: BookingAttempt,
    ) -> BookingOutcome:
        """Reserve and confirm the slot once per attempt, in one transaction.

        The idempotency claim, the slot reservation, and the booking write
        commit together (`DATA-003`), so a retry returns the original record and
        a racing attempt on the same slot loses to the uniqueness constraint.

        Raises:
            NotFoundError: the tenant or conversation is absent/outside tenant.
            ConflictError: the key was used for a materially different booking.
            SlotUnavailableError: the slot is past, reserved, or wrong tenant.
        """
        ...

    async def replay(self, tenant_id: str, scope: str, key: str) -> BookingRecord | None:
        """The committed booking this key produced, or ``None`` if it never booked.

        Read with the tenant so a key minted for one tenant can never resolve
        another's booking. Used by
        :meth:`~tenantchat.api.actions.RecordedBookingService.find_replay` so a
        repeated key is answered before the slot is re-validated.
        """
        ...

    async def for_tenant(self, tenant_id: str, *, limit: int = 200) -> tuple[BookingRecord, ...]:
        """Confirmed bookings, oldest first, bounded to *limit*."""
        ...

    async def count_for_tenant(self, tenant_id: str) -> int:
        """Every confirmed booking the tenant holds, for console pagination."""
        ...

    async def for_session(self, tenant_id: str, session_id: str) -> tuple[BookingRecord, ...]:
        """The bookings confirmed in one conversation, oldest first."""
        ...

    async def counts_by_session(
        self, tenant_id: str, session_ids: Collection[str]
    ) -> dict[str, int]:
        """Booking counts per conversation, for the queue's stat strip."""
        ...


class LeadStore(Protocol):
    async def record(self, command: LeadCommand, *, session_id: str) -> LeadRecord: ...

    async def for_tenant(self, tenant_id: str, *, limit: int = 200) -> tuple[LeadRecord, ...]:
        """Captured leads, oldest first, bounded to *limit*."""
        ...

    async def count_for_tenant(self, tenant_id: str) -> int:
        """Every lead the tenant holds, for console pagination."""
        ...

    async def for_session(self, tenant_id: str, session_id: str) -> tuple[LeadRecord, ...]:
        """The leads captured in one conversation, oldest first."""
        ...

    async def counts_by_session(
        self, tenant_id: str, session_ids: Collection[str]
    ) -> dict[str, int]:
        """Lead counts per conversation, for the queue's stat strip."""
        ...


class HandoffStore(Protocol):
    """Authoritative handoff rows, from escalation to staff resolution.

    ``record`` and ``for_tenant`` are the escalation side (`ARCH-001`); the
    ownership operations are the `FEAT-004` staff side. Every ownership
    mutation is a conditional write: the store applies it only from a status
    the transition permits, so a race to accept has exactly one winner no
    matter how many consoles fire at once — the database, not a UI lock, is
    the arbiter.
    """

    async def record(self, command: HandoffCommand, *, session_id: str) -> HandoffRecord:
        """Open a handoff and move the conversation to ``waiting_for_staff``.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
        """

    async def for_tenant(self, tenant_id: str) -> tuple[HandoffRecord, ...]:
        """Every handoff row for a tenant, oldest first."""

    async def open_for_tenant(self, tenant_id: str, *, limit: int) -> tuple[HandoffRecord, ...]:
        """The tenant's open queue (``requested``/``queued``/``assigned``).

        The staff queue reads this, oldest first and bounded to ``limit``.
        Resolved and cancelled rows are history, not work.
        """

    async def get(self, tenant_id: str, handoff_id: str) -> HandoffRecord:
        """One handoff row, tenant-qualified.

        Raises:
            NotFoundError: no such handoff, or it belongs to another tenant.
        """

    async def for_session(self, tenant_id: str, session_id: str) -> HandoffRecord | None:
        """The most recent handoff for one conversation, or ``None``.

        The visitor-turn gate reads this: it is the conversation's current
        state, and the router decides whether the agent may answer from its
        ``status``.
        """

    async def accept(
        self,
        tenant_id: str,
        handoff_id: str,
        *,
        principal_id: str,
        audit_event: AuditEvent | None = None,
    ) -> HandoffRecord:
        """Assign an unowned handoff to one staff member, atomically.

        ``audit_event``, when given, is persisted in the same transaction as
        the transition (R-39).

        Raises:
            NotFoundError: no such handoff, or it belongs to another tenant.
            HandoffTransitionError: the handoff already has an owner or closed.
        """

    async def release(
        self,
        tenant_id: str,
        handoff_id: str,
        *,
        principal_id: str,
        administrative: bool = False,
        audit_event: AuditEvent | None = None,
    ) -> HandoffRecord:
        """Release an assigned handoff back to the queue and resume the agent.

        ``administrative`` admits a supervisor releasing a colleague's stale
        assignment (the staff-disconnect recovery path); otherwise only the
        current owner may release.

        Raises:
            NotFoundError: no such handoff, or it belongs to another tenant.
            HandoffTransitionError: not assigned, or held by someone else and
                the caller is not authorized to release it.
        """

    async def resolve(
        self,
        tenant_id: str,
        handoff_id: str,
        *,
        principal_id: str,
        administrative: bool = False,
        audit_event: AuditEvent | None = None,
    ) -> HandoffRecord:
        """Close an open handoff and mark the conversation closed.

        Any staff member may resolve an unowned handoff; an assigned one is
        resolved by its owner, or by a supervisor when ``administrative``.

        Raises:
            NotFoundError: no such handoff, or it belongs to another tenant.
            HandoffTransitionError: the handoff is not open, or is held by
                someone else and the caller is not authorized to resolve it.
        """


class MembershipStore(Protocol):
    """Per-tenant operator role assignment (SEC-001).

    ``assign`` is an upsert: the record's identity is the (tenant, subject)
    pair, and re-assigning a role replaces the old one rather than erroring.
    ``revoke`` returns whether a row was removed, so a caller can distinguish
    "revoked" from "had never been assigned" without probing existence.
    """

    async def role_for(self, tenant_id: str, subject: str) -> str | None: ...

    async def assign(self, *, tenant_id: str, subject: str, role: str) -> TenantMembership: ...

    async def revoke(self, *, tenant_id: str, subject: str) -> bool: ...

    async def for_principal(self, subject: str) -> tuple[TenantMembership, ...]: ...

    async def for_tenant(self, tenant_id: str) -> tuple[TenantMembership, ...]:
        """Everyone with a role inside one tenant, for the permissions console."""
        ...


class AuditStore(Protocol):
    """Append-only accountability records, one per administrative mutation.

    ``record`` is fire-and-forget from the caller's perspective: the event's
    occurred_at is authoritative, so the implementation stamps it. Reads are
    tenant-qualified only; `PRIV-001` consumes this surface for retention,
    export, and erasure of an operator's records.
    """

    async def record(self, event: AuditEvent) -> AuditEvent: ...

    async def for_tenant(
        self,
        tenant_id: str,
        *,
        limit: int = 200,
        since: datetime | None = None,
        until: datetime | None = None,
        actions: tuple[str, ...] = (),
        principal: str | None = None,
    ) -> tuple[AuditEvent, ...]:
        """The tenant's events, newest first, bounded to *limit*.

        The filters are the `FEAT-016` trail query surface: a time window, a
        closed set of action names, and one principal — never free text, so a
        tenant ID cannot become a search token. Each filter narrows the tenant
        rows before the bound is applied.
        """
        ...


class IdempotencyStore(Protocol):
    """Remembers which effects have already been attempted, and how they ended.

    Two phases, because the record has to exist *before* the action does. A store
    that wrote only on success would let two concurrent retries both find nothing
    and both commit. `DATA-003` collapses the two phases and the booking write
    into a single transaction, at which point a crash between them stops being
    representable at all.
    """

    async def begin(
        self,
        tenant_id: str,
        *,
        scope: str,
        key: IdempotencyKey,
        fingerprint: str,
    ) -> dict[str, object] | None:
        """Claim the key, or return the completed response it already has.

        Returns ``None`` when the caller now owns the attempt and must perform
        the action and then call :meth:`complete`.

        Raises:
            ConflictError: an attempt with this key is still in flight, or the
                same key was used for a materially different request.
        """
        ...

    async def complete(
        self,
        tenant_id: str,
        *,
        scope: str,
        key: IdempotencyKey,
        response: dict[str, object],
    ) -> None:
        """Record what the committed action produced.

        Raises:
            NotFoundError: the key was never claimed by :meth:`begin`.
        """
        ...

    async def sweep(self) -> int:
        """Drop rows past retention (finished answers, crashed claims).

        Returns the number of rows removed, so the caller can observe the
        working set actually shrinking.
        """
        ...


class InMemoryConversationStore:
    """A concurrency-safe fake; production composition never constructs it.

    ``audit`` is where an ``append``-carried audit row lands (R-39); the
    hermetic tests wire the same ``InMemoryAuditStore`` their assertions read.
    """

    def __init__(self, *, audit: InMemoryAuditStore | None = None) -> None:
        self._sessions: dict[tuple[str, uuid.UUID], ConversationRecord] = {}
        self._messages: dict[tuple[str, uuid.UUID], list[MessageRecord]] = {}
        self._lock = asyncio.Lock()
        self._audit = audit

    async def create(self, tenant_id: str) -> ConversationRecord:
        now = datetime.now(UTC)
        record = ConversationRecord(
            session_id=uuid.uuid4(),
            tenant_id=tenant_id,
            status="active",
            outcome="none",
            version=1,
            started_at=now,
            last_activity_at=now,
            closed_at=None,
        )
        async with self._lock:
            key = (tenant_id, record.session_id)
            self._sessions[key] = record
            self._messages[key] = []
        return record

    async def get(self, tenant_id: str, session_id: uuid.UUID) -> ConversationRecord:
        async with self._lock:
            record = self._sessions.get((tenant_id, session_id))
        if record is None:
            raise NotFoundError(detail="conversation absent or outside tenant")
        return record

    async def append(
        self,
        tenant_id: str,
        session_id: uuid.UUID,
        *,
        role: MessageRole,
        content: str,
        model_name: str | None = None,
        metadata: dict[str, object] | None = None,
        audit_event: AuditEvent | None = None,
    ) -> MessageRecord:
        async with self._lock:
            key = (tenant_id, session_id)
            session = self._sessions.get(key)
            if session is None:
                raise NotFoundError(detail="conversation absent or outside tenant")
            now = datetime.now(UTC)
            record = MessageRecord(
                message_id=uuid.uuid4(),
                tenant_id=tenant_id,
                session_id=session_id,
                sequence_number=session.version,
                role=role,
                content=content,
                model_name=model_name,
                metadata=dict(metadata or {}),
                created_at=now,
            )
            self._messages[key].append(record)
            self._sessions[key] = replace(
                session, version=session.version + 1, last_activity_at=now
            )
        if audit_event is not None and self._audit is not None:
            # Same contract as the PostgreSQL store: the row names the message
            # it vouches for.
            stamped = replace(
                audit_event, details={**audit_event.details, "message_id": str(record.message_id)}
            )
            await self._audit.record(stamped)
        return replace(record, metadata=dict(record.metadata))

    async def transcript(self, tenant_id: str, session_id: uuid.UUID) -> tuple[MessageRecord, ...]:
        await self.get(tenant_id, session_id)
        async with self._lock:
            return tuple(
                replace(message, metadata=dict(message.metadata))
                for message in self._messages[(tenant_id, session_id)]
            )

    async def for_tenant(self, tenant_id: str, *, limit: int) -> tuple[ConversationRecord, ...]:
        async with self._lock:
            conversations = [
                record
                for key, record in self._sessions.items()
                if key[0] == tenant_id and self._messages[key]
            ]
        conversations.sort(key=lambda record: record.last_activity_at, reverse=True)
        return tuple(conversations[:limit])

    async def message_counts(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        async with self._lock:
            counts = {
                session_id: len(self._messages.get((tenant_id, session_id), ()))
                for session_id in session_ids
            }
        return counts

    async def last_messages(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, MessageRecord]:
        async with self._lock:
            return {
                session_id: self._messages[(tenant_id, session_id)][-1]
                for session_id in session_ids
                if self._messages.get((tenant_id, session_id))
            }


class InMemoryBookingStore:
    """An explicit API test fake, never the production source of truth.

    Mirrors the single-transaction contract of the PostgreSQL store: the attempt
    is claimed, the slot reserved, and the booking recorded under one lock, so a
    retry returns the original record and a racing attempt on the same slot is
    refused. ``offered_slots`` feeds the current labels into a slot conflict so
    the caller can re-prompt with real alternatives.
    """

    def __init__(self, offered_slots: Callable[[str, str], Sequence[str]] | None = None) -> None:
        self._records: list[BookingRecord] = []
        self._attempts: dict[tuple[str, str, str], tuple[str, BookingRecord]] = {}
        self._taken: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()
        self._offered_slots = offered_slots or (lambda _tenant, _service: ())

    def taken_slot_ids(self, tenant_id: str) -> frozenset[str]:
        """Slot IDs this tenant has a confirmed booking for.

        Read as a snapshot field rather than under the lock: asyncio never
        interleaves a synchronous read mid-await, so a lock would serialize
        readers that are not competing.
        """
        return frozenset(slot_id for owner, slot_id in self._taken if owner == tenant_id)

    async def confirm(
        self,
        command: BookingCommand,
        *,
        session_id: str,
        attempt: BookingAttempt,
    ) -> BookingOutcome:
        async with self._lock:
            index = (attempt.tenant_id, attempt.scope, attempt.key.value)
            existing = self._attempts.get(index)
            if existing is not None:
                fingerprint, prior = existing
                if fingerprint != attempt.request_hash:
                    raise ConflictError(
                        detail=f"idempotency key reused for a different {attempt.scope}"
                    )
                return BookingOutcome(record=prior, replayed=True)

            if command.slot_start <= datetime.now(UTC):
                raise SlotUnavailableError(
                    offered=tuple(self._offered_slots(attempt.tenant_id, command.service.slug)),
                    detail=f"{command.slot!r} has already passed",
                )
            if any(
                record.tenant_id == attempt.tenant_id and record.slot_id == command.slot_id
                for record in self._records
            ):
                raise SlotUnavailableError(
                    offered=tuple(self._offered_slots(attempt.tenant_id, command.service.slug)),
                    detail=f"{command.slot!r} is already reserved",
                )

            record = self._booking(command, session_id, attempt)
            self._records.append(record)
            self._attempts[index] = (attempt.request_hash, record)
            self._taken.add((attempt.tenant_id, command.slot_id))
            return BookingOutcome(record=record, replayed=False)

    async def replay(self, tenant_id: str, scope: str, key: str) -> BookingRecord | None:
        booked = self._attempts.get((tenant_id, scope, key))
        return booked[1] if booked is not None else None

    def _booking(
        self, command: BookingCommand, session_id: str, attempt: BookingAttempt
    ) -> BookingRecord:
        return BookingRecord(
            booking_id=_reference("BK"),
            tenant_id=attempt.tenant_id,
            session_id=session_id,
            customer_name=command.customer_name,
            contact=command.contact,
            address=command.address,
            service_slug=command.service.slug,
            service_name=command.service.display_name,
            slot=command.slot,
            slot_id=command.slot_id,
            slot_start=command.slot_start,
            slot_end=command.slot_end,
            created_at=datetime.now(UTC),
        )

    async def for_tenant(self, tenant_id: str, *, limit: int = 200) -> tuple[BookingRecord, ...]:
        ordered = tuple(record for record in self._records if record.tenant_id == tenant_id)
        return ordered[:limit]

    async def count_for_tenant(self, tenant_id: str) -> int:
        return sum(1 for record in self._records if record.tenant_id == tenant_id)

    async def for_session(self, tenant_id: str, session_id: str) -> tuple[BookingRecord, ...]:
        return tuple(
            record
            for record in self._records
            if record.tenant_id == tenant_id and record.session_id == session_id
        )

    async def counts_by_session(
        self, tenant_id: str, session_ids: Collection[str]
    ) -> dict[str, int]:
        wanted = set(session_ids)
        return {
            session_id: sum(
                1
                for record in self._records
                if record.tenant_id == tenant_id and record.session_id == session_id
            )
            for session_id in wanted
        }


class InMemoryLeadStore:
    """An explicit API test fake, never the production source of truth."""

    def __init__(self) -> None:
        self._records: list[LeadRecord] = []

    async def record(self, command: LeadCommand, *, session_id: str) -> LeadRecord:
        lead = LeadRecord(
            lead_id=_reference("LD"),
            tenant_id=command.tenant_id,
            session_id=session_id,
            customer_name=command.customer_name,
            contact=command.contact,
            service=command.service,
            service_slug=command.service_slug,
            summary=command.summary,
            address_or_zip=command.address_or_zip,
            urgency=command.urgency,
            created_at=datetime.now(UTC),
        )
        self._records.append(lead)
        return lead

    async def for_tenant(self, tenant_id: str, *, limit: int = 200) -> tuple[LeadRecord, ...]:
        ordered = tuple(record for record in self._records if record.tenant_id == tenant_id)
        return ordered[:limit]

    async def count_for_tenant(self, tenant_id: str) -> int:
        return sum(1 for record in self._records if record.tenant_id == tenant_id)

    async def for_session(self, tenant_id: str, session_id: str) -> tuple[LeadRecord, ...]:
        return tuple(
            record
            for record in self._records
            if record.tenant_id == tenant_id and record.session_id == session_id
        )

    async def counts_by_session(
        self, tenant_id: str, session_ids: Collection[str]
    ) -> dict[str, int]:
        wanted = set(session_ids)
        return {
            session_id: sum(
                1
                for record in self._records
                if record.tenant_id == tenant_id and record.session_id == session_id
            )
            for session_id in wanted
        }


def _handoff_not_found() -> NotFoundError:
    return NotFoundError(detail="handoff absent or outside tenant")


def _transition_error(
    record: HandoffRecord, *, permitted: frozenset[HandoffStatus]
) -> HandoffTransitionError:
    return HandoffTransitionError(
        current=record.status,
        permitted=frozenset(state.value for state in permitted),
        detail=f"handoff {record.handoff_id} is {record.status}",
    )


def _ownership_error(record: HandoffRecord) -> HandoffOwnershipError:
    """The status admits the transition; the caller simply is not the owner.

    A distinct error so the client stops advising "reload the queue" — the
    queue did not move, this principal holds no ownership of the conversation.
    """
    return HandoffOwnershipError(
        current=record.status,
        permitted=frozenset({HandoffStatus.ASSIGNED.value}),
        detail=f"handoff {record.handoff_id} is held by another staff member",
    )


class InMemoryHandoffStore:
    """An explicit API test fake, never the production source of truth.

    The ownership transitions serialize on an :class:`asyncio.Lock`, mirroring
    the atomic conditional update the Postgres store runs. A hermetic test can
    therefore race two accepts and observe one winner — the same guarantee the
    repository specification asserts against the real database.

    ``audit`` is where a transition's audit row lands (R-39); the hermetic
    tests wire the same ``InMemoryAuditStore`` their assertions read.
    """

    def __init__(self, *, audit: InMemoryAuditStore | None = None) -> None:
        self._records: list[HandoffRecord] = []
        self._lock = asyncio.Lock()
        self._audit = audit

    async def _settle_audit(self, event: AuditEvent | None) -> None:
        if event is not None and self._audit is not None:
            await self._audit.record(event)

    async def record(self, command: HandoffCommand, *, session_id: str) -> HandoffRecord:
        handoff = HandoffRecord(
            handoff_id=_reference("HO"),
            tenant_id=command.tenant_id,
            session_id=session_id,
            reason=command.reason,
            summary=command.summary,
            created_at=datetime.now(UTC),
        )
        async with self._lock:
            self._records.append(handoff)
        return handoff

    async def for_tenant(self, tenant_id: str) -> tuple[HandoffRecord, ...]:
        return tuple(record for record in self._records if record.tenant_id == tenant_id)

    async def open_for_tenant(self, tenant_id: str, *, limit: int) -> tuple[HandoffRecord, ...]:
        open_rows = [
            record
            for record in self._records
            if record.tenant_id == tenant_id and HandoffStatus(record.status) in RESOLVABLE_STATUSES
        ]
        return tuple(open_rows[:limit])

    async def get(self, tenant_id: str, handoff_id: str) -> HandoffRecord:
        for record in self._records:
            if record.tenant_id == tenant_id and record.handoff_id == handoff_id:
                return record
        raise _handoff_not_found()

    async def for_session(self, tenant_id: str, session_id: str) -> HandoffRecord | None:
        matches = [
            record
            for record in self._records
            if record.tenant_id == tenant_id and record.session_id == session_id
        ]
        return matches[-1] if matches else None

    async def accept(
        self,
        tenant_id: str,
        handoff_id: str,
        *,
        principal_id: str,
        audit_event: AuditEvent | None = None,
    ) -> HandoffRecord:
        async with self._lock:
            for index, record in enumerate(self._records):
                if record.tenant_id != tenant_id or record.handoff_id != handoff_id:
                    continue
                if HandoffStatus(record.status) not in ACCEPTABLE_STATUSES:
                    raise _transition_error(record, permitted=ACCEPTABLE_STATUSES)
                accepted = replace(
                    record,
                    status="assigned",
                    assigned_principal_id=principal_id,
                    assigned_at=datetime.now(UTC),
                    released_at=None,
                )
                self._records[index] = accepted
                await self._settle_audit(audit_event)
                return accepted
        raise _handoff_not_found()

    async def release(
        self,
        tenant_id: str,
        handoff_id: str,
        *,
        principal_id: str,
        administrative: bool = False,
        audit_event: AuditEvent | None = None,
    ) -> HandoffRecord:
        async with self._lock:
            for index, record in enumerate(self._records):
                if record.tenant_id != tenant_id or record.handoff_id != handoff_id:
                    continue
                if HandoffStatus(record.status) not in RELEASABLE_STATUSES:
                    raise _transition_error(record, permitted=RELEASABLE_STATUSES)
                if record.assigned_principal_id != principal_id and not administrative:
                    raise _ownership_error(record)
                released = replace(
                    record,
                    status="queued",
                    assigned_principal_id=None,
                    assigned_at=None,
                    released_at=datetime.now(UTC),
                )
                self._records[index] = released
                await self._settle_audit(audit_event)
                return released
        raise _handoff_not_found()

    async def resolve(
        self,
        tenant_id: str,
        handoff_id: str,
        *,
        principal_id: str,
        administrative: bool = False,
        audit_event: AuditEvent | None = None,
    ) -> HandoffRecord:
        async with self._lock:
            for index, record in enumerate(self._records):
                if record.tenant_id != tenant_id or record.handoff_id != handoff_id:
                    continue
                if HandoffStatus(record.status) not in RESOLVABLE_STATUSES:
                    raise _transition_error(record, permitted=RESOLVABLE_STATUSES)
                if (
                    record.status == "assigned"
                    and record.assigned_principal_id != principal_id
                    and not administrative
                ):
                    raise _ownership_error(record)
                resolved = replace(
                    record,
                    status="resolved",
                    resolved_at=datetime.now(UTC),
                    resolved_by_principal_id=principal_id,
                )
                self._records[index] = resolved
                await self._settle_audit(audit_event)
                return resolved
        raise _handoff_not_found()


class InMemoryMembershipStore:
    """A concurrency-safe fake with the same upsert/revoke semantics."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], TenantMembership] = {}
        self._lock = asyncio.Lock()

    async def role_for(self, tenant_id: str, subject: str) -> str | None:
        async with self._lock:
            row = self._rows.get((tenant_id, subject))
        return None if row is None else row.role

    async def assign(self, *, tenant_id: str, subject: str, role: str) -> TenantMembership:
        now = datetime.now(UTC)
        async with self._lock:
            existing = self._rows.get((tenant_id, subject))
            row = TenantMembership(
                tenant_id=tenant_id,
                principal_subject=subject,
                role=role,
                created_at=existing.created_at if existing else now,
                updated_at=now,
            )
            self._rows[(tenant_id, subject)] = row
        return row

    async def revoke(self, *, tenant_id: str, subject: str) -> bool:
        async with self._lock:
            return self._rows.pop((tenant_id, subject), None) is not None

    async def for_principal(self, subject: str) -> tuple[TenantMembership, ...]:
        async with self._lock:
            return tuple(row for key, row in sorted(self._rows.items()) if key[1] == subject)

    async def for_tenant(self, tenant_id: str) -> tuple[TenantMembership, ...]:
        async with self._lock:
            return tuple(
                row
                for key, row in sorted(self._rows.items(), key=lambda item: item[0])
                if key[0] == tenant_id
            )


class InMemoryAuditStore:
    """A concurrency-safe fake; production writes append-only rows."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = asyncio.Lock()

    async def record(self, event: AuditEvent) -> AuditEvent:
        async with self._lock:
            self._events.append(event)
        return event

    async def for_tenant(
        self,
        tenant_id: str,
        *,
        limit: int = 200,
        since: datetime | None = None,
        until: datetime | None = None,
        actions: tuple[str, ...] = (),
        principal: str | None = None,
    ) -> tuple[AuditEvent, ...]:
        wanted_actions = set(actions)
        async with self._lock:
            events = [
                event
                for event in self._events
                if event.tenant_id == tenant_id
                and (not wanted_actions or event.action in wanted_actions)
                and (principal is None or event.principal_id == principal)
                and (since is None or event.occurred_at >= since)
                and (until is None or event.occurred_at <= until)
            ]
        events.sort(key=lambda event: event.occurred_at, reverse=True)
        return tuple(events[:limit])


@dataclass(frozen=True, slots=True)
class _Attempt:
    fingerprint: str
    response: dict[str, object] | None
    expires_at: datetime


class InMemoryIdempotencyStore:
    """A concurrency-safe fake with the same claim-then-complete semantics.

    Claims expire like the PostgreSQL store's: a crashed in-flight attempt
    stops blocking its key once past retention, a finished answer serves its
    retries until it is swept, and :meth:`sweep` drops rows past retention —
    so a test can prove the recovery without a database.
    """

    def __init__(self, *, now: Callable[[], datetime] | None = None) -> None:
        self._attempts: dict[tuple[str, str, str], _Attempt] = {}
        self._lock = asyncio.Lock()
        self._now = now or (lambda: datetime.now(UTC))

    async def begin(
        self,
        tenant_id: str,
        *,
        scope: str,
        key: IdempotencyKey,
        fingerprint: str,
    ) -> dict[str, object] | None:
        async with self._lock:
            index = (tenant_id, scope, key.value)
            existing = self._attempts.get(index)
            # Same refusal order as the PostgreSQL store: a fingerprint
            # mismatch is a key-reuse conflict regardless of expiry, a finished
            # answer serves its retries until the sweep drops it, and only an
            # expired in-flight row — a crashed attempt — is reclaimable.
            if existing is not None and existing.fingerprint != fingerprint:
                raise ConflictError(detail=f"idempotency key reused for a different {scope}")
            if (
                existing is not None
                and existing.response is None
                and existing.expires_at <= self._now()
            ):
                del self._attempts[index]
                existing = None
            if existing is None:
                self._attempts[index] = _Attempt(
                    fingerprint=fingerprint,
                    response=None,
                    expires_at=self._now() + IDEMPOTENCY_RETENTION,
                )
                return None
            if existing.response is not None:
                return dict(existing.response)
            raise ConflictError(detail=f"an earlier {scope} attempt is still in flight")

    async def complete(
        self,
        tenant_id: str,
        *,
        scope: str,
        key: IdempotencyKey,
        response: dict[str, object],
    ) -> None:
        async with self._lock:
            index = (tenant_id, scope, key.value)
            existing = self._attempts.get(index)
            if existing is None:
                raise NotFoundError(detail=f"no claimed {scope} attempt for this key")
            self._attempts[index] = _Attempt(
                fingerprint=existing.fingerprint,
                response=dict(response),
                expires_at=existing.expires_at,
            )

    async def sweep(self) -> int:
        async with self._lock:
            expired = [
                index
                for index, attempt in self._attempts.items()
                if attempt.expires_at <= self._now()
            ]
            for index in expired:
                del self._attempts[index]
            return len(expired)


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """One purpose granted (or withdrawn) by one session."""

    record_id: uuid.UUID
    tenant_id: str
    session_id: str
    purpose: ConsentPurpose
    status: ConsentStatus
    statement: str
    granted_at: datetime
    withdrawn_at: datetime | None


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """One inference-plane row: the governed envelope `OBS-004` will populate.

    ``content`` is the opaque object that holds the prompt, retrieved evidence,
    model output, and verdicts. PRIV-002 never parses it — the schema owns the
    envelope, `OBS-004` owns the shape — but export, erasure, and retention
    all treat the whole record as one content-bearing unit.

    The column fields are the content-free projection the attribution query
    surface filters on: the outcome, the component-manifest hash, the
    diagnosis causes, the turn index, and the trace schema version. They are
    derived from the content at write time and never hold content themselves.

    ``recorded_at`` is when the turn happened, the timestamp retention purges
    on (independently of, and shorter than, the transcript's).
    """

    turn_id: uuid.UUID
    tenant_id: str
    session_id: uuid.UUID
    trace_id: str | None
    content: dict[str, object]
    recorded_at: datetime
    outcome: str = "unknown"
    component_manifest_hash: str = ""
    diagnosis_causes: tuple[str, ...] = ()
    diagnosis_statuses: tuple[str, ...] = ()
    turn_index: int = 0
    trace_schema_version: str = "1"
    source_generation_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, slots=True)
class TurnRecordProjection:
    """A derived dataset pinned to the turn record it was built from.

    `FEAT-008` promotes reviewed turns into evaluation datasets here: the
    anonymized case payload (`evals.scorer.EvalCase` shape) is the projection's
    ``payload``. Erasure of the turn record removes every projection of it (the
    schema cascades), which is what makes "any projection derived from the
    record" eraseable without a second registry.
    """

    projection_id: uuid.UUID
    tenant_id: str
    turn_record_id: uuid.UUID
    kind: str
    created_at: datetime
    payload: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TraceAccessGrant:
    """One operator's dedicated turn-record read grant for one tenant.

    Deliberately separate from ``tenant_memberships``: this is the `PRIV-002`
    role, and holding a tenant-admin membership confers no trace access. It is
    still tenant-qualified and auditable — ``granted_by`` names the platform
    administrator who issued it.
    """

    tenant_id: str
    principal_subject: str
    granted_at: datetime
    granted_by: str


@dataclass(frozen=True, slots=True)
class TurnFeedback:
    """One visitor's rating of one turn record, idempotently upserted.

    ``reason`` is the visitor's own words — content-bearing, so it lives under
    the same governance as the turn it refers to (it cascades off the turn
    record on erasure) and never reaches a log, metric, or the content-free
    queue list.
    """

    feedback_id: uuid.UUID
    tenant_id: str
    turn_id: uuid.UUID
    rating: str
    reason: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewCase:
    """One queue entry for one turn record.

    The priority inputs are frozen at enqueue time — the deterministic score,
    the recurrence count, whether the turn committed a business action, and
    whether its manifest hash is new to the queue — so re-sorting later is a
    matter of reading stored integers, never re-deriving from content.

    ``case_id`` names the promoted evaluation case once the reviewer approves
    promotion; ``closing_eval_*`` is the acceptance-5 linkage, written exactly
    once by the evaluation gate when the first passing run covers the case.
    """

    review_id: uuid.UUID
    tenant_id: str
    turn_id: uuid.UUID
    source: str
    status: str
    priority: int
    recurrence: int
    manifest_hash: str
    committed_actions: bool
    novel_manifest: bool
    case_id: str | None
    reviewer_subject: str | None
    reviewed_at: datetime | None
    verdict: str | None
    verdict_note: str | None
    corrected_answer: str | None
    proposed_fix: str | None
    closing_eval_run_id: str | None
    closing_eval_case_id: str | None
    closing_eval_passed_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ReviewDiagnosis:
    """One reviewer-authored diagnosis record, kept apart from the detector's.

    ``relationship`` and ``automatic_index`` store the disagreement explicitly:
    the detector's records live in the turn's opaque content and are never
    mutated, so automatic and reviewer diagnoses can always be shown side by
    side (acceptance 4).
    """

    diagnosis_id: uuid.UUID
    tenant_id: str
    review_id: uuid.UUID
    relationship: str
    automatic_index: int | None
    cause: str
    stage: str
    role: str
    status: str
    confidence: str
    evidence: tuple[str, ...]
    note: str | None
    created_at: datetime


class ConsentStore(Protocol):
    """Consent grants, keyed by tenant and session.

    A session grants purposes under the statement the server derived from the
    tenant's policy; the store is what the idempotent services read through
    :class:`~tenantchat.core.ports.ConsentSource` when a contact-bearing action
    is about to commit.
    """

    async def record(
        self,
        tenant_id: str,
        session_id: str,
        *,
        purposes: Collection[ConsentPurpose],
        statement: str,
    ) -> tuple[ConsentRecord, ...]:
        """Record a grant, replacing any earlier grant of the same purpose.

        Idempotent per session and purpose: re-recording the same purposes
        returns the existing grant rather than stacking duplicates.
        """

    async def consent_grant(self, tenant_id: str, session_id: str) -> ConsentGrant:
        """The grant a session currently holds, empty when none was recorded.

        Only ``granted`` purposes count; a withdrawn purpose is absent from
        the returned grant.
        """

    async def for_session(self, tenant_id: str, session_id: str) -> tuple[ConsentRecord, ...]:
        """The full consent history for one session, for export and audit."""


class InMemoryConsentStore:
    """An explicit API test fake, never the production source of truth."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str, ConsentPurpose], ConsentRecord] = {}
        self._lock = asyncio.Lock()

    async def record(
        self,
        tenant_id: str,
        session_id: str,
        *,
        purposes: Collection[ConsentPurpose],
        statement: str,
    ) -> tuple[ConsentRecord, ...]:
        now = datetime.now(UTC)
        async with self._lock:
            recorded: list[ConsentRecord] = []
            for purpose in purposes:
                key = (tenant_id, session_id, purpose)
                existing = self._records.get(key)
                if existing is not None and existing.status is ConsentStatus.GRANTED:
                    recorded.append(existing)
                    continue
                record = ConsentRecord(
                    record_id=uuid.uuid4(),
                    tenant_id=tenant_id,
                    session_id=session_id,
                    purpose=purpose,
                    status=ConsentStatus.GRANTED,
                    statement=statement,
                    granted_at=now,
                    withdrawn_at=None,
                )
                self._records[key] = record
                recorded.append(record)
        return tuple(recorded)

    async def consent_grant(self, tenant_id: str, session_id: str) -> ConsentGrant:
        async with self._lock:
            granted = [
                record
                for (tenant, session, _purpose), record in self._records.items()
                if tenant == tenant_id
                and session == session_id
                and record.status is ConsentStatus.GRANTED
            ]
        if not granted:
            return ConsentGrant(
                tenant_id=tenant_id,
                session_id=session_id,
                purposes=frozenset(),
                statement="",
                granted_at=datetime.min.replace(tzinfo=UTC),
            )
        return ConsentGrant(
            tenant_id=tenant_id,
            session_id=session_id,
            purposes=frozenset(record.purpose for record in granted),
            statement=granted[0].statement,
            granted_at=max(record.granted_at for record in granted),
        )

    async def for_session(self, tenant_id: str, session_id: str) -> tuple[ConsentRecord, ...]:
        async with self._lock:
            return tuple(
                record
                for (tenant, session, _purpose), record in self._records.items()
                if tenant == tenant_id and session == session_id
            )


class TurnRecordStore(Protocol):
    """The inference-plane envelope, written and read under `PRIV-002` governance.

    ``record`` is the seam `OBS-004` populates at trace-finalization time; the
    caller supplies the opaque content object and the store stamps the id and
    timestamp. Reads are the trace viewer's surface and are expected to be
    audited by the caller with a `TurnRecordReadReason`.
    """

    async def record(
        self,
        tenant_id: str,
        session_id: uuid.UUID,
        *,
        content: dict[str, object],
        trace_id: str | None = None,
        recorded_at: datetime | None = None,
        outcome: str = "unknown",
        component_manifest_hash: str = "",
        diagnosis_causes: tuple[str, ...] = (),
        diagnosis_statuses: tuple[str, ...] = (),
        turn_index: int = 0,
        trace_schema_version: str = "1",
        source_generation_ids: tuple[uuid.UUID, ...] = (),
    ) -> TurnRecord:
        """Append one turn record for a session.

        The metadata columns are the content-free projection the attribution
        query surface filters on; the caller derives them from the content.

        Raises:
            NotFoundError: the session is absent or belongs to another tenant.
        """

    async def get(self, tenant_id: str, turn_id: uuid.UUID) -> TurnRecord:
        """One turn record, tenant-qualified.

        Raises:
            NotFoundError: no such record, or it belongs to another tenant.
        """

    async def for_session(
        self, tenant_id: str, session_id: uuid.UUID, *, limit: int
    ) -> tuple[TurnRecord, ...]:
        """The session's turn records, oldest first, bounded to *limit*."""

    async def for_turn_ids(
        self, tenant_id: str, turn_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, TurnRecord]:
        """Batch the queue's turn fetches into one `id = ANY(...)` query.

        Missing ids are absent from the result, never an error: a queue row
        whose turn was purged must not take the whole page down.
        """

    async def for_trace_id(self, tenant_id: str, trace_id: str) -> TurnRecord:
        """The one record the correlation trace id names, tenant-qualified.

        A correlation id is minted per request, so at most one record carries
        it; the first match is returned when history ever violates that.

        Raises:
            NotFoundError: no such record, or it belongs to another tenant.
        """

    async def search(
        self,
        tenant_id: str,
        *,
        manifest_hash: str | None = None,
        causes: tuple[str, ...] = (),
        statuses: tuple[str, ...] = (),
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        generation_ids: tuple[uuid.UUID, ...] = (),
    ) -> tuple[TurnRecord, ...]:
        """The tenant's records matching the content-free filters, newest first.

        This is the `OBS-004` attribution query surface: filter by the
        component-manifest hash (what build answered), by diagnosis causes or
        statuses (what failed, and how certain the record is), by outcome, by
        recorded time, or by the index generations the retrieval cited —
        bounded to *limit*.
        """

    async def projections_for_turn(
        self, tenant_id: str, turn_id: uuid.UUID
    ) -> tuple[TurnRecordProjection, ...]:
        """The derived datasets pinned to one turn record (empty when none).

        The schema cascades these off the turn record on erasure; this read
        exists so the trace viewer can see what was derived from what it reads.
        """

    async def create_projection(
        self,
        tenant_id: str,
        turn_id: uuid.UUID,
        *,
        kind: str,
        payload: Mapping[str, object],
    ) -> TurnRecordProjection:
        """Pin a derived dataset to a turn record.

        `FEAT-008` promotion is the only writer today: the anonymized
        evaluation case becomes the projection's payload, erased with the
        record it was derived from.

        Raises:
            NotFoundError: the turn record is absent or belongs to another tenant.
        """


class TraceAccessStore(Protocol):
    """The `PRIV-002` dedicated role for turn-record reads.

    Granting is a platform-administrator action, audited like membership
    assignment; checking is what the trace read route gates on. Separate from
    transcript memberships on purpose: a transcript viewer holds no trace
    access unless granted here.
    """

    async def grant(self, tenant_id: str, subject: str, *, granted_by: str) -> TraceAccessGrant:
        """Grant trace-read access; re-granting is an idempotent upsert."""

    async def revoke(self, tenant_id: str, subject: str) -> bool:
        """Revoke trace-read access; returns whether a grant was removed."""

    async def has_access(self, tenant_id: str, subject: str) -> bool:
        """Whether the operator may read this tenant's turn records."""

    async def for_tenant(self, tenant_id: str) -> tuple[TraceAccessGrant, ...]:
        """The tenant's current grants, for the operator console."""


class TurnFeedbackStore(Protocol):
    """Visitor ratings of turn records, idempotent per turn.

    The record is written only after the turn is proven to belong to the
    credential's tenant and session, which is what keeps feedback from naming
    a conversation the visitor never took part in (acceptance 1).
    """

    async def record(
        self,
        tenant_id: str,
        turn_id: uuid.UUID,
        *,
        rating: str,
        reason: str | None,
    ) -> TurnFeedback:
        """Record a rating, replacing any earlier rating of the same turn.

        Raises:
            NotFoundError: the turn record is absent or belongs to another tenant.
        """

    async def for_turn(self, tenant_id: str, turn_id: uuid.UUID) -> TurnFeedback | None:
        """The turn's current rating, or ``None`` when never rated."""


class ReviewQueueStore(Protocol):
    """The `FEAT-008` review queue: one case per turn, with its diagnosis
    overlay and its fix-closure reference.

    Enqueueing is idempotent per turn: a turn that was already flagged by the
    detector gains no second case when a visitor also thumbs it down — the
    first case is returned, and its ``source`` records how it first entered.
    Every mutation enforces the closed status machine; the store is where the
    transition lives because it must be atomic with the write.
    """

    async def enqueue(
        self,
        tenant_id: str,
        turn_id: uuid.UUID,
        *,
        source: str,
        priority: int,
        recurrence: int,
        manifest_hash: str,
        committed_actions: bool,
        novel_manifest: bool,
    ) -> ReviewCase:
        """Open a case for a turn, or return the one already open.

        Raises:
            NotFoundError: the turn record is absent or belongs to another tenant.
        """

    async def get(self, tenant_id: str, review_id: uuid.UUID) -> ReviewCase:
        """One case, tenant-qualified.

        Raises:
            NotFoundError: no such case, or it belongs to another tenant.
        """

    async def for_turn(self, tenant_id: str, turn_id: uuid.UUID) -> ReviewCase | None:
        """The case for one turn, or ``None`` when never enqueued."""

    async def search(
        self,
        tenant_id: str,
        *,
        statuses: tuple[str, ...] = (),
        limit: int = 50,
    ) -> tuple[ReviewCase, ...]:
        """The tenant's cases, highest priority first, bounded to *limit*.

        Ties break by enqueue time (oldest first), so the ordering is
        deterministic for a given store state.
        """

    async def count_for_manifest(self, tenant_id: str, manifest_hash: str) -> int:
        """How many cases the tenant already has for one manifest hash.

        The recurrence input of the priority formula: zero means the hash is
        novel to this tenant's queue. Deliberately unbounded — recurrence is
        capped by the formula, not by this count.
        """

    async def take(
        self,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        reviewer: str,
        audit_event: AuditEvent | None = None,
    ) -> ReviewCase:
        """Mark an open case as in review by one operator.

        ``audit_event``, when given, is persisted in the same transaction as
        the transition (R-39).

        Raises:
            NotFoundError: no such case, or it belongs to another tenant.
            ReviewTransitionError: the case is not ``open``.
        """

    async def submit(
        self,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        reviewer: str,
        verdict: str,
        note: str | None,
        corrected_answer: str | None,
        proposed_fix: str | None,
        status: str,
        diagnoses: tuple[ReviewDiagnosis, ...],
        audit_event: AuditEvent | None = None,
    ) -> ReviewCase:
        """Record the reviewer's decision and move the case to its destination.

        The diagnosis rows replace any earlier overlay for this review — a
        review can be corrected only by resubmitting, and the audit trail
        preserves every submission. The destination is ``awaiting_fix`` or
        ``rejected``; ``resolved`` is reachable only through
        :meth:`record_eval_pass`. ``audit_event`` commits with the decision
        (R-39).

            Raises:
                NotFoundError: no such case, or it belongs to another tenant.
                ReviewTransitionError: the case is not ``open``, ``in_review``,
                    or ``awaiting_fix`` (resubmission corrects a pending review;
                    a closed case is history).
        """

    async def record_eval_pass(
        self,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        run_id: str,
        case_id: str,
        passed_at: datetime,
    ) -> ReviewCase:
        """Close an ``awaiting_fix`` case with its first passing run.

        Writing the closing reference is a no-op for an already-closed case,
        so re-applying an evaluation report cannot overwrite the first run
        that passed (acceptance 5).

        Raises:
            NotFoundError: no such case, or it belongs to another tenant.
        """

    async def set_case_id(
        self, tenant_id: str, review_id: uuid.UUID, *, case_id: str
    ) -> ReviewCase:
        """Attach the promoted evaluation case id to a review.

        Raises:
            NotFoundError: no such case, or it belongs to another tenant.
        """

    async def diagnoses(self, tenant_id: str, review_id: uuid.UUID) -> tuple[ReviewDiagnosis, ...]:
        """The reviewer's diagnosis rows for one case, oldest first."""

    async def for_case_ids(
        self, tenant_id: str, case_ids: Collection[str]
    ) -> tuple[ReviewCase, ...]:
        """The tenant's open cases whose promoted case id is in *case_ids*.

        This is the lookup the evaluation gate (`RAG-008`) runs a report
        against: find the reviews that report covered, then close the ones
        whose status is still ``awaiting_fix``.
        """


class InMemoryTurnRecordStore:
    """A concurrency-safe fake; production composition never constructs it."""

    def __init__(self) -> None:
        self._records: dict[uuid.UUID, TurnRecord] = {}
        self._projections: dict[uuid.UUID, TurnRecordProjection] = {}
        self._lock = asyncio.Lock()

    async def record(
        self,
        tenant_id: str,
        session_id: uuid.UUID,
        *,
        content: dict[str, object],
        trace_id: str | None = None,
        recorded_at: datetime | None = None,
        outcome: str = "unknown",
        component_manifest_hash: str = "",
        diagnosis_causes: tuple[str, ...] = (),
        diagnosis_statuses: tuple[str, ...] = (),
        turn_index: int = 0,
        trace_schema_version: str = "1",
        source_generation_ids: tuple[uuid.UUID, ...] = (),
    ) -> TurnRecord:
        record = TurnRecord(
            turn_id=uuid.uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            trace_id=trace_id,
            content=dict(content),
            recorded_at=recorded_at or datetime.now(UTC),
            outcome=outcome,
            component_manifest_hash=component_manifest_hash,
            diagnosis_causes=tuple(diagnosis_causes),
            diagnosis_statuses=tuple(diagnosis_statuses),
            turn_index=turn_index,
            trace_schema_version=trace_schema_version,
            source_generation_ids=tuple(source_generation_ids),
        )
        async with self._lock:
            self._records[record.turn_id] = record
        return record

    async def get(self, tenant_id: str, turn_id: uuid.UUID) -> TurnRecord:
        async with self._lock:
            record = self._records.get(turn_id)
        if record is None or record.tenant_id != tenant_id:
            raise NotFoundError(detail="turn record absent or outside tenant")
        return replace(record, content=dict(record.content))

    async def for_session(
        self, tenant_id: str, session_id: uuid.UUID, *, limit: int
    ) -> tuple[TurnRecord, ...]:
        async with self._lock:
            records = [
                replace(record, content=dict(record.content))
                for record in self._records.values()
                if record.tenant_id == tenant_id and record.session_id == session_id
            ]
        records.sort(key=lambda record: record.recorded_at)
        return tuple(records[:limit])

    async def for_turn_ids(
        self, tenant_id: str, turn_ids: Collection[uuid.UUID]
    ) -> dict[uuid.UUID, TurnRecord]:
        async with self._lock:
            return {
                turn_id: replace(record, content=dict(record.content))
                for turn_id in turn_ids
                if (record := self._records.get(turn_id)) is not None
                and record.tenant_id == tenant_id
            }

    async def for_trace_id(self, tenant_id: str, trace_id: str) -> TurnRecord:
        async with self._lock:
            record = next(
                (
                    record
                    for record in self._records.values()
                    if record.tenant_id == tenant_id and record.trace_id == trace_id
                ),
                None,
            )
        if record is None:
            raise NotFoundError(detail="turn record absent or outside tenant")
        return replace(record, content=dict(record.content))

    async def search(
        self,
        tenant_id: str,
        *,
        manifest_hash: str | None = None,
        causes: tuple[str, ...] = (),
        statuses: tuple[str, ...] = (),
        outcome: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        limit: int = 50,
        generation_ids: tuple[uuid.UUID, ...] = (),
    ) -> tuple[TurnRecord, ...]:
        wanted_causes = set(causes)
        wanted_statuses = set(statuses)
        wanted_generations = set(generation_ids)
        async with self._lock:
            records = [
                replace(record, content=dict(record.content))
                for record in self._records.values()
                if record.tenant_id == tenant_id
                and (manifest_hash is None or record.component_manifest_hash == manifest_hash)
                and (not wanted_causes or wanted_causes.issubset(set(record.diagnosis_causes)))
                and (
                    not wanted_statuses or wanted_statuses.issubset(set(record.diagnosis_statuses))
                )
                and (outcome is None or record.outcome == outcome)
                and (since is None or record.recorded_at >= since)
                and (until is None or record.recorded_at <= until)
                and (
                    not wanted_generations
                    or bool(set(record.source_generation_ids) & wanted_generations)
                )
            ]
        records.sort(key=lambda record: record.recorded_at, reverse=True)
        return tuple(records[:limit])

    async def projections_for_turn(
        self, tenant_id: str, turn_id: uuid.UUID
    ) -> tuple[TurnRecordProjection, ...]:
        async with self._lock:
            projections = [
                replace(projection, payload=dict(projection.payload))
                for projection in self._projections.values()
                if projection.tenant_id == tenant_id and projection.turn_record_id == turn_id
            ]
        projections.sort(key=lambda projection: (projection.created_at, projection.projection_id))
        return tuple(projections)

    async def create_projection(
        self,
        tenant_id: str,
        turn_id: uuid.UUID,
        *,
        kind: str,
        payload: Mapping[str, object],
    ) -> TurnRecordProjection:
        """Pin a derived dataset (an `FEAT-008` evaluation case) to a turn."""
        async with self._lock:
            record = self._records.get(turn_id)
            if record is None or record.tenant_id != tenant_id:
                raise NotFoundError(detail="turn record absent or outside tenant")
            projection = TurnRecordProjection(
                projection_id=uuid.uuid4(),
                tenant_id=tenant_id,
                turn_record_id=turn_id,
                kind=kind,
                created_at=datetime.now(UTC),
                payload=dict(payload),
            )
            self._projections[projection.projection_id] = projection
        return projection


class InMemoryTurnFeedbackStore:
    """A concurrency-safe fake; production composition never constructs it."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, uuid.UUID], TurnFeedback] = {}
        self._lock = asyncio.Lock()

    async def record(
        self,
        tenant_id: str,
        turn_id: uuid.UUID,
        *,
        rating: str,
        reason: str | None,
    ) -> TurnFeedback:
        now = datetime.now(UTC)
        async with self._lock:
            existing = self._records.get((tenant_id, turn_id))
            record = TurnFeedback(
                feedback_id=existing.feedback_id if existing else uuid.uuid4(),
                tenant_id=tenant_id,
                turn_id=turn_id,
                rating=rating,
                reason=reason,
                created_at=existing.created_at if existing else now,
            )
            self._records[(tenant_id, turn_id)] = record
        return record

    async def for_turn(self, tenant_id: str, turn_id: uuid.UUID) -> TurnFeedback | None:
        async with self._lock:
            return self._records.get((tenant_id, turn_id))


class InMemoryReviewQueueStore:
    """A concurrency-safe fake with the same closed status machine.

    ``audit`` is where a decision's audit row lands (R-39); the hermetic tests
    wire the same ``InMemoryAuditStore`` their assertions read.
    """

    def __init__(self, *, audit: InMemoryAuditStore | None = None) -> None:
        self._cases: dict[uuid.UUID, ReviewCase] = {}
        self._diagnoses: dict[uuid.UUID, list[ReviewDiagnosis]] = {}
        self._lock = asyncio.Lock()
        self._audit = audit

    async def _settle_audit(self, event: AuditEvent | None) -> None:
        if event is not None and self._audit is not None:
            await self._audit.record(event)

    @staticmethod
    def _touch(case: ReviewCase) -> ReviewCase:
        return replace(case, updated_at=datetime.now(UTC))

    async def enqueue(
        self,
        tenant_id: str,
        turn_id: uuid.UUID,
        *,
        source: str,
        priority: int,
        recurrence: int,
        manifest_hash: str,
        committed_actions: bool,
        novel_manifest: bool,
    ) -> ReviewCase:
        now = datetime.now(UTC)
        async with self._lock:
            existing = next(
                (case for case in self._cases.values() if case.turn_id == turn_id),
                None,
            )
            if existing is not None:
                if existing.tenant_id != tenant_id:
                    raise NotFoundError(detail="turn record absent or outside tenant")
                return existing
            case = ReviewCase(
                review_id=uuid.uuid4(),
                tenant_id=tenant_id,
                turn_id=turn_id,
                source=source,
                status="open",
                priority=priority,
                recurrence=recurrence,
                manifest_hash=manifest_hash,
                committed_actions=committed_actions,
                novel_manifest=novel_manifest,
                case_id=None,
                reviewer_subject=None,
                reviewed_at=None,
                verdict=None,
                verdict_note=None,
                corrected_answer=None,
                proposed_fix=None,
                closing_eval_run_id=None,
                closing_eval_case_id=None,
                closing_eval_passed_at=None,
                created_at=now,
                updated_at=now,
            )
            self._cases[case.review_id] = case
            return case

    async def get(self, tenant_id: str, review_id: uuid.UUID) -> ReviewCase:
        async with self._lock:
            case = self._cases.get(review_id)
        if case is None or case.tenant_id != tenant_id:
            raise NotFoundError(detail="review case absent or outside tenant")
        return case

    async def for_turn(self, tenant_id: str, turn_id: uuid.UUID) -> ReviewCase | None:
        async with self._lock:
            return next(
                (case for case in self._cases.values() if case.turn_id == turn_id),
                None,
            )

    async def search(
        self,
        tenant_id: str,
        *,
        statuses: tuple[str, ...] = (),
        limit: int = 50,
    ) -> tuple[ReviewCase, ...]:
        wanted = set(statuses)
        async with self._lock:
            cases = [
                case
                for case in self._cases.values()
                if case.tenant_id == tenant_id and (not wanted or case.status in wanted)
            ]
        cases.sort(key=lambda case: (-case.priority, case.created_at))
        return tuple(cases[:limit])

    async def count_for_manifest(self, tenant_id: str, manifest_hash: str) -> int:
        async with self._lock:
            return sum(
                1
                for case in self._cases.values()
                if case.tenant_id == tenant_id and case.manifest_hash == manifest_hash
            )

    async def take(
        self,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        reviewer: str,
        audit_event: AuditEvent | None = None,
    ) -> ReviewCase:
        async with self._lock:
            case = self._cases.get(review_id)
            if case is None or case.tenant_id != tenant_id:
                raise NotFoundError(detail="review case absent or outside tenant")
            if case.status != "open":
                raise ReviewTransitionError(current=case.status, permitted=frozenset({"open"}))
            updated = replace(
                case, status="in_review", reviewer_subject=reviewer, updated_at=datetime.now(UTC)
            )
            self._cases[review_id] = updated
        await self._settle_audit(audit_event)
        return updated

    async def submit(
        self,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        reviewer: str,
        verdict: str,
        note: str | None,
        corrected_answer: str | None,
        proposed_fix: str | None,
        status: str,
        diagnoses: tuple[ReviewDiagnosis, ...],
        audit_event: AuditEvent | None = None,
    ) -> ReviewCase:
        now = datetime.now(UTC)
        async with self._lock:
            case = self._cases.get(review_id)
            if case is None or case.tenant_id != tenant_id:
                raise NotFoundError(detail="review case absent or outside tenant")
            if case.status not in ("open", "in_review", "awaiting_fix"):
                raise ReviewTransitionError(
                    current=case.status,
                    permitted=frozenset({"open", "in_review", "awaiting_fix"}),
                )
            updated = replace(
                case,
                status=status,
                reviewer_subject=reviewer,
                reviewed_at=now,
                verdict=verdict,
                verdict_note=note,
                corrected_answer=corrected_answer,
                proposed_fix=proposed_fix,
                updated_at=now,
            )
            self._cases[review_id] = updated
            self._diagnoses[review_id] = list(diagnoses)
        await self._settle_audit(audit_event)
        return updated

    async def record_eval_pass(
        self,
        tenant_id: str,
        review_id: uuid.UUID,
        *,
        run_id: str,
        case_id: str,
        passed_at: datetime,
    ) -> ReviewCase:
        async with self._lock:
            case = self._cases.get(review_id)
            if case is None or case.tenant_id != tenant_id:
                raise NotFoundError(detail="review case absent or outside tenant")
            if case.closing_eval_run_id is not None:
                return case
            updated = replace(
                case,
                status="resolved",
                closing_eval_run_id=run_id,
                closing_eval_case_id=case_id,
                closing_eval_passed_at=passed_at,
                updated_at=passed_at,
            )
            self._cases[review_id] = updated
            return updated

    async def set_case_id(
        self, tenant_id: str, review_id: uuid.UUID, *, case_id: str
    ) -> ReviewCase:
        async with self._lock:
            case = self._cases.get(review_id)
            if case is None or case.tenant_id != tenant_id:
                raise NotFoundError(detail="review case absent or outside tenant")
            updated = replace(case, case_id=case_id)
            self._cases[review_id] = updated
            return updated

    async def diagnoses(self, tenant_id: str, review_id: uuid.UUID) -> tuple[ReviewDiagnosis, ...]:
        async with self._lock:
            rows = self._diagnoses.get(review_id, ())
            if rows and any(row.tenant_id != tenant_id for row in rows):
                raise NotFoundError(detail="review case absent or outside tenant")
            return tuple(rows)

    async def for_case_ids(
        self, tenant_id: str, case_ids: Collection[str]
    ) -> tuple[ReviewCase, ...]:
        wanted = set(case_ids)
        async with self._lock:
            cases = [
                case
                for case in self._cases.values()
                if case.tenant_id == tenant_id and case.case_id in wanted
            ]
        return tuple(cases)


class InMemoryTraceAccessStore:
    """A concurrency-safe fake; production composition never constructs it."""

    def __init__(self) -> None:
        self._grants: dict[tuple[str, str], TraceAccessGrant] = {}
        self._lock = asyncio.Lock()

    async def grant(self, tenant_id: str, subject: str, *, granted_by: str) -> TraceAccessGrant:
        now = datetime.now(UTC)
        async with self._lock:
            existing = self._grants.get((tenant_id, subject))
            grant = TraceAccessGrant(
                tenant_id=tenant_id,
                principal_subject=subject,
                granted_at=existing.granted_at if existing else now,
                granted_by=granted_by,
            )
            self._grants[(tenant_id, subject)] = grant
        return grant

    async def revoke(self, tenant_id: str, subject: str) -> bool:
        async with self._lock:
            return self._grants.pop((tenant_id, subject), None) is not None

    async def has_access(self, tenant_id: str, subject: str) -> bool:
        async with self._lock:
            return (tenant_id, subject) in self._grants

    async def for_tenant(self, tenant_id: str) -> tuple[TraceAccessGrant, ...]:
        async with self._lock:
            return tuple(
                grant
                for (tenant, _subject), grant in sorted(self._grants.items())
                if tenant == tenant_id
            )


@dataclass(frozen=True, slots=True)
class PrivacyRequestRecord:
    """A deletion request from an operator, fulfilled by the erasure worker.

    ``contact_value`` is the canonical form the worker searches on, and it is
    anonymized when the request completes so the queue itself does not hoard
    contact details indefinitely.
    """

    request_id: uuid.UUID
    tenant_id: str
    status: str
    contact_kind: str
    contact_value: str
    requested_by: str
    requested_at: datetime
    processed_at: datetime | None


@dataclass(frozen=True, slots=True)
class SubjectRecords:
    """Everything the platform holds about one subject, assembled for export.

    Built by :class:`PrivacyStore` from the sessions a contact maps to; the
    export route renders it without touching the stores again. ``turn_records``
    and ``projections`` are the inference plane's contribution: a turn record
    holds the same conversation's content, and a projection is derived from it,
    so both belong in a data-subject export.
    """

    sessions: tuple[ConversationRecord, ...]
    messages: tuple[MessageRecord, ...]
    leads: tuple[LeadRecord, ...]
    bookings: tuple[BookingRecord, ...]
    handoffs: tuple[HandoffRecord, ...]
    consent: tuple[ConsentRecord, ...]
    turn_records: tuple[TurnRecord, ...]
    projections: tuple[TurnRecordProjection, ...]


@dataclass(frozen=True, slots=True)
class ErasureReport:
    """Rows removed (or anonymized) by one deletion-request run.

    The audit event for a completed request carries these counts, which is
    what makes a deletion auditable without a deletion log table.
    """

    sessions_deleted: int
    messages_deleted: int
    leads_anonymized: int
    bookings_anonymized: int
    handoffs_anonymized: int
    consent_records_deleted: int
    checkpoints_deleted: int
    turn_records_deleted: int


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """What the retention worker removed in one pass.

    Counts are per data class so the audit event is a number, not a list of
    references. Idempotency keys are absent: they hold only hashes and are not
    linked to sessions, so nothing about them can be counted for a subject.
    ``turn_records_deleted`` is the inference plane's independent, shorter
    retention (``PRIV-002``); projections are erased by cascading off their
    turn records and counted there.
    """

    sessions_deleted: int
    messages_deleted: int
    tool_executions_deleted: int
    consent_records_deleted: int
    turn_records_deleted: int


class PrivacyStore(Protocol):
    """Subject discovery, export assembly, erasure, and retention purge.

    Erasure and purge are the only operations here that run under the
    erasure role's credentials (see ``provision_privacy_role.sql``): the
    application role is granted no ``DELETE`` on sessions or transcripts, so
    these effects cannot be caused through the API.
    """

    async def sessions_for_contact(self, tenant_id: str, contact: Contact) -> tuple[uuid.UUID, ...]:
        """Every session that holds a record for this contact, newest first.

        Matches on the canonical contact value across leads, bookings, and
        transcript content.
        """

    async def subject_records(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> SubjectRecords:
        """Everything stored for the named sessions, for export."""

    async def erase_subject(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> ErasureReport:
        """Remove all rows for the sessions, irreversibly.

        Deletes rows rather than anonymizing them where the role permits;
        leads and bookings are anonymized instead, because the schema makes
        them the tenant's business records and keeps foreign keys that a row
        delete would violate.
        """

    async def purge_expired(
        self, tenant_id: str, policy: RetentionPolicy, *, now: datetime
    ) -> PurgeReport:
        """Remove the tenant's expired records, and the sessions that then hold
        nothing but the shell.

        ``now`` is passed in so a test can pin the clock; the worker calls
        this with the current time, once per tenant, so each purge is an
        auditable per-tenant event.
        """

    async def create_privacy_request(
        self, tenant_id: str, *, contact: Contact, requested_by: str
    ) -> PrivacyRequestRecord:
        """Queue a deletion request for the erasure worker."""

    async def pending_privacy_requests(self) -> tuple[PrivacyRequestRecord, ...]:
        """Requests the erasure worker has not finished, oldest first."""

    async def complete_privacy_request(
        self, request_id: uuid.UUID, *, processed_at: datetime
    ) -> None:
        """Mark a request fulfilled and anonymize its contact value."""

    async def fail_privacy_request(self, request_id: uuid.UUID) -> None:
        """Mark a request failed so an operator can see it did not run."""

    async def requests_for_tenant(self, tenant_id: str) -> tuple[PrivacyRequestRecord, ...]:
        """The tenant's deletion requests, newest first, for the operator queue."""


class InMemoryPrivacyStore:
    """An explicit API test fake over the other in-memory stores.

    Reaches into its sibling fakes because subject discovery spans them; the
    PostgreSQL implementation answers the same questions in SQL. Construction
    mirrors what ``create_app`` wires for the same stores.
    """

    def __init__(
        self,
        conversations: InMemoryConversationStore,
        bookings: InMemoryBookingStore,
        leads: InMemoryLeadStore,
        handoffs: InMemoryHandoffStore,
        consent: InMemoryConsentStore,
        turn_records: InMemoryTurnRecordStore | None = None,
    ) -> None:
        self._conversations = conversations
        self._bookings = bookings
        self._leads = leads
        self._handoffs = handoffs
        self._consent = consent
        self._turn_records = turn_records or InMemoryTurnRecordStore()
        self._requests: dict[uuid.UUID, PrivacyRequestRecord] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _matches(contact: Contact, text: str) -> bool:
        # The canonical phone form (+15552221919) never appears verbatim in a
        # message; compare the digits instead.
        if contact.kind is ContactKind.PHONE:
            return contact.value.removeprefix("+1") in re.sub(r"\D", "", text)
        return contact.value.casefold() in text.casefold()

    async def sessions_for_contact(self, tenant_id: str, contact: Contact) -> tuple[uuid.UUID, ...]:
        async with self._conversations._lock:
            sessions = {
                key[1]
                for key in self._conversations._messages
                if key[0] == tenant_id
                and any(
                    self._matches(contact, message.content)
                    for message in self._conversations._messages[key]
                )
            }
        async with self._turn_records._lock:
            for record in self._turn_records._records.values():
                if record.tenant_id == tenant_id and self._matches(contact, str(record.content)):
                    sessions.add(record.session_id)
        for booking in self._bookings._records:
            if booking.tenant_id == tenant_id and self._matches(contact, booking.contact.value):
                sessions.add(uuid.UUID(booking.session_id))
        for lead in self._leads._records:
            if lead.tenant_id == tenant_id and self._matches(contact, lead.contact.value):
                sessions.add(uuid.UUID(lead.session_id))
        return tuple(sorted(sessions, key=str))

    async def subject_records(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> SubjectRecords:
        wanted = {str(session) for session in session_ids}
        sessions = [
            record
            for (tenant, session), record in self._conversations._sessions.items()
            if tenant == tenant_id and str(session) in wanted
        ]
        messages = [
            message
            for (tenant, session), entries in self._conversations._messages.items()
            if tenant == tenant_id and str(session) in wanted
            for message in entries
        ]
        return SubjectRecords(
            sessions=tuple(sessions),
            messages=tuple(messages),
            leads=tuple(
                record
                for record in self._leads._records
                if record.tenant_id == tenant_id and record.session_id in wanted
            ),
            bookings=tuple(
                record
                for record in self._bookings._records
                if record.tenant_id == tenant_id and record.session_id in wanted
            ),
            handoffs=tuple(
                record
                for record in self._handoffs._records
                if record.tenant_id == tenant_id and record.session_id in wanted
            ),
            consent=tuple(
                record
                for record in self._consent._records.values()
                if record.tenant_id == tenant_id and record.session_id in wanted
            ),
            turn_records=tuple(
                record
                for record in self._turn_records._records.values()
                if record.tenant_id == tenant_id and str(record.session_id) in wanted
            ),
            projections=(),
        )

    async def erase_subject(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> ErasureReport:
        wanted = {str(session) for session in session_ids}
        async with self._conversations._lock:
            for tenant, session in list(self._conversations._messages):
                if tenant == tenant_id and str(session) in wanted:
                    del self._conversations._messages[(tenant, session)]
                    self._conversations._sessions.pop((tenant, session), None)
        async with self._consent._lock:
            consent_records_deleted = 0
            for key in list(self._consent._records):
                if key[0] == tenant_id and key[1] in wanted:
                    del self._consent._records[key]
                    consent_records_deleted += 1
        async with self._turn_records._lock:
            turn_records_deleted = 0
            for turn_id in list(self._turn_records._records):
                record = self._turn_records._records[turn_id]
                if record.tenant_id == tenant_id and str(record.session_id) in wanted:
                    del self._turn_records._records[turn_id]
                    turn_records_deleted += 1
        bookings = len(
            [
                r
                for r in self._bookings._records
                if r.tenant_id == tenant_id and r.session_id in wanted
            ]
        )
        leads = len(
            [r for r in self._leads._records if r.tenant_id == tenant_id and r.session_id in wanted]
        )
        handoffs = len(
            [
                r
                for r in self._handoffs._records
                if r.tenant_id == tenant_id and r.session_id in wanted
            ]
        )
        self._bookings._records = [
            r
            for r in self._bookings._records
            if not (r.tenant_id == tenant_id and r.session_id in wanted)
        ]
        self._leads._records = [
            r
            for r in self._leads._records
            if not (r.tenant_id == tenant_id and r.session_id in wanted)
        ]
        self._handoffs._records = [
            r
            for r in self._handoffs._records
            if not (r.tenant_id == tenant_id and r.session_id in wanted)
        ]
        return ErasureReport(
            sessions_deleted=len(wanted),
            messages_deleted=0,
            leads_anonymized=leads,
            bookings_anonymized=bookings,
            handoffs_anonymized=handoffs,
            consent_records_deleted=consent_records_deleted,
            checkpoints_deleted=0,
            turn_records_deleted=turn_records_deleted,
        )

    async def purge_expired(
        self, tenant_id: str, policy: RetentionPolicy, *, now: datetime
    ) -> PurgeReport:
        transcript_age = policy.max_age(DataClass.TRANSCRIPT)
        trace_age = policy.max_age(DataClass.INFERENCE_TRACE)
        if transcript_age is None and trace_age is None:
            return PurgeReport(0, 0, 0, 0, 0)
        async with self._turn_records._lock:
            trace_cutoff = now - trace_age if trace_age is not None else None
            turn_records_deleted = 0
            for turn_id in list(self._turn_records._records):
                record = self._turn_records._records[turn_id]
                if (
                    record.tenant_id == tenant_id
                    and trace_cutoff is not None
                    and record.recorded_at < trace_cutoff
                ):
                    del self._turn_records._records[turn_id]
                    turn_records_deleted += 1
        if transcript_age is None:
            return PurgeReport(0, 0, 0, 0, turn_records_deleted)
        cutoff = now - transcript_age
        async with self._conversations._lock:
            purgeable = [
                key
                for key, records in self._conversations._messages.items()
                if key[0] == tenant_id and any(record.created_at < cutoff for record in records)
            ]
            messages_deleted = sum(
                len(records)
                for key, records in self._conversations._messages.items()
                if key in purgeable
            )
            sessions_deleted = 0
            for key in purgeable:
                if key in self._conversations._sessions:
                    self._conversations._sessions.pop(key, None)
                    sessions_deleted += 1
                del self._conversations._messages[key]
        async with self._consent._lock:
            purged_sessions = {str(key[1]) for key in purgeable}
            consent_records_deleted = 0
            for grant_key in list(self._consent._records):
                if grant_key[1] in purged_sessions:
                    del self._consent._records[grant_key]
                    consent_records_deleted += 1
        return PurgeReport(
            sessions_deleted=sessions_deleted,
            messages_deleted=messages_deleted,
            tool_executions_deleted=0,
            consent_records_deleted=consent_records_deleted,
            turn_records_deleted=turn_records_deleted,
        )

    async def create_privacy_request(
        self, tenant_id: str, *, contact: Contact, requested_by: str
    ) -> PrivacyRequestRecord:
        record = PrivacyRequestRecord(
            request_id=uuid.uuid4(),
            tenant_id=tenant_id,
            status="pending",
            contact_kind=contact.kind.value,
            contact_value=contact.value,
            requested_by=requested_by,
            requested_at=datetime.now(UTC),
            processed_at=None,
        )
        async with self._lock:
            self._requests[record.request_id] = record
        return record

    async def pending_privacy_requests(self) -> tuple[PrivacyRequestRecord, ...]:
        async with self._lock:
            return tuple(record for record in self._requests.values() if record.status == "pending")

    async def complete_privacy_request(
        self, request_id: uuid.UUID, *, processed_at: datetime
    ) -> None:
        async with self._lock:
            record = self._requests.get(request_id)
            if record is None:
                raise NotFoundError(detail="no privacy request with this id")
            self._requests[request_id] = replace(
                record,
                status="completed",
                processed_at=processed_at,
                contact_value=ANONYMIZED_NAME,
            )

    async def fail_privacy_request(self, request_id: uuid.UUID) -> None:
        async with self._lock:
            record = self._requests.get(request_id)
            if record is None:
                raise NotFoundError(detail="no privacy request with this id")
            self._requests[request_id] = replace(
                record, status="failed", processed_at=datetime.now(UTC)
            )

    async def requests_for_tenant(self, tenant_id: str) -> tuple[PrivacyRequestRecord, ...]:
        async with self._lock:
            records = [
                record for record in self._requests.values() if record.tenant_id == tenant_id
            ]
        records.sort(key=lambda record: record.requested_at, reverse=True)
        return tuple(records)


class KnowledgeStore(Protocol):
    """The knowledge system of record's ingestion-facing surface (RAG-001).

    The ingestion job, the upload route, and the integrity detector all work
    through this contract; the in-memory fake below and the PostgreSQL
    repository both satisfy it.
    """

    async def stage_version(
        self,
        tenant_id: str,
        *,
        source_id: uuid.UUID,
        external_key: str,
        title: str,
        checksum: ContentChecksum,
        byte_size: int,
        media_type: str,
        storage_key: str,
        visibility: Visibility = Visibility.PUBLIC,
    ) -> KnowledgeDocument: ...
    async def load_document(self, tenant_id: str, document_id: uuid.UUID) -> KnowledgeDocument: ...
    async def document_for_version(
        self, tenant_id: str, version_id: uuid.UUID
    ) -> KnowledgeDocument: ...
    async def record_indexing_started(
        self, tenant_id: str, version_id: uuid.UUID
    ) -> KnowledgeDocument: ...
    async def record_indexed(
        self, tenant_id: str, version_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument: ...
    async def record_index_failure(
        self, tenant_id: str, version_id: uuid.UUID, *, error_code: str
    ) -> KnowledgeDocument: ...
    async def versions_in_state(
        self, tenant_id: str, state: VersionState
    ) -> tuple[DocumentVersion, ...]: ...
    async def quarantine(
        self, tenant_id: str, version_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        """Withdraw a version from retrieval pending review (`RAG-007`).

        The ingestion worker calls this when the content-safety scan finds
        suspicious embedded instructions or unsupported active content. Only a
        :meth:`quarantine_review` may lift it.
        """
        ...

    async def quarantine_review(
        self,
        tenant_id: str,
        version_id: uuid.UUID,
        *,
        approved: bool,
        reviewed_by: str,
        at: datetime,
    ) -> KnowledgeDocument:
        """Apply a reviewer's decision on a quarantined version.

        Approval clears the quarantine so ingestion may re-run; rejection
        leaves the version quarantined.
        """
        ...

    async def versions_in_safety_state(
        self, tenant_id: str, state: SafetyState
    ) -> tuple[DocumentVersion, ...]:
        """The tenant's versions in one safety state, for the review queue."""
        ...

    async def list_sources(self, tenant_id: str) -> tuple[KnowledgeSource, ...]:
        """Every source one tenant owns, for the admin console."""
        ...

    async def load_source(self, tenant_id: str, source_id: uuid.UUID) -> KnowledgeSource:
        """One source, tenant-qualified.

        Raises:
            NotFoundError: the source is absent or belongs to another tenant.
        """
        ...

    async def documents_for_source(
        self, tenant_id: str, source_id: uuid.UUID
    ) -> tuple[KnowledgeDocument, ...]:
        """Every document under one source, with full revision history."""
        ...

    # The operator lifecycle surface `FEAT-001` drives: every write goes
    # through the aggregate's plan methods, so the rules stay in the domain.
    async def register_source(
        self,
        tenant_id: str,
        *,
        domain: KnowledgeDomain,
        kind: SourceKind,
        display_name: str,
        external_reference: str | None = None,
    ) -> KnowledgeSource:
        """Create a source, or return the one already registered under the name."""
        ...

    async def set_source_enabled(
        self, tenant_id: str, source_id: uuid.UUID, *, enabled: bool
    ) -> KnowledgeSource:
        """Withdraw or restore every document under a source at once."""
        ...

    async def approve(
        self, tenant_id: str, version_id: uuid.UUID, *, approved_by: str, at: datetime
    ) -> KnowledgeDocument:
        """Record that a reviewer accepted a draft."""
        ...

    async def publish(
        self,
        tenant_id: str,
        version_id: uuid.UUID,
        *,
        at: datetime,
        effective_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> KnowledgeDocument:
        """Make one approved version current, superseding whichever was."""
        ...

    async def expire(
        self, tenant_id: str, version_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        """End the current version's effective window."""
        ...

    async def delete_document(
        self, tenant_id: str, document_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        """Withdraw a document and every revision of it (a tombstone)."""
        ...


class InMemoryKnowledgeStore:
    """Hermetic fake of the knowledge system of record (RAG-001).

    Mirrors :class:`tenantchat.api.persistence.knowledge.PostgresKnowledgeStore`
    for the ingestion lifecycle's unit tests. Rules stay in the domain — every
    transition goes through the aggregate's plan methods — so a test that passes
    against this fake is testing the same decisions the repository makes.
    """

    def __init__(self) -> None:
        self._sources: dict[tuple[str, uuid.UUID], KnowledgeSource] = {}
        self._source_names: dict[tuple[str, str, str], uuid.UUID] = {}
        self._documents: dict[uuid.UUID, KnowledgeDocument] = {}
        self._document_keys: dict[tuple[str, uuid.UUID, str], uuid.UUID] = {}

    def _source(self, tenant_id: str, source_id: uuid.UUID) -> KnowledgeSource:
        source = self._sources.get((tenant_id, source_id))
        if source is None:
            raise NotFoundError(detail=f"source {source_id} absent or outside tenant {tenant_id}")
        return source

    def _document_for_version(self, tenant_id: str, version_id: uuid.UUID) -> KnowledgeDocument:
        for document in self._documents.values():
            if document.tenant_id != tenant_id:
                continue
            for version in document.versions:
                if version.version_id == version_id:
                    return document
        raise NotFoundError(detail=f"version {version_id} absent or outside tenant {tenant_id}")

    def _document(self, tenant_id: str, document_id: uuid.UUID) -> KnowledgeDocument:
        document = self._documents.get(document_id)
        if document is None or document.tenant_id != tenant_id:
            detail = f"document {document_id} absent or outside tenant {tenant_id}"
            raise NotFoundError(detail=detail)
        return document

    @staticmethod
    def _apply(document: KnowledgeDocument, version: DocumentVersion) -> KnowledgeDocument:
        return replace(
            document,
            versions=tuple(
                version if item.version_id == version.version_id else item
                for item in document.versions
            ),
        )

    async def register_source(
        self,
        tenant_id: str,
        *,
        domain: KnowledgeDomain,
        kind: SourceKind,
        display_name: str,
        external_reference: str | None = None,
    ) -> KnowledgeSource:
        key = (tenant_id, domain.value, display_name)
        existing = self._source_names.get(key)
        if existing is not None:
            return self._source(tenant_id, existing)
        source = KnowledgeSource(
            source_id=uuid.uuid4(),
            tenant_id=tenant_id,
            domain=domain,
            kind=kind,
            display_name=display_name,
        )
        self._sources[(tenant_id, source.source_id)] = source
        self._source_names[key] = source.source_id
        return source

    async def set_source_enabled(
        self, tenant_id: str, source_id: uuid.UUID, *, enabled: bool
    ) -> KnowledgeSource:
        source = self._source(tenant_id, source_id)
        updated = replace(source, enabled=enabled)
        self._sources[(tenant_id, source_id)] = updated
        return updated

    async def stage_version(
        self,
        tenant_id: str,
        *,
        source_id: uuid.UUID,
        external_key: str,
        title: str,
        checksum: ContentChecksum,
        byte_size: int,
        media_type: str,
        storage_key: str,
        visibility: Visibility = Visibility.PUBLIC,
    ) -> KnowledgeDocument:
        source = self._source(tenant_id, source_id)
        document_id = self._document_keys.get((tenant_id, source_id, external_key))
        if document_id is None:
            document = KnowledgeDocument(
                document_id=uuid.uuid4(),
                tenant_id=tenant_id,
                source=source,
                external_key=external_key,
                title=title,
            )
            self._documents[document.document_id] = document
            self._document_keys[(tenant_id, source_id, external_key)] = document.document_id
        else:
            document = self._document(tenant_id, document_id)
        if document.deleted:
            raise NotFoundError(detail=f"document {document.document_id} is deleted")
        if document.version_with_checksum(checksum) is not None:
            return document
        version = DocumentVersion(
            version_id=uuid.uuid4(),
            tenant_id=tenant_id,
            document_id=document.document_id,
            revision=document.next_revision(),
            state=VersionState.DRAFT,
            indexing_state=IndexingState.PENDING,
            visibility=visibility,
            checksum=checksum,
            byte_size=byte_size,
            media_type=media_type,
            storage_key=storage_key,
        )
        updated = replace(document, versions=(*document.versions, version))
        self._documents[document.document_id] = updated
        return updated

    async def load_document(self, tenant_id: str, document_id: uuid.UUID) -> KnowledgeDocument:
        return self._document(tenant_id, document_id)

    async def document_for_version(
        self, tenant_id: str, version_id: uuid.UUID
    ) -> KnowledgeDocument:
        return self._document_for_version(tenant_id, version_id)

    async def approve(
        self, tenant_id: str, version_id: uuid.UUID, *, approved_by: str, at: datetime
    ) -> KnowledgeDocument:
        document = self._document_for_version(tenant_id, version_id)
        plan = document.plan_approval(version_id, approved_by=approved_by, at=at)
        version = document.version(version_id)
        updated = self._apply(
            document,
            replace(
                version,
                state=VersionState.APPROVED,
                approved_at=plan.approved_at,
                approved_by=plan.approved_by,
            ),
        )
        self._documents[document.document_id] = updated
        return updated

    async def publish(
        self,
        tenant_id: str,
        version_id: uuid.UUID,
        *,
        at: datetime,
        effective_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> KnowledgeDocument:
        document = self._document_for_version(tenant_id, version_id)
        plan = document.plan_publication(
            version_id, at=at, effective_at=effective_at, expires_at=expires_at
        )
        versions: list[DocumentVersion] = []
        for item in document.versions:
            if item.version_id == plan.supersedes_version_id:
                versions.append(
                    replace(item, state=VersionState.SUPERSEDED, superseded_at=plan.published_at)
                )
            elif item.version_id == plan.version_id:
                versions.append(
                    replace(
                        item,
                        state=VersionState.PUBLISHED,
                        effective_at=plan.effective_at,
                        expires_at=plan.expires_at,
                        published_at=plan.published_at,
                        superseded_at=None,
                    )
                )
            else:
                versions.append(item)
        updated = replace(document, versions=tuple(versions))
        self._documents[document.document_id] = updated
        return updated

    async def expire(
        self, tenant_id: str, version_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        document = self._document_for_version(tenant_id, version_id)
        plan = document.plan_expiry(version_id, at=at)
        version = document.version(version_id)
        updated = self._apply(document, replace(version, expires_at=plan.expires_at))
        self._documents[document.document_id] = updated
        return updated

    async def record_indexing_started(
        self, tenant_id: str, version_id: uuid.UUID
    ) -> KnowledgeDocument:
        return await self._record_indexing(tenant_id, version_id, state=IndexingState.INDEXING)

    async def record_indexed(
        self, tenant_id: str, version_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        return await self._record_indexing(
            tenant_id, version_id, state=IndexingState.INDEXED, indexed_at=at
        )

    async def record_index_failure(
        self, tenant_id: str, version_id: uuid.UUID, *, error_code: str
    ) -> KnowledgeDocument:
        return await self._record_indexing(
            tenant_id, version_id, state=IndexingState.FAILED, error_code=error_code
        )

    async def _record_indexing(
        self,
        tenant_id: str,
        version_id: uuid.UUID,
        *,
        state: IndexingState,
        indexed_at: datetime | None = None,
        error_code: str | None = None,
    ) -> KnowledgeDocument:
        document = self._document_for_version(tenant_id, version_id)
        document.version_for_indexing(version_id)
        version = document.version(version_id)
        updated = self._apply(
            document,
            replace(
                version,
                indexing_state=state,
                indexed_at=indexed_at,
                index_error_code=error_code,
            ),
        )
        self._documents[document.document_id] = updated
        return updated

    async def delete_document(
        self, tenant_id: str, document_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        document = self._document(tenant_id, document_id)
        if document.deleted:
            return document
        updated = replace(
            document,
            deleted=True,
            versions=tuple(replace(item, state=VersionState.DELETED) for item in document.versions),
        )
        self._documents[document_id] = updated
        return updated

    async def retrievable_versions(self, context: RetrievalContext) -> tuple[DocumentVersion, ...]:
        versions: list[DocumentVersion] = []
        for document in self._documents.values():
            if document.tenant_id != context.tenant_id:
                continue
            version = document.retrievable_version(context)
            if version is not None:
                versions.append(version)
        versions.sort(key=lambda item: (item.document_id, item.revision))
        return tuple(versions)

    async def versions_in_state(
        self, tenant_id: str, state: VersionState
    ) -> tuple[DocumentVersion, ...]:
        versions: list[DocumentVersion] = []
        for document in self._documents.values():
            if document.tenant_id != tenant_id:
                continue
            versions.extend(item for item in document.versions if item.state is state)
        versions.sort(key=lambda item: (item.document_id, item.revision))
        return tuple(versions)

    async def quarantine(
        self, tenant_id: str, version_id: uuid.UUID, *, at: datetime
    ) -> KnowledgeDocument:
        document = self._document_for_version(tenant_id, version_id)
        version = document.version(version_id)
        updated = self._apply(
            document,
            replace(
                version, safety_state=SafetyState.QUARANTINED, indexing_state=IndexingState.PENDING
            ),
        )
        self._documents[document.document_id] = updated
        return updated

    async def quarantine_review(
        self,
        tenant_id: str,
        version_id: uuid.UUID,
        *,
        approved: bool,
        reviewed_by: str,
        at: datetime,
    ) -> KnowledgeDocument:
        document = self._document_for_version(tenant_id, version_id)
        plan = document.plan_quarantine_review(
            version_id, approved=approved, reviewed_by=reviewed_by, at=at
        )
        version = document.version(version_id)
        safety_state = SafetyState.CLEAR if plan.approved else SafetyState.QUARANTINED
        updated = self._apply(document, replace(version, safety_state=safety_state))
        self._documents[document.document_id] = updated
        return updated

    async def versions_in_safety_state(
        self, tenant_id: str, safety_state: SafetyState
    ) -> tuple[DocumentVersion, ...]:
        versions: list[DocumentVersion] = []
        for document in self._documents.values():
            if document.tenant_id != tenant_id:
                continue
            versions.extend(item for item in document.versions if item.safety_state is safety_state)
        versions.sort(key=lambda item: (item.document_id, item.revision))
        return tuple(versions)

    async def list_sources(self, tenant_id: str) -> tuple[KnowledgeSource, ...]:
        sources: list[KnowledgeSource] = []
        for (owner, _source_id), source in self._sources.items():
            if owner == tenant_id:
                sources.append(source)
        sources.sort(key=lambda source: (source.domain.value, source.display_name))
        return tuple(sources)

    async def load_source(self, tenant_id: str, source_id: uuid.UUID) -> KnowledgeSource:
        return self._source(tenant_id, source_id)

    async def documents_for_source(
        self, tenant_id: str, source_id: uuid.UUID
    ) -> tuple[KnowledgeDocument, ...]:
        source = self._source(tenant_id, source_id)
        documents = [
            document
            for document in self._documents.values()
            if document.tenant_id == tenant_id and document.source.source_id == source.source_id
        ]
        documents.sort(key=lambda document: document.external_key)
        return tuple(documents)


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# AGENT-001: durable routing decisions and agent workflows.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RoutingRow:
    """One persisted routing decision, one row of ``routing_decisions``.

    ``candidates`` is the whole scored candidate list, not the winner: this is
    the record `OBS-004` diagnoses a misrouted turn from.
    """

    turn_index: int
    tenant_id: str
    session_id: str
    policy_version: str
    agent_version: str
    outcome: RoutingOutcome
    rule: RoutingRule
    chosen_intent: IntentName | None
    confidence: float
    candidates: tuple[IntentCandidate, ...]
    direct_threshold: float
    clarify_threshold: float
    conflict_gap: float
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowRow:
    """One workflow row, as the store reads and writes it."""

    workflow_id: str
    tenant_id: str
    session_id: str
    intent: IntentName
    agent_version: str
    status: WorkflowStatus
    collected_fields: dict[str, str]
    pending_confirmation: dict[str, object] | None
    tool_results: tuple[dict[str, object], ...]
    next_allowed_actions: tuple[str, ...]
    turn_index: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class WorkflowEventRow:
    """One workflow event, as the store reads it back.

    ``kind`` names the mutation: the seven state-machine transitions plus
    ``start`` and ``update``, which are logged the same way.
    """

    workflow_id: str
    kind: str
    payload: dict[str, object]
    created_at: datetime


class WorkflowStore(Protocol):
    """The persistence contract behind the workflow application service.

    The graph never sees this type: the idempotent service in
    :mod:`tenantchat.api.actions` wraps these methods with validation and
    converts the rows to the domain's
    :class:`~tenantchat.core.workflows.WorkflowState`.
    """

    async def record_routing(
        self,
        *,
        tenant_id: str,
        session_id: str,
        turn_index: int,
        decision: RoutingDecision,
        agent_version: str,
        idempotency_key: IdempotencyKey,
    ) -> None:
        """Persist one decision, deduplicated by (session, turn)."""
        ...

    async def current(self, tenant_id: str, session_id: str) -> WorkflowRow | None:
        """The session's active or paused workflow, newest first."""
        ...

    async def last_routing(self, tenant_id: str, session_id: str) -> RoutingRow | None:
        """The session's most recent routing decision."""
        ...

    async def start(
        self,
        *,
        tenant_id: str,
        session_id: str,
        intent: IntentName,
        agent_version: str,
        next_allowed_actions: tuple[str, ...],
        turn_index: int,
        idempotency_key: IdempotencyKey,
    ) -> WorkflowRow:
        """Open an active workflow, returning the existing one on replay."""
        ...

    async def update(
        self,
        *,
        tenant_id: str,
        session_id: str,
        workflow_id: str,
        collected_fields: Mapping[str, str],
        tool_results: tuple[ToolResult, ...],
        next_allowed_actions: tuple[str, ...],
        turn_index: int,
        idempotency_key: IdempotencyKey,
    ) -> WorkflowRow:
        """Merge one turn's evidence into the workflow, replay-safe by key."""
        ...

    async def transition(
        self,
        *,
        tenant_id: str,
        session_id: str,
        workflow_id: str,
        transition: WorkflowTransition,
        payload: Mapping[str, object],
        idempotency_key: IdempotencyKey,
    ) -> WorkflowRow:
        """Apply one transition exactly once per key, or refuse it."""
        ...

    async def routing_decisions(self, tenant_id: str, session_id: str) -> tuple[RoutingRow, ...]:
        """Every recorded decision for a session, ascending by turn."""
        ...

    async def workflows(self, tenant_id: str, session_id: str) -> tuple[WorkflowRow, ...]:
        """Every workflow a session has run, oldest first."""
        ...

    async def events(self, tenant_id: str, workflow_id: str) -> tuple[WorkflowEventRow, ...]:
        """A workflow's event log, oldest first."""
        ...


class InMemoryWorkflowStore:
    """A concurrency-safe fake with the same semantics as the PostgreSQL store.

    Transitions go through the same pure domain function the PostgreSQL store
    calls, so an invalid transition is refused identically against both.
    """

    def __init__(self) -> None:
        self._routing: dict[tuple[str, str, int], RoutingRow] = {}
        self._workflows: dict[tuple[str, str, str], WorkflowRow] = {}
        self._events: dict[tuple[str, str, str, str], WorkflowEventRow] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _event_key(
        tenant_id: str, workflow_id: str, key: IdempotencyKey
    ) -> tuple[str, str, str, str]:
        return (tenant_id, workflow_id, key.value, "event")

    async def record_routing(
        self,
        *,
        tenant_id: str,
        session_id: str,
        turn_index: int,
        decision: RoutingDecision,
        agent_version: str,
        idempotency_key: IdempotencyKey,
    ) -> None:
        del idempotency_key  # the (session, turn) key is the deduplication
        async with self._lock:
            self._routing[(tenant_id, session_id, turn_index)] = RoutingRow(
                turn_index=turn_index,
                tenant_id=tenant_id,
                session_id=session_id,
                policy_version=decision.policy_version,
                agent_version=agent_version,
                outcome=decision.outcome,
                rule=decision.rule,
                chosen_intent=decision.chosen,
                confidence=decision.confidence,
                candidates=decision.candidates,
                direct_threshold=decision.direct_threshold,
                clarify_threshold=decision.clarify_threshold,
                conflict_gap=decision.conflict_gap,
                created_at=datetime.now(UTC),
            )

    async def current(self, tenant_id: str, session_id: str) -> WorkflowRow | None:
        async with self._lock:
            candidates = [
                row
                for row in self._workflows.values()
                if row.tenant_id == tenant_id
                and row.session_id == session_id
                and row.status in (WorkflowStatus.ACTIVE, WorkflowStatus.PAUSED)
            ]
            if not candidates:
                return None
            return max(candidates, key=lambda row: (row.created_at, row.workflow_id))

    async def last_routing(self, tenant_id: str, session_id: str) -> RoutingRow | None:
        async with self._lock:
            rows = [
                row
                for row in self._routing.values()
                if row.tenant_id == tenant_id and row.session_id == session_id
            ]
            if not rows:
                return None
            return max(rows, key=lambda row: row.turn_index)

    async def start(
        self,
        *,
        tenant_id: str,
        session_id: str,
        intent: IntentName,
        agent_version: str,
        next_allowed_actions: tuple[str, ...],
        turn_index: int,
        idempotency_key: IdempotencyKey,
    ) -> WorkflowRow:
        async with self._lock:
            existing = [
                row
                for row in self._workflows.values()
                if row.tenant_id == tenant_id
                and row.session_id == session_id
                and row.status is WorkflowStatus.ACTIVE
            ]
            if existing:
                return max(existing, key=lambda row: (row.created_at, row.workflow_id))
            now = datetime.now(UTC)
            row = WorkflowRow(
                workflow_id=f"wf-{uuid.uuid4().hex}",
                tenant_id=tenant_id,
                session_id=session_id,
                intent=intent,
                agent_version=agent_version,
                status=WorkflowStatus.ACTIVE,
                collected_fields={},
                pending_confirmation=None,
                tool_results=(),
                next_allowed_actions=next_allowed_actions,
                turn_index=turn_index,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            self._workflows[(tenant_id, session_id, row.workflow_id)] = row
            self._record_event(row, "start", {}, idempotency_key)
            return row

    async def update(
        self,
        *,
        tenant_id: str,
        session_id: str,
        workflow_id: str,
        collected_fields: Mapping[str, str],
        tool_results: tuple[ToolResult, ...],
        next_allowed_actions: tuple[str, ...],
        turn_index: int,
        idempotency_key: IdempotencyKey,
    ) -> WorkflowRow:
        async with self._lock:
            row = self._workflow(tenant_id, session_id, workflow_id)
            event_key = self._event_key(tenant_id, workflow_id, idempotency_key)
            if event_key in self._events:
                # A replayed update: the merge is content-idempotent, so the
                # existing row is already the state the replay would produce,
                # and re-recording the event would restamp its timestamp.
                return row
            merged_fields = {**row.collected_fields, **collected_fields}
            by_call_id = {result["call_id"]: result for result in row.tool_results}
            for result in tool_results:
                by_call_id[result.call_id] = {
                    "call_id": result.call_id,
                    "name": result.name,
                    "result": result.result,
                }
            updated = replace(
                row,
                collected_fields=merged_fields,
                tool_results=tuple(by_call_id.values()),
                next_allowed_actions=next_allowed_actions,
                turn_index=turn_index,
                updated_at=datetime.now(UTC),
            )
            self._workflows[(tenant_id, session_id, workflow_id)] = updated
            self._record_event(
                updated,
                "update",
                {
                    "fields": dict(collected_fields),
                    "results": [result.call_id for result in tool_results],
                },
                idempotency_key,
            )
            return updated

    async def transition(
        self,
        *,
        tenant_id: str,
        session_id: str,
        workflow_id: str,
        transition: WorkflowTransition,
        payload: Mapping[str, object],
        idempotency_key: IdempotencyKey,
    ) -> WorkflowRow:
        async with self._lock:
            row = self._workflow(tenant_id, session_id, workflow_id)
            event_key = self._event_key(tenant_id, workflow_id, idempotency_key)
            if event_key in self._events:
                return row
            moved = transition_workflow(
                _row_state(row), transition, payload=payload, now=datetime.now(UTC)
            )
            updated = _state_row(moved)
            self._workflows[(tenant_id, session_id, workflow_id)] = updated
            self._record_event(updated, transition.value, payload, idempotency_key)
            return updated

    async def routing_decisions(self, tenant_id: str, session_id: str) -> tuple[RoutingRow, ...]:
        async with self._lock:
            rows = [
                row
                for row in self._routing.values()
                if row.tenant_id == tenant_id and row.session_id == session_id
            ]
            return tuple(sorted(rows, key=lambda row: row.turn_index))

    async def workflows(self, tenant_id: str, session_id: str) -> tuple[WorkflowRow, ...]:
        async with self._lock:
            rows = [
                row
                for row in self._workflows.values()
                if row.tenant_id == tenant_id and row.session_id == session_id
            ]
            return tuple(sorted(rows, key=lambda row: (row.created_at, row.workflow_id)))

    async def events(self, tenant_id: str, workflow_id: str) -> tuple[WorkflowEventRow, ...]:
        async with self._lock:
            rows = [
                row
                for (tenant, workflow, _key, _kind), row in self._events.items()
                if tenant == tenant_id and workflow == workflow_id
            ]
            return tuple(sorted(rows, key=lambda row: row.created_at))

    def _workflow(self, tenant_id: str, session_id: str, workflow_id: str) -> WorkflowRow:
        row = self._workflows.get((tenant_id, session_id, workflow_id))
        if row is None:
            raise NotFoundError(detail="workflow absent or outside tenant")
        return row

    def _record_event(
        self,
        row: WorkflowRow,
        kind: str,
        payload: Mapping[str, object],
        key: IdempotencyKey,
    ) -> None:
        self._events[self._event_key(row.tenant_id, row.workflow_id, key)] = WorkflowEventRow(
            workflow_id=row.workflow_id,
            kind=kind,
            payload=dict(payload),
            created_at=row.updated_at,
        )


def _row_state(row: WorkflowRow) -> WorkflowState:
    return WorkflowState(
        workflow_id=row.workflow_id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        intent=row.intent,
        agent_version=row.agent_version,
        status=row.status,
        collected_fields=row.collected_fields,
        pending_confirmation=row.pending_confirmation,
        tool_results=tuple(
            ToolResult(
                call_id=str(result["call_id"]),
                name=str(result["name"]),
                result=str(result["result"]),
            )
            for result in row.tool_results
        ),
        next_allowed_actions=row.next_allowed_actions,
        turn_index=row.turn_index,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _state_row(state: WorkflowState) -> WorkflowRow:
    return WorkflowRow(
        workflow_id=state.workflow_id,
        tenant_id=state.tenant_id,
        session_id=state.session_id,
        intent=state.intent,
        agent_version=state.agent_version,
        status=state.status,
        collected_fields=dict(state.collected_fields),
        pending_confirmation=(
            dict(state.pending_confirmation) if state.pending_confirmation is not None else None
        ),
        tool_results=tuple(
            {
                "call_id": result.call_id,
                "name": result.name,
                "result": result.result,
            }
            for result in state.tool_results
        ),
        next_allowed_actions=state.next_allowed_actions,
        turn_index=state.turn_index,
        created_at=state.created_at,
        updated_at=state.updated_at,
        completed_at=state.completed_at,
    )
