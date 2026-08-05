"""PostgreSQL durable-job repository with transactional leases and controls."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from tenantchat.api.jobs import (
    JobEvent,
    JobEventType,
    JobKind,
    JobRecord,
    JobStatus,
    payload_fingerprint,
    retry_delay,
    validate_error_code,
)
from tenantchat.api.persistence.tenancy import require_active_tenant
from tenantchat.core.errors import ConflictError, NotFoundError

_JOB_COLUMN_NAMES = (
    "id",
    "tenant_id",
    "kind",
    "payload",
    "idempotency_key",
    "status",
    "attempt_count",
    "max_attempts",
    "replay_count",
    "available_at",
    "lease_owner",
    "lease_expires_at",
    "last_error_code",
    "created_at",
    "updated_at",
    "completed_at",
)
_JOB_COLUMNS = ", ".join(_JOB_COLUMN_NAMES)
_QUALIFIED_JOB_COLUMNS = ", ".join(f"jobs.{column}" for column in _JOB_COLUMN_NAMES)


def _job(row: object) -> JobRecord:
    mapping = row._mapping  # type: ignore[attr-defined]
    return JobRecord(
        job_id=mapping["id"],
        tenant_id=mapping["tenant_id"],
        kind=JobKind(mapping["kind"]),
        payload=dict(mapping["payload"]),
        idempotency_key=mapping["idempotency_key"],
        status=JobStatus(mapping["status"]),
        attempt_count=mapping["attempt_count"],
        max_attempts=mapping["max_attempts"],
        replay_count=mapping["replay_count"],
        available_at=mapping["available_at"],
        lease_owner=mapping["lease_owner"],
        lease_expires_at=mapping["lease_expires_at"],
        last_error_code=mapping["last_error_code"],
        created_at=mapping["created_at"],
        updated_at=mapping["updated_at"],
        completed_at=mapping["completed_at"],
    )


def _job_event(row: object) -> JobEvent:
    mapping = row._mapping  # type: ignore[attr-defined]
    return JobEvent(
        event_id=mapping["id"],
        job_id=mapping["job_id"],
        tenant_id=mapping["tenant_id"],
        event=JobEventType(mapping["event"]),
        actor_type=mapping["actor_type"],
        actor_id=mapping["actor_id"],
        request_id=mapping["request_id"],
        details=dict(mapping["details"]),
        occurred_at=mapping["occurred_at"],
    )


async def _record_event(
    connection: AsyncConnection,
    job: JobRecord,
    event: JobEventType,
    *,
    actor_type: str,
    actor_id: str | None = None,
    request_id: str | None = None,
    details: Mapping[str, object] | None = None,
) -> None:
    statement = text(
        """
        INSERT INTO background_job_events
            (tenant_id, job_id, event, actor_type, actor_id, request_id, details)
        VALUES
            (:tenant_id, :job_id, :event, :actor_type, :actor_id, :request_id, :details)
        """
    ).bindparams(bindparam("details", type_=JSONB))
    await connection.execute(
        statement,
        {
            "tenant_id": job.tenant_id,
            "job_id": job.job_id,
            "event": event.value,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "request_id": request_id,
            "details": dict(details or {}),
        },
    )


class PostgresJobStore:
    """At-least-once delivery over leases; effects dedupe by idempotency key."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

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
        job_id = uuid.uuid4()
        fingerprint = payload_fingerprint(payload)
        statement = text(
            f"""
            INSERT INTO background_jobs
                (id, tenant_id, kind, payload, payload_hash, idempotency_key,
                 max_attempts, available_at)
            VALUES
                (:id, :tenant_id, :kind, :payload, :payload_hash,
                 :idempotency_key, :max_attempts, COALESCE(:available_at, now()))
            ON CONFLICT (tenant_id, kind, idempotency_key) DO NOTHING
            RETURNING {_JOB_COLUMNS}
            """
        ).bindparams(bindparam("payload", type_=JSONB))
        async with self._engine.begin() as connection:
            await require_active_tenant(connection, tenant_id)
            result = await connection.execute(
                statement,
                {
                    "id": job_id,
                    "tenant_id": tenant_id,
                    "kind": kind.value,
                    "payload": dict(payload),
                    "payload_hash": fingerprint,
                    "idempotency_key": idempotency_key,
                    "max_attempts": max_attempts,
                    "available_at": available_at,
                },
            )
            row = result.one_or_none()
            if row is not None:
                job = _job(row)
                await _record_event(connection, job, JobEventType.ENQUEUED, actor_type="service")
                return job

            duplicate = await connection.execute(
                text(
                    f"""
                    SELECT {_JOB_COLUMNS}, payload_hash
                    FROM background_jobs
                    WHERE tenant_id = :tenant_id AND kind = :kind
                      AND idempotency_key = :idempotency_key
                    """  # noqa: S608 - _JOB_COLUMNS is a module constant
                ),
                {
                    "tenant_id": tenant_id,
                    "kind": kind.value,
                    "idempotency_key": idempotency_key,
                },
            )
            existing = duplicate.one()
            if existing.payload_hash != fingerprint:
                raise ConflictError(detail="job idempotency key was reused for different work")
            return _job(existing)

    async def lease(
        self, *, worker_id: str, limit: int, lease_for: timedelta
    ) -> tuple[JobRecord, ...]:
        if not worker_id or len(worker_id) > 200:
            raise ValueError("worker id must be between 1 and 200 characters")
        if not 1 <= limit <= 100:
            raise ValueError("job lease limit must be between 1 and 100")
        if lease_for <= timedelta(0):
            raise ValueError("job lease duration must be positive")
        statement = text(
            f"""
            WITH candidates AS (
                SELECT id
                FROM background_jobs
                WHERE (status = 'pending' AND available_at <= now())
                   OR (status = 'running' AND lease_expires_at <= now())
                ORDER BY available_at, created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT :limit
            )
            UPDATE background_jobs AS jobs
            SET status = 'running', attempt_count = attempt_count + 1,
                lease_owner = :worker_id,
                lease_expires_at = now() + make_interval(secs => :lease_seconds),
                updated_at = now(), completed_at = NULL
            FROM candidates
            WHERE jobs.id = candidates.id
            RETURNING {_QUALIFIED_JOB_COLUMNS}
            """  # noqa: S608 - _JOB_COLUMNS is a module constant
        )
        async with self._engine.begin() as connection:
            result = await connection.execute(
                statement,
                {
                    "limit": limit,
                    "worker_id": worker_id,
                    "lease_seconds": lease_for.total_seconds(),
                },
            )
            jobs = tuple(_job(row) for row in result.all())
            for job in jobs:
                await _record_event(
                    connection,
                    job,
                    JobEventType.LEASED,
                    actor_type="worker",
                    details={"attempt": job.attempt_count},
                )
            return jobs

    async def _owned(
        self, connection: AsyncConnection, job_id: uuid.UUID, worker_id: str
    ) -> JobRecord:
        result = await connection.execute(
            text(
                f"""
                SELECT {_JOB_COLUMNS}
                FROM background_jobs
                WHERE id = :job_id AND status = 'running' AND lease_owner = :worker_id
                FOR UPDATE
                """  # noqa: S608 - _JOB_COLUMNS is a module constant
            ),
            {"job_id": job_id, "worker_id": worker_id},
        )
        row = result.one_or_none()
        if row is None:
            raise ConflictError(detail="job lease is absent or owned by another worker")
        return _job(row)

    async def renew(self, job_id: uuid.UUID, *, worker_id: str, lease_for: timedelta) -> JobRecord:
        if lease_for <= timedelta(0):
            raise ValueError("job lease duration must be positive")
        async with self._engine.begin() as connection:
            current = await self._owned(connection, job_id, worker_id)
            result = await connection.execute(
                text(
                    f"""
                    UPDATE background_jobs
                    SET lease_expires_at = now() + make_interval(secs => :lease_seconds),
                        updated_at = now()
                    WHERE id = :job_id
                    RETURNING {_JOB_COLUMNS}
                    """  # noqa: S608 - _JOB_COLUMNS is a module constant
                ),
                {"job_id": current.job_id, "lease_seconds": lease_for.total_seconds()},
            )
            job = _job(result.one())
            await _record_event(connection, job, JobEventType.LEASE_RENEWED, actor_type="worker")
            return job

    async def succeed(self, job_id: uuid.UUID, *, worker_id: str) -> JobRecord:
        async with self._engine.begin() as connection:
            current = await self._owned(connection, job_id, worker_id)
            result = await connection.execute(
                text(
                    f"""
                    UPDATE background_jobs
                    SET status = 'succeeded', lease_owner = NULL,
                        lease_expires_at = NULL, last_error_code = NULL,
                        updated_at = now(), completed_at = now()
                    WHERE id = :job_id
                    RETURNING {_JOB_COLUMNS}
                    """  # noqa: S608 - _JOB_COLUMNS is a module constant
                ),
                {"job_id": current.job_id},
            )
            job = _job(result.one())
            await _record_event(connection, job, JobEventType.SUCCEEDED, actor_type="worker")
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
        async with self._engine.begin() as connection:
            current = await self._owned(connection, job_id, worker_id)
            dead = not retryable or current.attempt_count >= current.max_attempts
            delay = retry_delay(current.attempt_count, backoff_base, backoff_cap)
            now = datetime.now(UTC)
            result = await connection.execute(
                text(
                    f"""
                    UPDATE background_jobs
                    SET status = :status, available_at = :available_at,
                        lease_owner = NULL, lease_expires_at = NULL,
                        last_error_code = :error_code, updated_at = :updated_at,
                        completed_at = :completed_at
                    WHERE id = :job_id
                    RETURNING {_JOB_COLUMNS}
                    """  # noqa: S608 - _JOB_COLUMNS is a module constant
                ),
                {
                    "job_id": current.job_id,
                    "status": (JobStatus.DEAD_LETTERED.value if dead else JobStatus.PENDING.value),
                    "available_at": now if dead else now + delay,
                    "error_code": error_code,
                    "updated_at": now,
                    "completed_at": now if dead else None,
                },
            )
            job = _job(result.one())
            await _record_event(
                connection,
                job,
                JobEventType.DEAD_LETTERED if dead else JobEventType.RETRY_SCHEDULED,
                actor_type="worker",
                details={"error_code": error_code, "attempt": job.attempt_count},
            )
            return job

    async def for_tenant(
        self, tenant_id: str, *, status: JobStatus | None = None, limit: int = 100
    ) -> tuple[JobRecord, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("job list limit must be between 1 and 200")
        predicate = "AND status = :status" if status is not None else ""
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_JOB_COLUMNS}
                    FROM background_jobs
                    WHERE tenant_id = :tenant_id {predicate}
                    ORDER BY created_at DESC, id DESC
                    LIMIT :limit
                    """  # noqa: S608 - predicate is a module-built constant
                ),
                {
                    "tenant_id": tenant_id,
                    "status": status.value if status is not None else None,
                    "limit": limit,
                },
            )
            return tuple(_job(row) for row in result.all())

    async def get(self, tenant_id: str, job_id: uuid.UUID) -> JobRecord:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    f"""
                    SELECT {_JOB_COLUMNS}
                    FROM background_jobs
                    WHERE tenant_id = :tenant_id AND id = :job_id
                    """  # noqa: S608 - _JOB_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id, "job_id": job_id},
            )
            row = result.one_or_none()
        if row is None:
            raise NotFoundError(detail="job absent or outside tenant")
        return _job(row)

    async def events(self, tenant_id: str, job_id: uuid.UUID) -> tuple[JobEvent, ...]:
        await self.get(tenant_id, job_id)
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    SELECT id, tenant_id, job_id, event, actor_type, actor_id,
                           request_id, details, occurred_at
                    FROM background_job_events
                    WHERE tenant_id = :tenant_id AND job_id = :job_id
                    ORDER BY id
                    """
                ),
                {"tenant_id": tenant_id, "job_id": job_id},
            )
            return tuple(_job_event(row) for row in result.all())

    async def retry_dead_letter(
        self,
        tenant_id: str,
        job_id: uuid.UUID,
        *,
        actor_id: str,
        request_id: str,
    ) -> JobRecord:
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    f"""
                    UPDATE background_jobs
                    SET status = 'pending', attempt_count = 0,
                        replay_count = replay_count + 1, available_at = now(),
                        last_error_code = NULL, completed_at = NULL, updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :job_id
                      AND status = 'dead_lettered'
                    RETURNING {_JOB_COLUMNS}
                    """  # noqa: S608 - _JOB_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id, "job_id": job_id},
            )
            row = result.one_or_none()
            if row is None:
                await self._raise_operator_state(connection, tenant_id, job_id, "retried")
            job = _job(row)
            await _record_event(
                connection,
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
        async with self._engine.begin() as connection:
            result = await connection.execute(
                text(
                    f"""
                    UPDATE background_jobs
                    SET status = 'cancelled', lease_owner = NULL,
                        lease_expires_at = NULL, completed_at = now(), updated_at = now()
                    WHERE tenant_id = :tenant_id AND id = :job_id
                      AND status IN ('pending', 'dead_lettered')
                    RETURNING {_JOB_COLUMNS}
                    """  # noqa: S608 - _JOB_COLUMNS is a module constant
                ),
                {"tenant_id": tenant_id, "job_id": job_id},
            )
            row = result.one_or_none()
            if row is None:
                await self._raise_operator_state(connection, tenant_id, job_id, "cancelled")
            job = _job(row)
            await _record_event(
                connection,
                job,
                JobEventType.OPERATOR_CANCELLED,
                actor_type="staff",
                actor_id=actor_id,
                request_id=request_id,
            )
            return job

    @staticmethod
    async def _raise_operator_state(
        connection: AsyncConnection, tenant_id: str, job_id: uuid.UUID, operation: str
    ) -> None:
        result = await connection.execute(
            text(
                "SELECT status FROM background_jobs WHERE tenant_id = :tenant_id AND id = :job_id"
            ),
            {"tenant_id": tenant_id, "job_id": job_id},
        )
        if result.scalar_one_or_none() is None:
            raise NotFoundError(detail="job absent or outside tenant")
        if operation == "retried":
            raise ConflictError(detail="only dead-lettered jobs can be retried")
        raise ConflictError(detail="only pending or dead-lettered jobs can be cancelled")
