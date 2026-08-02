"""Persistence contracts and explicit in-memory test doubles.

The contracts expose server-issued conversation IDs and append-only message
operations. There is intentionally no method that accepts or replaces a
transcript. PostgreSQL implementations live in :mod:`tenantchat.api.persistence`;
the fakes in this module are injected only by hermetic tests.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from tenantchat.core.commands import BookingCommand, LeadCommand, LeadUrgency
from tenantchat.core.contact import Contact
from tenantchat.core.errors import NotFoundError


def _reference(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


class MessageRole(StrEnum):
    VISITOR = "visitor"
    ASSISTANT = "assistant"
    STAFF = "staff"
    SYSTEM = "system"
    TOOL = "tool"


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


class BookingStore(Protocol):
    async def record(self, command: BookingCommand, *, session_id: str) -> BookingRecord: ...

    async def for_tenant(self, tenant_id: str) -> tuple[BookingRecord, ...]: ...


class LeadStore(Protocol):
    async def record(self, command: LeadCommand, *, session_id: str) -> LeadRecord: ...

    async def for_tenant(self, tenant_id: str) -> tuple[LeadRecord, ...]: ...


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


class InMemoryBookingStore:
    """An explicit API test fake, never the production source of truth."""

    def __init__(self) -> None:
        self._records: list[BookingRecord] = []

    async def record(self, command: BookingCommand, *, session_id: str) -> BookingRecord:
        booking = BookingRecord(
            booking_id=_reference("BK"),
            tenant_id=command.tenant_id,
            session_id=session_id,
            customer_name=command.customer_name,
            contact=command.contact,
            address=command.address,
            service_slug=command.service.slug,
            service_name=command.service.display_name,
            slot=command.slot,
            created_at=datetime.now(UTC),
        )
        self._records.append(booking)
        return booking

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
