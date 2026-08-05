"""Tenant-qualified SQLAlchemy adapters for authoritative business records."""

from __future__ import annotations

import uuid
from dataclasses import replace

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tenantchat.api.persistence.tenancy import require_active_tenant
from tenantchat.api.store import (
    AuditActorType,
    AuditEvent,
    BookingRecord,
    ConversationRecord,
    HandoffRecord,
    LeadRecord,
    MessageRecord,
    MessageRole,
    TenantMembership,
)
from tenantchat.core.commands import (
    BookingCommand,
    HandoffCommand,
    HandoffReason,
    LeadCommand,
    LeadUrgency,
)
from tenantchat.core.contact import Contact
from tenantchat.core.errors import ConflictError, NotFoundError


def _conversation(row: object) -> ConversationRecord:
    mapping = row._mapping  # type: ignore[attr-defined]
    return ConversationRecord(
        session_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        status=mapping["status"],
        outcome=mapping["outcome"],
        version=mapping["version"],
        started_at=mapping["started_at"],
        last_activity_at=mapping["last_activity_at"],
        closed_at=mapping["closed_at"],
    )


def _message(row: object) -> MessageRecord:
    mapping = row._mapping  # type: ignore[attr-defined]
    return MessageRecord(
        message_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        session_id=mapping["chat_session_id"],
        sequence_number=mapping["sequence_number"],
        role=MessageRole(mapping["role"]),
        content=mapping["content"],
        model_name=mapping["model_name"],
        metadata=dict(mapping["metadata"]),
        created_at=mapping["created_at"],
    )


def _lead_urgency(value: str) -> LeadUrgency:
    legacy = {"low": "flexible", "normal": "unknown", "high": "today"}
    return LeadUrgency(legacy.get(value, value))


def _membership(row: object) -> TenantMembership:
    mapping = row._mapping  # type: ignore[attr-defined]
    return TenantMembership(
        tenant_id=mapping["tenant_id"],
        principal_subject=mapping["principal_subject"],
        role=mapping["role"],
        created_at=mapping["created_at"],
        updated_at=mapping["updated_at"],
    )


def _audit_event(row: object) -> AuditEvent:
    mapping = row._mapping  # type: ignore[attr-defined]
    return AuditEvent(
        tenant_id=mapping["tenant_id"],
        actor_type=AuditActorType(mapping["actor_type"]),
        principal_id=mapping["principal_id"],
        action=mapping["action"],
        resource_type=mapping["resource_type"],
        resource_id=mapping["resource_id"],
        request_id=mapping["request_id"],
        details=dict(mapping["details"]),
        occurred_at=mapping["occurred_at"],
    )


async def _action_session(
    connection: AsyncConnection, tenant_id: str, client_correlation_id: str
) -> uuid.UUID:
    """Resolve an untrusted correlation label to a server-owned session row.

    The label is never returned by a read or treated as authorization. The
    tenant-leading unique index only groups today's write-only API actions until
    SEC-002 replaces it with a server-issued visitor credential.
    """
    await require_active_tenant(connection, tenant_id)
    session_id = uuid.uuid4()
    correlation = client_correlation_id or None
    if correlation is None:
        await connection.execute(
            text(
                """
                INSERT INTO chat_sessions (id, tenant_id)
                VALUES (:session_id, :tenant_id)
                """
            ),
            {"session_id": session_id, "tenant_id": tenant_id},
        )
    else:
        inserted = await connection.execute(
            text(
                """
                INSERT INTO chat_sessions (id, tenant_id, client_correlation_id)
                VALUES (:session_id, :tenant_id, :correlation)
                ON CONFLICT (tenant_id, client_correlation_id)
                    WHERE client_correlation_id IS NOT NULL
                DO NOTHING
                RETURNING id
                """
            ),
            {"session_id": session_id, "tenant_id": tenant_id, "correlation": correlation},
        )
        inserted_id = inserted.scalar_one_or_none()
        if inserted_id is not None:
            session_id = inserted_id
        else:
            existing = await connection.execute(
                text(
                    """
                    SELECT id FROM chat_sessions
                    WHERE tenant_id = :tenant_id
                      AND client_correlation_id = :correlation
                    """
                ),
                {"tenant_id": tenant_id, "correlation": correlation},
            )
            session_id = existing.scalar_one()

    locked = await connection.execute(
        text(
            """
            SELECT id FROM chat_sessions
            WHERE tenant_id = :tenant_id AND id = :session_id
            FOR UPDATE
            """
        ),
        {"tenant_id": tenant_id, "session_id": session_id},
    )
    if locked.first() is None:
        raise NotFoundError(detail="conversation absent or outside tenant")
    return session_id


async def _advance_action_session(
    connection: AsyncConnection, tenant_id: str, session_id: uuid.UUID, outcome: str
) -> None:
    updated = await connection.execute(
        text(
            """
            UPDATE chat_sessions
            SET outcome = :outcome, version = version + 1, last_activity_at = now()
            WHERE tenant_id = :tenant_id AND id = :session_id
            """
        ),
        {"tenant_id": tenant_id, "session_id": session_id, "outcome": outcome},
    )
    if updated.rowcount != 1:
        raise NotFoundError(detail="conversation absent or outside tenant")


class PostgresConversationStore:
    """Append-only transcript storage serialized by a tenant-qualified row lock."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def create(self, tenant_id: str) -> ConversationRecord:
        session_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    """
                    INSERT INTO chat_sessions (id, tenant_id)
                    VALUES (:session_id, :tenant_id)
                    RETURNING id, tenant_id, status, outcome, version,
                              started_at, last_activity_at, closed_at
                    """
                ),
                {"session_id": session_id, "tenant_id": tenant_id},
            )
            return _conversation(result.one())

    async def get(self, tenant_id: str, session_id: uuid.UUID) -> ConversationRecord:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, status, outcome, version,
                           started_at, last_activity_at, closed_at
                    FROM chat_sessions
                    WHERE tenant_id = :tenant_id AND id = :session_id
                    """
                ),
                {"tenant_id": tenant_id, "session_id": session_id},
            )
            row = result.one_or_none()
        if row is None:
            raise NotFoundError(detail="conversation absent or outside tenant")
        return _conversation(row)

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
        message_id = uuid.uuid4()
        insert_message = text(
            """
            INSERT INTO messages
                (id, tenant_id, chat_session_id, sequence_number, role,
                 content, model_name, metadata)
            VALUES
                (:message_id, :tenant_id, :session_id, :sequence_number, :role,
                 :content, :model_name, :metadata)
            RETURNING id, tenant_id, chat_session_id, sequence_number, role,
                      content, model_name, metadata, created_at
            """
        ).bindparams(bindparam("metadata", type_=JSONB))

        async with self._engine.begin() as connection:
            locked = await connection.execute(
                text(
                    """
                    SELECT version FROM chat_sessions
                    WHERE tenant_id = :tenant_id AND id = :session_id
                    FOR UPDATE
                    """
                ),
                {"tenant_id": tenant_id, "session_id": session_id},
            )
            version = locked.scalar_one_or_none()
            if version is None:
                raise NotFoundError(detail="conversation absent or outside tenant")

            inserted = await connection.execute(
                insert_message,
                {
                    "message_id": message_id,
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "sequence_number": version,
                    "role": role.value,
                    "content": content,
                    "model_name": model_name,
                    "metadata": dict(metadata or {}),
                },
            )
            updated = await connection.execute(
                text(
                    """
                    UPDATE chat_sessions
                    SET version = version + 1, last_activity_at = now()
                    WHERE tenant_id = :tenant_id AND id = :session_id
                      AND version = :version
                    """
                ),
                {"tenant_id": tenant_id, "session_id": session_id, "version": version},
            )
            if updated.rowcount != 1:
                raise ConflictError(detail="conversation version changed while row was locked")
            return _message(inserted.one())

    async def transcript(self, tenant_id: str, session_id: uuid.UUID) -> tuple[MessageRecord, ...]:
        async with self._engine.begin() as connection:
            exists = await connection.execute(
                text(
                    """
                    SELECT id FROM chat_sessions
                    WHERE tenant_id = :tenant_id AND id = :session_id
                    """
                ),
                {"tenant_id": tenant_id, "session_id": session_id},
            )
            if exists.first() is None:
                raise NotFoundError(detail="conversation absent or outside tenant")
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, chat_session_id, sequence_number, role,
                           content, model_name, metadata, created_at
                    FROM messages
                    WHERE tenant_id = :tenant_id AND chat_session_id = :session_id
                    ORDER BY sequence_number
                    """
                ),
                {"tenant_id": tenant_id, "session_id": session_id},
            )
            return tuple(_message(row) for row in result.all())

    async def for_tenant(self, tenant_id: str, *, limit: int) -> tuple[ConversationRecord, ...]:
        """Conversations with at least one message, most recently active first.

        The ``EXISTS`` filter is what keeps the operator console readable: the
        same table holds the write-only rows a booking or a lead correlates
        against, and those carry no transcript.
        """
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, status, outcome, version,
                           started_at, last_activity_at, closed_at
                    FROM chat_sessions
                    WHERE tenant_id = :tenant_id
                      AND EXISTS (
                          SELECT 1 FROM messages
                          WHERE messages.tenant_id = chat_sessions.tenant_id
                            AND messages.chat_session_id = chat_sessions.id
                      )
                    ORDER BY last_activity_at DESC, id
                    LIMIT :limit
                    """
                ),
                {"tenant_id": tenant_id, "limit": limit},
            )
            return tuple(_conversation(row) for row in result.all())


class PostgresLeadStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, command: LeadCommand, *, session_id: str) -> LeadRecord:
        lead_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            authoritative_session_id = await _action_session(
                connection, command.tenant_id, session_id
            )
            result = await connection.execute(
                text(
                    """
                    INSERT INTO leads
                        (id, tenant_id, chat_session_id, urgency, customer_name,
                         contact_kind, contact_value, service_slug, service_label,
                         summary, address_or_zip)
                    VALUES
                        (:id, :tenant_id, :session_id, :urgency, :customer_name,
                         :contact_kind, :contact_value, :service_slug, :service_label,
                         :summary, :address_or_zip)
                    RETURNING created_at
                    """
                ),
                {
                    "id": lead_id,
                    "tenant_id": command.tenant_id,
                    "session_id": authoritative_session_id,
                    "urgency": command.urgency.value,
                    "customer_name": command.customer_name,
                    "contact_kind": command.contact.kind.value,
                    "contact_value": command.contact.value,
                    "service_slug": command.service_slug,
                    "service_label": command.service,
                    "summary": command.summary,
                    "address_or_zip": command.address_or_zip or None,
                },
            )
            created_at = result.scalar_one()
            await _advance_action_session(
                connection, command.tenant_id, authoritative_session_id, "lead"
            )
        return LeadRecord(
            lead_id=f"LD-{lead_id.hex.upper()}",
            tenant_id=command.tenant_id,
            session_id=str(authoritative_session_id),
            customer_name=command.customer_name,
            contact=command.contact,
            service=command.service,
            service_slug=command.service_slug,
            summary=command.summary,
            address_or_zip=command.address_or_zip,
            urgency=command.urgency,
            created_at=created_at,
        )

    async def for_tenant(self, tenant_id: str) -> tuple[LeadRecord, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, chat_session_id, customer_name,
                           contact_value, service_label, service_slug, summary,
                           address_or_zip, urgency, created_at
                    FROM leads
                    WHERE tenant_id = :tenant_id
                    ORDER BY created_at, id
                    """
                ),
                {"tenant_id": tenant_id},
            )
            rows = result.all()
        return tuple(
            LeadRecord(
                lead_id=f"LD-{row.id.hex.upper()}",
                tenant_id=row.tenant_id,
                session_id=str(row.chat_session_id),
                customer_name=row.customer_name,
                contact=Contact.parse(row.contact_value),
                service=row.service_label,
                service_slug=row.service_slug,
                summary=row.summary,
                address_or_zip=row.address_or_zip or "",
                urgency=_lead_urgency(row.urgency),
                created_at=row.created_at,
            )
            for row in rows
        )


class PostgresHandoffStore:
    """Durably records requests for a person to take a conversation over.

    Writing the handoff also moves the conversation to ``waiting_for_staff``, in
    the same transaction: a queued handoff whose session still reads ``active``
    is a conversation the assistant would keep answering while a staff member
    believes they own it.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, command: HandoffCommand, *, session_id: str) -> HandoffRecord:
        handoff_id = uuid.uuid4()
        async with self._engine.begin() as connection:
            authoritative_session_id = await _action_session(
                connection, command.tenant_id, session_id
            )
            result = await connection.execute(
                text(
                    """
                    INSERT INTO handoffs
                        (id, tenant_id, chat_session_id, status, reason, summary)
                    VALUES
                        (:id, :tenant_id, :session_id, 'requested', :reason, :summary)
                    RETURNING requested_at
                    """
                ),
                {
                    "id": handoff_id,
                    "tenant_id": command.tenant_id,
                    "session_id": authoritative_session_id,
                    "reason": command.reason.value,
                    "summary": command.summary,
                },
            )
            requested_at = result.scalar_one()
            await _advance_action_session(
                connection, command.tenant_id, authoritative_session_id, "handoff"
            )
            await connection.execute(
                text(
                    """
                    UPDATE chat_sessions SET status = 'waiting_for_staff'
                    WHERE tenant_id = :tenant_id AND id = :session_id AND status = 'active'
                    """
                ),
                {"tenant_id": command.tenant_id, "session_id": authoritative_session_id},
            )
        return HandoffRecord(
            handoff_id=f"HO-{handoff_id.hex.upper()}",
            tenant_id=command.tenant_id,
            session_id=str(authoritative_session_id),
            reason=command.reason,
            summary=command.summary,
            created_at=requested_at,
        )

    async def for_tenant(self, tenant_id: str) -> tuple[HandoffRecord, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, chat_session_id, reason, summary, requested_at
                    FROM handoffs
                    WHERE tenant_id = :tenant_id
                    ORDER BY requested_at, id
                    """
                ),
                {"tenant_id": tenant_id},
            )
            rows = result.all()
        return tuple(
            HandoffRecord(
                handoff_id=f"HO-{row.id.hex.upper()}",
                tenant_id=row.tenant_id,
                session_id=str(row.chat_session_id),
                reason=HandoffReason.parse(row.reason),
                summary=row.summary or "",
                created_at=row.requested_at,
            )
            for row in rows
        )


class PostgresBookingStore:
    """Durably records current static-slot bookings; DATA-003 reserves real slots."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, command: BookingCommand, *, session_id: str) -> BookingRecord:
        booking_uuid = uuid.uuid4()
        reference = f"BK-{booking_uuid.hex.upper()}"
        async with self._engine.begin() as connection:
            authoritative_session_id = await _action_session(
                connection, command.tenant_id, session_id
            )
            result = await connection.execute(
                text(
                    """
                    INSERT INTO bookings
                        (id, tenant_id, chat_session_id, status, reference,
                         service_slug, service_label, provider_name, slot_label,
                         customer_name, contact_kind, contact_value, service_address,
                         confirmed_at)
                    VALUES
                        (:id, :tenant_id, :session_id, 'confirmed', :reference,
                         :service_slug, :service_label, 'prototype-static-availability',
                         :slot_label, :customer_name, :contact_kind, :contact_value,
                         :service_address, now())
                    RETURNING created_at
                    """
                ),
                {
                    "id": booking_uuid,
                    "tenant_id": command.tenant_id,
                    "session_id": authoritative_session_id,
                    "reference": reference,
                    "service_slug": command.service.slug,
                    "service_label": command.service.display_name,
                    "slot_label": command.slot,
                    "customer_name": command.customer_name,
                    "contact_kind": command.contact.kind.value,
                    "contact_value": command.contact.value,
                    "service_address": command.address,
                },
            )
            created_at = result.scalar_one()
            await _advance_action_session(
                connection, command.tenant_id, authoritative_session_id, "booked"
            )
        return BookingRecord(
            booking_id=reference,
            tenant_id=command.tenant_id,
            session_id=str(authoritative_session_id),
            customer_name=command.customer_name,
            contact=command.contact,
            address=command.address,
            service_slug=command.service.slug,
            service_name=command.service.display_name,
            slot=command.slot,
            created_at=created_at,
        )

    async def for_tenant(self, tenant_id: str) -> tuple[BookingRecord, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT reference, tenant_id, chat_session_id, customer_name,
                           contact_value, service_address, service_slug,
                           service_label, slot_label, created_at
                    FROM bookings
                    WHERE tenant_id = :tenant_id
                    ORDER BY created_at, id
                    """
                ),
                {"tenant_id": tenant_id},
            )
            rows = result.all()
        return tuple(
            BookingRecord(
                booking_id=row.reference,
                tenant_id=row.tenant_id,
                session_id=str(row.chat_session_id),
                customer_name=row.customer_name,
                contact=Contact.parse(row.contact_value),
                address=row.service_address,
                service_slug=row.service_slug,
                service_name=row.service_label,
                slot=row.slot_label,
                created_at=row.created_at,
            )
            for row in rows
        )


class PostgresMembershipStore:
    """Per-tenant operator role rows (SEC-001).

    The upsert is the assignment operation: the composite primary key means
    re-assigning a role to the same operator in the same tenant can never
    produce a second row.
    """

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def role_for(self, tenant_id: str, subject: str) -> str | None:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT tenant_id, principal_subject, role, created_at, updated_at
                    FROM tenant_memberships
                    WHERE tenant_id = :tenant_id AND principal_subject = :subject
                    """
                ),
                {"tenant_id": tenant_id, "subject": subject},
            )
            row = result.one_or_none()
        return None if row is None else row.role

    async def assign(self, *, tenant_id: str, subject: str, role: str) -> TenantMembership:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    INSERT INTO tenant_memberships (tenant_id, principal_subject, role)
                    VALUES (:tenant_id, :subject, :role)
                    ON CONFLICT (tenant_id, principal_subject)
                    DO UPDATE SET role = :role, updated_at = now()
                    RETURNING tenant_id, principal_subject, role, created_at, updated_at
                    """
                ),
                {"tenant_id": tenant_id, "subject": subject, "role": role},
            )
            return _membership(result.one())

    async def revoke(self, *, tenant_id: str, subject: str) -> bool:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    DELETE FROM tenant_memberships
                    WHERE tenant_id = :tenant_id AND principal_subject = :subject
                    """
                ),
                {"tenant_id": tenant_id, "subject": subject},
            )
            return result.rowcount == 1

    async def for_principal(self, subject: str) -> tuple[TenantMembership, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT tenant_id, principal_subject, role, created_at, updated_at
                    FROM tenant_memberships
                    WHERE principal_subject = :subject
                    ORDER BY tenant_id
                    """
                ),
                {"subject": subject},
            )
            return tuple(_membership(row) for row in result.all())


class PostgresAuditStore:
    """Append-only accountability records; UPDATE and DELETE are revoked from
    the application role, so this store can only grow rows, never edit them."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(self, event: AuditEvent) -> AuditEvent:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    INSERT INTO audit_events
                        (tenant_id, actor_type, principal_id, action,
                         resource_type, resource_id, request_id, details)
                    VALUES
                        (:tenant_id, :actor_type, :principal_id, :action,
                         :resource_type, :resource_id, :request_id, :details)
                    RETURNING occurred_at
                    """
                ).bindparams(bindparam("details", type_=JSONB)),
                {
                    "tenant_id": event.tenant_id,
                    "actor_type": event.actor_type.value,
                    "principal_id": event.principal_id,
                    "action": event.action,
                    "resource_type": event.resource_type,
                    "resource_id": event.resource_id,
                    "request_id": event.request_id,
                    "details": dict(event.details),
                },
            )
            occurred_at = result.scalar_one()
        return replace(event, occurred_at=occurred_at)

    async def for_tenant(self, tenant_id: str, *, limit: int = 200) -> tuple[AuditEvent, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT tenant_id, actor_type, principal_id, action,
                           resource_type, resource_id, request_id, details, occurred_at
                    FROM audit_events
                    WHERE tenant_id = :tenant_id
                    ORDER BY occurred_at DESC, id DESC
                    LIMIT :limit
                    """
                ),
                {"tenant_id": tenant_id, "limit": limit},
            )
            return tuple(_audit_event(row) for row in result.all())
