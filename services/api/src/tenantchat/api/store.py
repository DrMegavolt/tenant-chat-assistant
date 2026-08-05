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
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from tenantchat.core.commands import (
    BookingCommand,
    HandoffCommand,
    HandoffReason,
    LeadCommand,
    LeadUrgency,
)
from tenantchat.core.contact import Contact, ContactKind
from tenantchat.core.errors import ConflictError, NotFoundError, SlotUnavailableError
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


def _reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


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
    handoff_id: str
    tenant_id: str
    session_id: str
    reason: HandoffReason
    summary: str
    created_at: datetime


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
    ) -> MessageRecord: ...

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

    async def for_tenant(self, tenant_id: str) -> tuple[BookingRecord, ...]: ...


class LeadStore(Protocol):
    async def record(self, command: LeadCommand, *, session_id: str) -> LeadRecord: ...

    async def for_tenant(self, tenant_id: str) -> tuple[LeadRecord, ...]: ...


class HandoffStore(Protocol):
    async def record(self, command: HandoffCommand, *, session_id: str) -> HandoffRecord: ...

    async def for_tenant(self, tenant_id: str) -> tuple[HandoffRecord, ...]: ...


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


class AuditStore(Protocol):
    """Append-only accountability records, one per administrative mutation.

    ``record`` is fire-and-forget from the caller's perspective: the event's
    occurred_at is authoritative, so the implementation stamps it. Reads are
    tenant-qualified only; `PRIV-001` consumes this surface for retention,
    export, and erasure of an operator's records.
    """

    async def record(self, event: AuditEvent) -> AuditEvent: ...

    async def for_tenant(self, tenant_id: str, *, limit: int = 200) -> tuple[AuditEvent, ...]: ...


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


class InMemoryConversationStore:
    """A concurrency-safe fake; production composition never constructs it."""

    def __init__(self) -> None:
        self._sessions: dict[tuple[str, uuid.UUID], ConversationRecord] = {}
        self._messages: dict[tuple[str, uuid.UUID], list[MessageRecord]] = {}
        self._lock = asyncio.Lock()

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

    async def for_tenant(self, tenant_id: str) -> tuple[BookingRecord, ...]:
        return tuple(record for record in self._records if record.tenant_id == tenant_id)


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

    async def for_tenant(self, tenant_id: str) -> tuple[LeadRecord, ...]:
        return tuple(record for record in self._records if record.tenant_id == tenant_id)


class InMemoryHandoffStore:
    """An explicit API test fake, never the production source of truth."""

    def __init__(self) -> None:
        self._records: list[HandoffRecord] = []

    async def record(self, command: HandoffCommand, *, session_id: str) -> HandoffRecord:
        handoff = HandoffRecord(
            handoff_id=_reference("HO"),
            tenant_id=command.tenant_id,
            session_id=session_id,
            reason=command.reason,
            summary=command.summary,
            created_at=datetime.now(UTC),
        )
        self._records.append(handoff)
        return handoff

    async def for_tenant(self, tenant_id: str) -> tuple[HandoffRecord, ...]:
        return tuple(record for record in self._records if record.tenant_id == tenant_id)


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


class InMemoryAuditStore:
    """A concurrency-safe fake; production writes append-only rows."""

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = asyncio.Lock()

    async def record(self, event: AuditEvent) -> AuditEvent:
        async with self._lock:
            self._events.append(event)
        return event

    async def for_tenant(self, tenant_id: str, *, limit: int = 200) -> tuple[AuditEvent, ...]:
        async with self._lock:
            events = [event for event in self._events if event.tenant_id == tenant_id]
        events.sort(key=lambda event: event.occurred_at, reverse=True)
        return tuple(events[:limit])


@dataclass(frozen=True, slots=True)
class _Attempt:
    fingerprint: str
    response: dict[str, object] | None


class InMemoryIdempotencyStore:
    """A concurrency-safe fake with the same claim-then-complete semantics."""

    def __init__(self) -> None:
        self._attempts: dict[tuple[str, str, str], _Attempt] = {}
        self._lock = asyncio.Lock()

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
            if existing is None:
                self._attempts[index] = _Attempt(fingerprint=fingerprint, response=None)
                return None
            if existing.fingerprint != fingerprint:
                raise ConflictError(detail=f"idempotency key reused for a different {scope}")
            if existing.response is None:
                raise ConflictError(detail=f"an earlier {scope} attempt is still in flight")
            return dict(existing.response)

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
                fingerprint=existing.fingerprint, response=dict(response)
            )


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

    ``recorded_at`` is when the turn happened, the timestamp retention purges
    on (independently of, and shorter than, the transcript's).
    """

    turn_id: uuid.UUID
    tenant_id: str
    session_id: uuid.UUID
    trace_id: str | None
    content: dict[str, object]
    recorded_at: datetime


@dataclass(frozen=True, slots=True)
class TurnRecordProjection:
    """A derived dataset pinned to the turn record it was built from.

    `FEAT-008` promotes reviewed turns into evaluation datasets here. Erasure
    of the turn record removes every projection of it (the schema cascades),
    which is what makes "any projection derived from the record" eraseable
    without a second registry.
    """

    projection_id: uuid.UUID
    tenant_id: str
    turn_record_id: uuid.UUID
    kind: str
    created_at: datetime


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
    ) -> TurnRecord:
        """Append one turn record for a session.

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

    async def projections_for_turn(
        self, tenant_id: str, turn_id: uuid.UUID
    ) -> tuple[TurnRecordProjection, ...]:
        """The derived datasets pinned to one turn record (empty when none).

        The schema cascades these off the turn record on erasure; this read
        exists so the trace viewer can see what was derived from what it reads.
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


class InMemoryTurnRecordStore:
    """A concurrency-safe fake; production composition never constructs it."""

    def __init__(self) -> None:
        self._records: dict[uuid.UUID, TurnRecord] = {}
        self._lock = asyncio.Lock()

    async def record(
        self,
        tenant_id: str,
        session_id: uuid.UUID,
        *,
        content: dict[str, object],
        trace_id: str | None = None,
        recorded_at: datetime | None = None,
    ) -> TurnRecord:
        record = TurnRecord(
            turn_id=uuid.uuid4(),
            tenant_id=tenant_id,
            session_id=session_id,
            trace_id=trace_id,
            content=dict(content),
            recorded_at=recorded_at or datetime.now(UTC),
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

    async def projections_for_turn(
        self, tenant_id: str, turn_id: uuid.UUID
    ) -> tuple[TurnRecordProjection, ...]:
        # The in-memory fake has no projection storage: projections are a
        # `FEAT-008` artifact, and the PostgreSQL adapter is authoritative for
        # them. An empty tuple keeps the hermetic read surface honest.
        del tenant_id, turn_id
        return ()


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
        document.plan_approval(version_id, approved_by=approved_by, at=at)
        version = document.version(version_id)
        updated = self._apply(document, replace(version, state=VersionState.APPROVED))
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
                versions.append(replace(item, state=VersionState.SUPERSEDED))
            elif item.version_id == plan.version_id:
                versions.append(
                    replace(
                        item,
                        state=VersionState.PUBLISHED,
                        effective_at=plan.effective_at,
                        expires_at=plan.expires_at,
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
        return await self._record_indexing(tenant_id, version_id, state=IndexingState.INDEXED)

    async def record_index_failure(
        self, tenant_id: str, version_id: uuid.UUID, *, error_code: str
    ) -> KnowledgeDocument:
        return await self._record_indexing(tenant_id, version_id, state=IndexingState.FAILED)

    async def _record_indexing(
        self,
        tenant_id: str,
        version_id: uuid.UUID,
        *,
        state: IndexingState,
    ) -> KnowledgeDocument:
        document = self._document_for_version(tenant_id, version_id)
        document.version_for_indexing(version_id)
        version = document.version(version_id)
        updated = self._apply(document, replace(version, indexing_state=state))
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
