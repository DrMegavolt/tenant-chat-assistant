"""PostgreSQL adapters for consent, export, erasure, retention, and audit.

The consent store runs under the application role like every other adapter.
Erasure and retention purge deliberately do not: ``provision_app_role.sql``
grants the app role no ``DELETE`` on sessions or transcripts, so the API cannot
cause them. The privacy store is built over the erasure role's engine
(``PRIVACY_DATABASE_URL``) and only ever runs from the worker, while the
export path — read-only — shares the application engine.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Final

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncEngine

from tenantchat.api.correlation import trace_id as current_trace_id
from tenantchat.api.jobs import JobKind, payload_fingerprint
from tenantchat.api.persistence.tenancy import require_active_tenant
from tenantchat.api.store import (
    BookingRecord,
    ConsentRecord,
    ConversationRecord,
    ErasureReport,
    HandoffRecord,
    LeadRecord,
    MessageRecord,
    MessageRole,
    PrivacyRequestRecord,
    PurgeReport,
    SubjectRecords,
)
from tenantchat.core.commands import HandoffReason, LeadUrgency
from tenantchat.core.contact import Contact, ContactKind
from tenantchat.core.privacy import (
    ConsentGrant,
    ConsentPurpose,
    ConsentStatus,
    DataClass,
    RetentionPolicy,
)

CHECKPOINT_TABLES: Final = ("checkpoints", "checkpoint_blobs", "checkpoint_writes")


def _consent_purpose(value: str) -> ConsentPurpose:
    return ConsentPurpose(value)


def _consent_record(row: object) -> ConsentRecord:
    mapping = row._mapping  # type: ignore[attr-defined]
    return ConsentRecord(
        record_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        session_id=str(mapping["chat_session_id"]),
        purpose=_consent_purpose(mapping["purpose"]),
        status=ConsentStatus(mapping["status"]),
        statement=mapping["statement"],
        granted_at=mapping["granted_at"],
        withdrawn_at=mapping["withdrawn_at"],
    )


def _privacy_request(row: object) -> PrivacyRequestRecord:
    mapping = row._mapping  # type: ignore[attr-defined]
    return PrivacyRequestRecord(
        request_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        status=mapping["status"],
        contact_kind=mapping["contact_kind"],
        contact_value=mapping["contact_value"],
        requested_by=mapping["requested_by"],
        requested_at=mapping["requested_at"],
        processed_at=mapping["processed_at"],
    )


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


def _lead(row: object) -> LeadRecord:
    mapping = row._mapping  # type: ignore[attr-defined]
    return LeadRecord(
        lead_id=f"LD-{mapping['id'].hex.upper()}",
        tenant_id=mapping["tenant_id"],
        session_id=str(mapping["chat_session_id"]),
        customer_name=mapping["customer_name"],
        contact=Contact.parse(mapping["contact_value"]),
        service=mapping["service_label"],
        service_slug=mapping["service_slug"],
        summary=mapping["summary"],
        address_or_zip=mapping["address_or_zip"] or "",
        urgency=LeadUrgency.parse(mapping["urgency"]),
        created_at=mapping["created_at"],
    )


def _booking(row: object) -> BookingRecord:
    mapping = row._mapping  # type: ignore[attr-defined]
    return BookingRecord(
        booking_id=mapping["reference"],
        tenant_id=mapping["tenant_id"],
        session_id=str(mapping["chat_session_id"]),
        customer_name=mapping["customer_name"],
        contact=Contact.parse(mapping["contact_value"]),
        address=mapping["service_address"],
        service_slug=mapping["service_slug"],
        service_name=mapping["service_label"],
        slot=mapping["slot_label"],
        slot_id=str(mapping["slot_id"]),
        slot_start=mapping["slot_start"],
        slot_end=mapping["slot_end"],
        created_at=mapping["created_at"],
    )


def _handoff(row: object) -> HandoffRecord:
    mapping = row._mapping  # type: ignore[attr-defined]
    return HandoffRecord(
        handoff_id=f"HO-{mapping['id'].hex.upper()}",
        tenant_id=mapping["tenant_id"],
        session_id=str(mapping["chat_session_id"]),
        reason=HandoffReason.parse(mapping["reason"]),
        summary=mapping["summary"] or "",
        created_at=mapping["requested_at"],
    )


class PostgresConsentStore:
    """Consent grants, upserted per session and purpose."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def record(
        self,
        tenant_id: str,
        session_id: str,
        *,
        purposes: Collection[ConsentPurpose],
        statement: str,
    ) -> tuple[ConsentRecord, ...]:
        insert = text(
            """
            INSERT INTO consent_records
                (id, tenant_id, chat_session_id, purpose, status, statement)
            VALUES
                (:id, :tenant_id, :session_id, :purpose, 'granted', :statement)
            ON CONFLICT (tenant_id, chat_session_id, purpose)
            DO UPDATE SET status = 'granted', statement = EXCLUDED.statement,
                          granted_at = now(), withdrawn_at = NULL
            RETURNING id, tenant_id, chat_session_id, purpose, status,
                      statement, granted_at, withdrawn_at
            """
        )
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            recorded: list[ConsentRecord] = []
            for purpose in purposes:
                result = await connection.execute(
                    insert,
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": tenant_id,
                        "session_id": session_id,
                        "purpose": purpose.value,
                        "statement": statement,
                    },
                )
                recorded.append(_consent_record(result.one()))
        return tuple(recorded)

    async def consent_grant(self, tenant_id: str, session_id: str) -> ConsentGrant:
        # A session that is not a UUID string cannot hold a consent row (the
        # column is a UUID foreign key), so it has no grant and the action is
        # refused. Checking here keeps a caller-supplied session label from
        # becoming a bind error on a path that only means "no consent".
        try:
            uuid.UUID(session_id)
        except ValueError:
            return ConsentGrant(
                tenant_id=tenant_id,
                session_id=session_id,
                purposes=frozenset(),
                statement="",
                granted_at=datetime.min.replace(tzinfo=UTC),
            )
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT purpose, statement, granted_at
                    FROM consent_records
                    WHERE tenant_id = :tenant_id
                      AND chat_session_id = :session_id
                      AND status = 'granted'
                    """
                ),
                {"tenant_id": tenant_id, "session_id": session_id},
            )
            rows = result.all()
        if not rows:
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
            purposes=frozenset(_consent_purpose(row.purpose) for row in rows),
            statement=rows[0].statement,
            granted_at=max(row.granted_at for row in rows),
        )

    async def for_session(self, tenant_id: str, session_id: str) -> tuple[ConsentRecord, ...]:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, chat_session_id, purpose, status,
                           statement, granted_at, withdrawn_at
                    FROM consent_records
                    WHERE tenant_id = :tenant_id AND chat_session_id = :session_id
                    ORDER BY purpose
                    """
                ),
                {"tenant_id": tenant_id, "session_id": session_id},
            )
            return tuple(_consent_record(row) for row in result.all())


class PostgresPrivacyStore:
    """Subject discovery, export assembly, and the two destructive workers.

    ``read_engine`` serves export and the deletion-request queue, which the
    API touches directly. ``erasure_engine`` serves only :meth:`erase_subject`
    and :meth:`purge_expired`, which the worker runs; its URL names the
    erasure role, the one role with ``DELETE`` on sessions and transcripts.
    A deployment without ``PRIVACY_DATABASE_URL`` builds the store with
    ``None`` — the API never calls the destructive operations, so it can
    serve export and the queue; the worker refuses to start without one.
    """

    def __init__(self, read_engine: AsyncEngine, erasure_engine: AsyncEngine | None) -> None:
        self._read = read_engine
        self._erasure = erasure_engine

    def _erasure_engine(self) -> AsyncEngine:
        if self._erasure is None:
            raise RuntimeError("PRIVACY_DATABASE_URL is not configured; erasure cannot run")
        return self._erasure

    @staticmethod
    def _session_ids_any(session_ids: Collection[uuid.UUID]) -> str:
        """Inline a parameter list as an array literal for ``= ANY(...)``.

        Kept in one place because every destructive query needs it, and a
        bind type mismatch here fails at runtime, never at lint time. The
        values are ``uuid.UUID`` objects, whose string form contains nothing
        an attacker can reach.
        """
        ids = ", ".join(f"'{session}'" for session in sorted(session_ids))
        return f"ARRAY[{ids}]::uuid[]"

    async def sessions_for_contact(self, tenant_id: str, contact: Contact) -> tuple[uuid.UUID, ...]:
        # The canonical phone form (+15552221919) never appears verbatim in a
        # message; compare the digits instead.
        if contact.kind is ContactKind.PHONE:
            probe = contact.value.removeprefix("+1")
            message_match = "regexp_replace(m.content, '[^0-9]', '', 'g') LIKE '%' || :probe || '%'"
        else:
            probe = contact.value.casefold()
            message_match = "m.content ILIKE '%' || :probe || '%'"
        async with self._read.begin() as connection:
            result = await connection.execute(
                text(
                    f"""
                    SELECT s.id
                    FROM chat_sessions s
                    WHERE s.tenant_id = :tenant_id
                      AND (
                          EXISTS (
                              SELECT 1 FROM messages m
                              WHERE m.tenant_id = s.tenant_id
                                AND m.chat_session_id = s.id
                                AND {message_match}
                          )
                          OR EXISTS (
                              SELECT 1 FROM leads l
                              WHERE l.tenant_id = s.tenant_id
                                AND l.chat_session_id = s.id
                                AND l.contact_value = :value
                          )
                          OR EXISTS (
                              SELECT 1 FROM bookings b
                              WHERE b.tenant_id = s.tenant_id
                                AND b.chat_session_id = s.id
                                AND b.contact_value = :value
                          )
                      )
                    ORDER BY s.last_activity_at DESC, s.id
                    """  # noqa: S608 - message_match is one of two module-built predicates
                ),
                {"tenant_id": tenant_id, "probe": probe, "value": contact.value},
            )
            return tuple(row.id for row in result.all())

    async def subject_records(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> SubjectRecords:
        ids = self._session_ids_any(session_ids)
        if not session_ids:
            return SubjectRecords((), (), (), (), (), ())
        async with self._read.begin() as connection:
            sessions = (
                await connection.execute(
                    text(
                        f"""
                        SELECT id, tenant_id, status, outcome, version,
                               started_at, last_activity_at, closed_at
                        FROM chat_sessions
                        WHERE tenant_id = :tenant_id AND id = ANY({ids})
                        ORDER BY last_activity_at DESC, id
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).all()
            messages = (
                await connection.execute(
                    text(
                        f"""
                        SELECT id, tenant_id, chat_session_id, sequence_number, role,
                               content, model_name, metadata, created_at
                        FROM messages
                        WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                        ORDER BY sequence_number
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).all()
            leads = (
                await connection.execute(
                    text(
                        f"""
                        SELECT id, tenant_id, chat_session_id, customer_name,
                               contact_value, service_label, service_slug, summary,
                               address_or_zip, urgency, created_at
                        FROM leads
                        WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                        ORDER BY created_at, id
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).all()
            bookings = (
                await connection.execute(
                    text(
                        f"""
                        SELECT reference, tenant_id, chat_session_id, customer_name,
                               contact_value, service_address, service_slug,
                               service_label, slot_label, slot_id, slot_start,
                               slot_end, created_at
                        FROM bookings
                        WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                        ORDER BY created_at, id
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).all()
            handoffs = (
                await connection.execute(
                    text(
                        f"""
                        SELECT id, tenant_id, chat_session_id, reason, summary,
                               requested_at
                        FROM handoffs
                        WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                        ORDER BY requested_at, id
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).all()
            consent = (
                await connection.execute(
                    text(
                        f"""
                        SELECT id, tenant_id, chat_session_id, purpose, status,
                               statement, granted_at, withdrawn_at
                        FROM consent_records
                        WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                        ORDER BY purpose
                        """
                    ),
                    {"tenant_id": tenant_id},
                )
            ).all()
        return SubjectRecords(
            sessions=tuple(_conversation(row) for row in sessions),
            messages=tuple(_message(row) for row in messages),
            leads=tuple(_lead(row) for row in leads),
            bookings=tuple(_booking(row) for row in bookings),
            handoffs=tuple(_handoff(row) for row in handoffs),
            consent=tuple(_consent_record(row) for row in consent),
        )

    async def erase_subject(
        self, tenant_id: str, session_ids: Collection[uuid.UUID]
    ) -> ErasureReport:
        """Remove every row for the sessions.

        Tool executions go first: their foreign key to ``messages`` is
        ``RESTRICT``, so a transcript row cannot be deleted under one. Leads,
        bookings, and handoffs keep their ``RESTRICT`` references to sessions
        from blocking the session delete at the end. All of it is one
        transaction, so a partial failure removes nothing.
        """
        if not session_ids:
            return ErasureReport(0, 0, 0, 0, 0, 0, 0)
        ids = self._session_ids_any(session_ids)
        threads = [f"{tenant_id}:{session}" for session in session_ids]
        async with self._erasure_engine().begin() as connection:
            await connection.execute(
                text(
                    f"""
                    DELETE FROM tool_executions
                    WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                    """  # noqa: S608 - ids are uuid.UUID literals, not caller text
                ),
                {"tenant_id": tenant_id},
            )
            message_rows = await connection.execute(
                text(
                    f"""
                    DELETE FROM messages
                    WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                    """  # noqa: S608 - ids are uuid.UUID literals, not caller text
                ),
                {"tenant_id": tenant_id},
            )
            lead_rows = await connection.execute(
                text(
                    f"""
                    DELETE FROM leads
                    WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                    """  # noqa: S608 - ids are uuid.UUID literals, not caller text
                ),
                {"tenant_id": tenant_id},
            )
            booking_rows = await connection.execute(
                text(
                    f"""
                    DELETE FROM bookings
                    WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                    """  # noqa: S608 - ids are uuid.UUID literals, not caller text
                ),
                {"tenant_id": tenant_id},
            )
            handoff_rows = await connection.execute(
                text(
                    f"""
                    DELETE FROM handoffs
                    WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                    """  # noqa: S608 - ids are uuid.UUID literals, not caller text
                ),
                {"tenant_id": tenant_id},
            )
            consent_rows = await connection.execute(
                text(
                    f"""
                    DELETE FROM consent_records
                    WHERE tenant_id = :tenant_id AND chat_session_id = ANY({ids})
                    """  # noqa: S608 - ids are uuid.UUID literals, not caller text
                ),
                {"tenant_id": tenant_id},
            )
            session_rows = await connection.execute(
                text(
                    f"""
                    DELETE FROM chat_sessions
                    WHERE tenant_id = :tenant_id AND id = ANY({ids})
                    """  # noqa: S608 - ids are uuid.UUID literals, not caller text
                ),
                {"tenant_id": tenant_id},
            )
            checkpoint_deleted = 0
            for table in CHECKPOINT_TABLES:
                removed = await connection.execute(
                    text(f"DELETE FROM {table} WHERE thread_id = ANY(:threads)"),  # noqa: S608 - CHECKPOINT_TABLES is a module constant
                    {"threads": threads},
                )
                checkpoint_deleted += removed.rowcount or 0
        return ErasureReport(
            sessions_deleted=session_rows.rowcount or 0,
            messages_deleted=message_rows.rowcount or 0,
            leads_anonymized=lead_rows.rowcount or 0,
            bookings_anonymized=booking_rows.rowcount or 0,
            handoffs_anonymized=handoff_rows.rowcount or 0,
            consent_records_deleted=consent_rows.rowcount or 0,
            checkpoints_deleted=checkpoint_deleted,
        )

    async def purge_expired(
        self, tenant_id: str, policy: RetentionPolicy, *, now: datetime
    ) -> PurgeReport:
        """Remove the tenant's expired transcripts, and the sessions left holding nothing.

        A session survives the purge while any of its records does — a booking
        outlives the transcript by design — so only shells with no leads,
        bookings, or handoffs are deleted. Consent is same-lifecycle as the
        transcript: it keeps the session company only as long as the session
        holds it, and is purged with the shell rather than keeping it alive
        forever. ``tool_rows`` is fetched before the transcript delete because
        its foreign key to ``messages`` is ``RESTRICT``.
        """
        transcript_age = policy.max_age(DataClass.TRANSCRIPT)
        if transcript_age is None:
            return PurgeReport(0, 0, 0, 0)
        cutoff = now - transcript_age
        async with self._erasure_engine().begin() as connection:
            candidates = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT chat_session_id FROM messages
                        WHERE tenant_id = :tenant_id AND created_at < :cutoff
                        UNION
                        SELECT chat_session_id FROM tool_executions
                        WHERE tenant_id = :tenant_id AND created_at < :cutoff
                        """
                        ),
                        {"tenant_id": tenant_id, "cutoff": cutoff},
                    )
                )
                .scalars()
                .all()
            )
            tool_rows = await connection.execute(
                text(
                    """
                    DELETE FROM tool_executions
                    WHERE tenant_id = :tenant_id AND created_at < :cutoff
                    """
                ),
                {"tenant_id": tenant_id, "cutoff": cutoff},
            )
            message_rows = await connection.execute(
                text(
                    """
                    DELETE FROM messages
                    WHERE tenant_id = :tenant_id AND created_at < :cutoff
                    """
                ),
                {"tenant_id": tenant_id, "cutoff": cutoff},
            )
            purged: tuple[uuid.UUID, ...] = ()
            consent_deleted = 0
            if candidates:
                ids = self._session_ids_any(candidates)
                # Find the shells first and count their consent records before
                # deleting the sessions, because the session delete cascades the
                # consent away and a report that then read 0 would be a lie.
                shells = (
                    await connection.execute(
                        text(
                            f"""
                        SELECT s.id, (
                            SELECT count(*) FROM consent_records c
                            WHERE c.tenant_id = s.tenant_id AND c.chat_session_id = s.id
                        ) AS consent_count
                        FROM chat_sessions s
                        WHERE s.tenant_id = :tenant_id
                          AND s.id = ANY({ids})
                          AND s.last_activity_at < :cutoff
                          AND NOT EXISTS (
                              SELECT 1 FROM messages m
                              WHERE m.tenant_id = s.tenant_id AND m.chat_session_id = s.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM tool_executions te
                              WHERE te.tenant_id = s.tenant_id AND te.chat_session_id = s.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM leads l
                              WHERE l.tenant_id = s.tenant_id AND l.chat_session_id = s.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM bookings b
                              WHERE b.tenant_id = s.tenant_id AND b.chat_session_id = s.id
                          )
                          AND NOT EXISTS (
                              SELECT 1 FROM handoffs h
                              WHERE h.tenant_id = s.tenant_id AND h.chat_session_id = s.id
                          )
                        """  # noqa: S608 - ids are uuid.UUID literals, not caller text
                        ),
                        {"tenant_id": tenant_id, "cutoff": cutoff},
                    )
                ).all()
                if shells:
                    purged = tuple(row.id for row in shells)
                    consent_deleted = int(sum(row.consent_count for row in shells))
                    purged_ids = self._session_ids_any(purged)
                    threads = [f"{tenant_id}:{session}" for session in purged]
                    for table in CHECKPOINT_TABLES:
                        await connection.execute(
                            text(f"DELETE FROM {table} WHERE thread_id = ANY(:threads)"),  # noqa: S608 - CHECKPOINT_TABLES is a module constant
                            {"threads": threads},
                        )
                    await connection.execute(
                        text(
                            f"""
                            DELETE FROM chat_sessions
                            WHERE tenant_id = :tenant_id AND id = ANY({purged_ids})
                            """  # noqa: S608 - purged_ids are uuid.UUID literals, not caller text
                        ),
                        {"tenant_id": tenant_id},
                    )
        return PurgeReport(
            sessions_deleted=len(purged),
            messages_deleted=message_rows.rowcount or 0,
            tool_executions_deleted=tool_rows.rowcount or 0,
            consent_records_deleted=consent_deleted,
        )

    async def create_privacy_request(
        self, tenant_id: str, *, contact: Contact, requested_by: str
    ) -> PrivacyRequestRecord:
        request_id = uuid.uuid4()
        job_id = uuid.uuid4()
        # The trace of the enqueuing request rides the payload (and is excluded
        # from the work fingerprint), so the worker logs under the same trace.
        job_payload = {
            "request_id": str(request_id),
            "trace_id": current_trace_id() or uuid.uuid4().hex,
        }
        async with self._read.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                text(
                    """
                    INSERT INTO privacy_requests
                        (id, tenant_id, contact_kind, contact_value, requested_by)
                    VALUES
                        (:id, :tenant_id, :kind, :value, :requested_by)
                    RETURNING id, tenant_id, status, contact_kind, contact_value,
                              requested_by, requested_at, processed_at
                    """
                ),
                {
                    "id": request_id,
                    "tenant_id": tenant_id,
                    "kind": contact.kind.value,
                    "value": contact.value,
                    "requested_by": requested_by,
                },
            )
            # The domain request and its delivery intent commit together. The
            # route repeats `enqueue` through the injected job port so its
            # in-memory test shape behaves identically; the unique key makes
            # that second production call a read of this same row.
            await connection.execute(
                text(
                    """
                    INSERT INTO background_jobs
                        (id, tenant_id, kind, payload, payload_hash, idempotency_key)
                    VALUES
                        (:job_id, :tenant_id, :kind,
                         :payload,
                         :payload_hash, :idempotency_key)
                    """
                ).bindparams(bindparam("payload", type_=JSONB)),
                {
                    "job_id": job_id,
                    "tenant_id": tenant_id,
                    "kind": JobKind.PRIVACY_DELETION.value,
                    "payload": job_payload,
                    "payload_hash": payload_fingerprint(job_payload),
                    "idempotency_key": str(request_id),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO background_job_events
                        (tenant_id, job_id, event, actor_type)
                    VALUES (:tenant_id, :job_id, 'enqueued', 'service')
                    """
                ),
                {"tenant_id": tenant_id, "job_id": job_id},
            )
            return _privacy_request(result.one())

    async def pending_privacy_requests(self) -> tuple[PrivacyRequestRecord, ...]:
        async with self._read.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, status, contact_kind, contact_value,
                           requested_by, requested_at, processed_at
                    FROM privacy_requests
                    WHERE status = 'pending'
                    ORDER BY requested_at, id
                    """
                )
            )
            return tuple(_privacy_request(row) for row in result.all())

    async def complete_privacy_request(
        self, request_id: uuid.UUID, *, processed_at: datetime
    ) -> None:
        # The contact value is anonymized in the same update that marks the
        # request done, so a completed queue is not a hoard of contact details.
        async with self._read.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE privacy_requests
                    SET status = 'completed', processed_at = :processed_at,
                        contact_value = :erased
                    WHERE id = :request_id AND status = 'pending'
                    """
                ),
                {
                    "request_id": request_id,
                    "processed_at": processed_at,
                    "erased": "erased",
                },
            )

    async def fail_privacy_request(self, request_id: uuid.UUID) -> None:
        async with self._read.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE privacy_requests
                    SET status = 'failed', processed_at = now()
                    WHERE id = :request_id AND status = 'pending'
                    """
                )
            )

    async def requests_for_tenant(self, tenant_id: str) -> tuple[PrivacyRequestRecord, ...]:
        async with self._read.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, status, contact_kind, contact_value,
                           requested_by, requested_at, processed_at
                    FROM privacy_requests
                    WHERE tenant_id = :tenant_id
                    ORDER BY requested_at DESC, id
                    """
                ),
                {"tenant_id": tenant_id},
            )
            return tuple(_privacy_request(row) for row in result.all())
