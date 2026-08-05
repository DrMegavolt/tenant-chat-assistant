"""The PRIV-001 erasure and retention worker.

Run on a schedule (a Kubernetes CronJob, typically daily):

    uv run --frozen python -m tenantchat.api.privacy_worker

One pass does two jobs. Deletion requests filed through the admin queue are
fulfilled: every record for the subject's sessions is removed and the request
is marked complete, with an audit row per request carrying the row counts.
Then retention expires: each tenant's transcripts past their policy's age are
purged, again with an audit row of counts per tenant.

Destructive operations run on the erasure role's engine
(``PRIVACY_DATABASE_URL``), the one role with ``DELETE`` on sessions and
transcripts. The process refuses to start without it: a worker that silently
skipped erasure would look like a working one.

``run_pass`` is the whole pass as a pure function of its inputs so a test can
drive it against in-memory stores and assert the audit events it writes.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from typing import Final

from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresAuditStore,
    PostgresPrivacyStore,
)
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    AuditActorType,
    AuditEvent,
    AuditStore,
    ErasureReport,
    PrivacyRequestRecord,
    PrivacyStore,
    PurgeReport,
)
from tenantchat.core.contact import Contact, ContactKind
from tenantchat.core.privacy import RetentionPolicy

_URL_VARIABLE: Final = "PRIVACY_DATABASE_URL"


async def process_deletion_request(
    request: PrivacyRequestRecord,
    store: PrivacyStore,
    audit: AuditStore,
    *,
    now: datetime,
) -> None:
    """Fulfil one idempotent deletion request and record its bounded outcome."""
    contact = Contact(kind=ContactKind(request.contact_kind), value=request.contact_value)
    sessions = await store.sessions_for_contact(request.tenant_id, contact)
    report = (
        await store.erase_subject(request.tenant_id, sessions)
        if sessions
        else ErasureReport(0, 0, 0, 0, 0, 0, 0, 0)
    )
    await store.complete_privacy_request(request.request_id, processed_at=now)
    await audit.record(
        AuditEvent(
            tenant_id=request.tenant_id,
            actor_type=AuditActorType.SERVICE,
            principal_id=None,
            action="privacy.erased",
            resource_type="privacy_request",
            resource_id=request.request_id,
            request_id=None,
            details={
                "sessions_deleted": report.sessions_deleted,
                "messages_deleted": report.messages_deleted,
                "leads_anonymized": report.leads_anonymized,
                "bookings_anonymized": report.bookings_anonymized,
                "handoffs_anonymized": report.handoffs_anonymized,
                "consent_records_deleted": report.consent_records_deleted,
                "checkpoints_deleted": report.checkpoints_deleted,
                "turn_records_deleted": report.turn_records_deleted,
            },
        )
    )


async def run_pass(
    registry: TenantRegistry,
    store: PrivacyStore,
    audit: AuditStore,
    *,
    now: datetime | None = None,
) -> int:
    """Fulfill every pending deletion request, then purge expired records.

    Returns the number of deletion requests completed, so a caller (or a test)
    can tell a pass that did nothing from one that erased something.
    """
    now = now or datetime.now(UTC)
    completed = 0
    for request in await store.pending_privacy_requests():
        await process_deletion_request(request, store, audit, now=now)
        completed += 1

    policy = RetentionPolicy.defaults()
    for tenant_id in registry.all():
        purge_report = await store.purge_expired(tenant_id, policy, now=now)
        if purge_report == PurgeReport(0, 0, 0, 0, 0):
            continue
        await audit.record(
            AuditEvent(
                tenant_id=tenant_id,
                actor_type=AuditActorType.SERVICE,
                principal_id=None,
                action="privacy.retention_purged",
                resource_type="retention",
                resource_id=None,
                request_id=None,
                details={
                    "sessions_deleted": purge_report.sessions_deleted,
                    "messages_deleted": purge_report.messages_deleted,
                    "tool_executions_deleted": purge_report.tool_executions_deleted,
                    "consent_records_deleted": purge_report.consent_records_deleted,
                    "turn_records_deleted": purge_report.turn_records_deleted,
                },
            )
        )
    return completed


async def _run_once(settings: Settings) -> int:
    if not settings.privacy_database_url:
        raise ValueError(f"{_URL_VARIABLE} is required; the worker must not run without it")
    if not settings.database_url:
        raise ValueError("DATABASE_URL is required; the worker needs the read role")
    from tenantchat.api.registry import TenantRegistry

    read = Database.connect(
        settings.database_url,
        DatabasePoolSettings(
            size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            timeout_seconds=settings.database_pool_timeout_seconds,
            recycle_seconds=settings.database_pool_recycle_seconds,
        ),
    )
    erasure = Database.connect(
        settings.privacy_database_url,
        DatabasePoolSettings(
            size=settings.privacy_database_pool_size,
            max_overflow=settings.privacy_database_max_overflow,
            timeout_seconds=settings.database_pool_timeout_seconds,
            recycle_seconds=settings.database_pool_recycle_seconds,
        ),
    )
    try:
        store = PostgresPrivacyStore(read.engine, erasure.engine)
        audit = PostgresAuditStore(read.engine)
        return await run_pass(TenantRegistry.seeded(), store, audit, now=datetime.now(UTC))
    finally:
        await read.dispose()
        await erasure.dispose()


def main() -> int:
    settings = Settings.from_environment()
    if not settings.database_url:
        sys.stderr.write("DATABASE_URL is required.\n")
        return 2
    if not settings.privacy_database_url:
        sys.stderr.write(f"{_URL_VARIABLE} is required.\n")
        return 2
    try:
        completed = asyncio.run(_run_once(settings))
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    sys.stdout.write(f"privacy pass complete: {completed} deletion request(s) fulfilled\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
