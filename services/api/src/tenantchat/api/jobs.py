"""Durable, tenant-scoped background-job contracts and an explicit test fake.

PostgreSQL is the production system of record (see ``persistence.jobs``).  The
in-memory implementation exists only for HTTP and worker unit tests; it mirrors
the state machine so those tests do not need a database for every error branch.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Protocol

from tenantchat.core.errors import ConflictError, NotFoundError

_SAFE_ERROR_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,99}$")


class JobKind(StrEnum):
    """Foundation-owned job classes; dependent tasks supply most handlers."""

    INGESTION = "ingestion"
    CRM_DELIVERY = "crm_delivery"
    NOTIFICATION = "notification"
    PRIVACY_DELETION = "privacy_deletion"
    WEBHOOK = "webhook"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD_LETTERED = "dead_lettered"
    CANCELLED = "cancelled"


class JobEventType(StrEnum):
    ENQUEUED = "enqueued"
    LEASED = "leased"
    LEASE_RENEWED = "lease_renewed"
    RETRY_SCHEDULED = "retry_scheduled"
    SUCCEEDED = "succeeded"
    DEAD_LETTERED = "dead_lettered"
    OPERATOR_RETRIED = "operator_retried"
    OPERATOR_CANCELLED = "operator_cancelled"


@dataclass(frozen=True, slots=True)
class JobRecord:
    job_id: uuid.UUID
    tenant_id: str
    kind: JobKind
    payload: dict[str, object]
    idempotency_key: str
    status: JobStatus
    attempt_count: int
    max_attempts: int
    replay_count: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class JobEvent:
    event_id: int
    job_id: uuid.UUID
    tenant_id: str
    event: JobEventType
    actor_type: str
    actor_id: str | None
    request_id: str | None
    details: dict[str, object]
    occurred_at: datetime


def payload_fingerprint(payload: Mapping[str, object]) -> str:
    """Stable hash used to reject an idempotency key reused for new work."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def validate_error_code(error_code: str) -> str:
    """Return a bounded, non-PII error code or reject unsafe handler output."""
    if not _SAFE_ERROR_CODE.fullmatch(error_code):
        raise ValueError("job error code must be a bounded lowercase identifier")
    return error_code


def retry_delay(attempt_count: int, base: timedelta, cap: timedelta) -> timedelta:
    """Capped exponential backoff without overflowing at high attempt limits."""
    exponent = min(max(attempt_count - 1, 0), 63)
    seconds = min(base.total_seconds() * (2**exponent), cap.total_seconds())
    return timedelta(seconds=seconds)


class JobStore(Protocol):
    async def enqueue(
        self,
        tenant_id: str,
        *,
        kind: JobKind,
        payload: Mapping[str, object],
        idempotency_key: str,
        max_attempts: int = 5,
        available_at: datetime | None = None,
    ) -> JobRecord: ...

    async def lease(
        self,
        *,
        worker_id: str,
        limit: int,
        lease_for: timedelta,
    ) -> tuple[JobRecord, ...]: ...

    async def renew(
        self, job_id: uuid.UUID, *, worker_id: str, lease_for: timedelta
    ) -> JobRecord: ...

    async def succeed(self, job_id: uuid.UUID, *, worker_id: str) -> JobRecord: ...

    async def fail(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        error_code: str,
        retryable: bool,
        backoff_base: timedelta,
        backoff_cap: timedelta,
    ) -> JobRecord: ...

    async def for_tenant(
        self, tenant_id: str, *, status: JobStatus | None = None, limit: int = 100
    ) -> tuple[JobRecord, ...]: ...

    async def get(self, tenant_id: str, job_id: uuid.UUID) -> JobRecord: ...

    async def events(self, tenant_id: str, job_id: uuid.UUID) -> tuple[JobEvent, ...]: ...

    async def retry_dead_letter(
        self,
        tenant_id: str,
        job_id: uuid.UUID,
        *,
        actor_id: str,
        request_id: str,
    ) -> JobRecord: ...

    async def cancel(
        self,
        tenant_id: str,
        job_id: uuid.UUID,
        *,
        actor_id: str,
        request_id: str,
    ) -> JobRecord: ...


class InMemoryJobStore:
    """State-machine fake. Never use this for a deployed worker."""

    def __init__(self) -> None:
        self._jobs: dict[uuid.UUID, JobRecord] = {}
        self._dedupe: dict[tuple[str, JobKind, str], tuple[uuid.UUID, str]] = {}
        self._events: list[JobEvent] = []
        self._lock = asyncio.Lock()

    def _event(
        self,
        job: JobRecord,
        event: JobEventType,
        *,
        actor_type: str,
        actor_id: str | None = None,
        request_id: str | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        self._events.append(
            JobEvent(
                event_id=len(self._events) + 1,
                job_id=job.job_id,
                tenant_id=job.tenant_id,
                event=event,
                actor_type=actor_type,
                actor_id=actor_id,
                request_id=request_id,
                details=dict(details or {}),
                occurred_at=datetime.now(UTC),
            )
        )

    async def enqueue(
        self,
        tenant_id: str,
        *,
        kind: JobKind,
        payload: Mapping[str, object],
        idempotency_key: str,
        max_attempts: int = 5,
        available_at: datetime | None = None,
    ) -> JobRecord:
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("job idempotency key must be between 1 and 200 characters")
        if not 1 <= max_attempts <= 100:
            raise ValueError("job max attempts must be between 1 and 100")
        fingerprint = payload_fingerprint(payload)
        key = (tenant_id, kind, idempotency_key)
        async with self._lock:
            duplicate = self._dedupe.get(key)
            if duplicate is not None:
                if duplicate[1] != fingerprint:
                    raise ConflictError(detail="job idempotency key was reused for different work")
                return self._jobs[duplicate[0]]
            now = datetime.now(UTC)
            job = JobRecord(
                job_id=uuid.uuid4(),
                tenant_id=tenant_id,
                kind=kind,
                payload=dict(payload),
                idempotency_key=idempotency_key,
                status=JobStatus.PENDING,
                attempt_count=0,
                max_attempts=max_attempts,
                replay_count=0,
                available_at=available_at or now,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            self._jobs[job.job_id] = job
            self._dedupe[key] = (job.job_id, fingerprint)
            self._event(job, JobEventType.ENQUEUED, actor_type="service")
            return job

    async def lease(
        self, *, worker_id: str, limit: int, lease_for: timedelta
    ) -> tuple[JobRecord, ...]:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker id must be between 1 and 200 characters")
        if not 1 <= limit <= 100:
            raise ValueError("job lease limit must be between 1 and 100")
        if lease_for <= timedelta(0):
            raise ValueError("job lease duration must be positive")
        now = datetime.now(UTC)
        leased: list[JobRecord] = []
        async with self._lock:
            candidates = sorted(
                self._jobs.values(), key=lambda item: (item.available_at, item.created_at)
            )
            for current in candidates:
                eligible = current.status is JobStatus.PENDING and current.available_at <= now
                expired = (
                    current.status is JobStatus.RUNNING
                    and current.lease_expires_at is not None
                    and current.lease_expires_at <= now
                )
                if not (eligible or expired):
                    continue
                job = replace(
                    current,
                    status=JobStatus.RUNNING,
                    attempt_count=current.attempt_count + 1,
                    lease_owner=worker_id,
                    lease_expires_at=now + lease_for,
                    updated_at=now,
                )
                self._jobs[job.job_id] = job
                self._event(job, JobEventType.LEASED, actor_type="worker")
                leased.append(job)
                if len(leased) == limit:
                    break
        return tuple(leased)

    def _owned(self, job_id: uuid.UUID, worker_id: str) -> JobRecord:
        job = self._jobs.get(job_id)
        if job is None or job.status is not JobStatus.RUNNING or job.lease_owner != worker_id:
            raise ConflictError(detail="job lease is absent or owned by another worker")
        return job

    async def renew(self, job_id: uuid.UUID, *, worker_id: str, lease_for: timedelta) -> JobRecord:
        if lease_for <= timedelta(0):
            raise ValueError("job lease duration must be positive")
        async with self._lock:
            current = self._owned(job_id, worker_id)
            now = datetime.now(UTC)
            job = replace(current, lease_expires_at=now + lease_for, updated_at=now)
            self._jobs[job_id] = job
            self._event(job, JobEventType.LEASE_RENEWED, actor_type="worker")
            return job

    async def succeed(self, job_id: uuid.UUID, *, worker_id: str) -> JobRecord:
        async with self._lock:
            current = self._owned(job_id, worker_id)
            now = datetime.now(UTC)
            job = replace(
                current,
                status=JobStatus.SUCCEEDED,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=None,
                updated_at=now,
                completed_at=now,
            )
            self._jobs[job_id] = job
            self._event(job, JobEventType.SUCCEEDED, actor_type="worker")
            return job

    async def fail(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        error_code: str,
        retryable: bool,
        backoff_base: timedelta,
        backoff_cap: timedelta,
    ) -> JobRecord:
        validate_error_code(error_code)
        if backoff_base <= timedelta(0) or backoff_cap < backoff_base:
            raise ValueError("job backoff bounds are invalid")
        async with self._lock:
            current = self._owned(job_id, worker_id)
            now = datetime.now(UTC)
            dead = not retryable or current.attempt_count >= current.max_attempts
            delay = retry_delay(current.attempt_count, backoff_base, backoff_cap)
            job = replace(
                current,
                status=JobStatus.DEAD_LETTERED if dead else JobStatus.PENDING,
                available_at=now if dead else now + delay,
                lease_owner=None,
                lease_expires_at=None,
                last_error_code=error_code,
                updated_at=now,
                completed_at=now if dead else None,
            )
            self._jobs[job_id] = job
            self._event(
                job,
                JobEventType.DEAD_LETTERED if dead else JobEventType.RETRY_SCHEDULED,
                actor_type="worker",
                details={"error_code": error_code, "attempt": job.attempt_count},
            )
            return job

    async def for_tenant(
        self, tenant_id: str, *, status: JobStatus | None = None, limit: int = 100
    ) -> tuple[JobRecord, ...]:
        async with self._lock:
            jobs = [
                job
                for job in self._jobs.values()
                if job.tenant_id == tenant_id and (status is None or job.status is status)
            ]
        jobs.sort(key=lambda item: (item.created_at, str(item.job_id)), reverse=True)
        return tuple(jobs[:limit])

    async def get(self, tenant_id: str, job_id: uuid.UUID) -> JobRecord:
        async with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.tenant_id != tenant_id:
            raise NotFoundError(detail="job absent or outside tenant")
        return job

    async def events(self, tenant_id: str, job_id: uuid.UUID) -> tuple[JobEvent, ...]:
        await self.get(tenant_id, job_id)
        async with self._lock:
            return tuple(
                event
                for event in self._events
                if event.tenant_id == tenant_id and event.job_id == job_id
            )

    async def retry_dead_letter(
        self,
        tenant_id: str,
        job_id: uuid.UUID,
        *,
        actor_id: str,
        request_id: str,
    ) -> JobRecord:
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is None or current.tenant_id != tenant_id:
                raise NotFoundError(detail="job absent or outside tenant")
            if current.status is not JobStatus.DEAD_LETTERED:
                raise ConflictError(detail="only dead-lettered jobs can be retried")
            now = datetime.now(UTC)
            job = replace(
                current,
                status=JobStatus.PENDING,
                attempt_count=0,
                replay_count=current.replay_count + 1,
                available_at=now,
                last_error_code=None,
                completed_at=None,
                updated_at=now,
            )
            self._jobs[job_id] = job
            self._event(
                job,
                JobEventType.OPERATOR_RETRIED,
                actor_type="staff",
                actor_id=actor_id,
                request_id=request_id,
            )
            return job

    async def cancel(
        self,
        tenant_id: str,
        job_id: uuid.UUID,
        *,
        actor_id: str,
        request_id: str,
    ) -> JobRecord:
        async with self._lock:
            current = self._jobs.get(job_id)
            if current is None or current.tenant_id != tenant_id:
                raise NotFoundError(detail="job absent or outside tenant")
            if current.status not in {JobStatus.PENDING, JobStatus.DEAD_LETTERED}:
                raise ConflictError(detail="only pending or dead-lettered jobs can be cancelled")
            now = datetime.now(UTC)
            job = replace(
                current,
                status=JobStatus.CANCELLED,
                lease_owner=None,
                lease_expires_at=None,
                completed_at=now,
                updated_at=now,
            )
            self._jobs[job_id] = job
            self._event(
                job,
                JobEventType.OPERATOR_CANCELLED,
                actor_type="staff",
                actor_id=actor_id,
                request_id=request_id,
            )
            return job
