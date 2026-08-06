"""Long-running REL-003 worker over the PostgreSQL durable job queue.

Delivery is deliberately at least once: a process can die after an external
receiver commits but before this worker acknowledges the job. Every handler is
therefore given the stable job idempotency key and must pass it to its effect
boundary. A receiver that has already committed the key returns its original
result, making the business effect exactly once even when execution repeats.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import socket
import sys
import uuid
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import text

from tenantchat.api.correlation import (
    CorrelationContext,
    bind,
    context_extra,
    reset,
    tenant_pseudonym,
)
from tenantchat.api.index_integrity import IndexIntegrityStore
from tenantchat.api.ingestion import IngestionDependencies, ingestion_handler
from tenantchat.api.jobs import (
    JobExecutionError,
    JobHandler,
    JobKind,
    JobRecord,
    JobStore,
)
from tenantchat.api.logging_setup import configure_logging, resolve_service
from tenantchat.api.persistence import (
    Database,
    DatabasePoolSettings,
    PostgresAuditStore,
    PostgresJobStore,
    PostgresPrivacyStore,
)
from tenantchat.api.persistence.index_integrity import PostgresIndexIntegrityStore
from tenantchat.api.persistence.knowledge import PostgresKnowledgeStore
from tenantchat.api.privacy_worker import process_deletion_request
from tenantchat.api.search import (
    ElasticsearchSearchIndex,
    EmbeddingServiceClient,
)
from tenantchat.api.settings import Settings
from tenantchat.api.storage import DiskObjectStore
from tenantchat.api.store import AuditStore, PrivacyStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class WorkerSettings:
    worker_id: str
    poll_interval: float = 1.0
    lease_duration: timedelta = timedelta(seconds=60)
    batch_size: int = 10
    backoff_base: timedelta = timedelta(seconds=5)
    backoff_cap: timedelta = timedelta(hours=1)
    # The shared key tenant pseudonyms derive from; must match the API's, or a
    # job line and the request that enqueued it name the tenant differently.
    log_pseudonym_key: str | None = None

    def __post_init__(self) -> None:
        if not self.worker_id or len(self.worker_id) > 200:
            raise ValueError("JOB_WORKER_ID must be between 1 and 200 characters")
        if self.poll_interval <= 0:
            raise ValueError("JOB_POLL_SECONDS must be positive")
        if self.lease_duration <= timedelta(0):
            raise ValueError("JOB_LEASE_SECONDS must be positive")
        if not 1 <= self.batch_size <= 100:
            raise ValueError("JOB_BATCH_SIZE must be between 1 and 100")
        if self.backoff_base <= timedelta(0) or self.backoff_cap < self.backoff_base:
            raise ValueError("JOB_BACKOFF_SECONDS/JOB_BACKOFF_CAP_SECONDS are invalid")

    @classmethod
    def from_environment(cls) -> WorkerSettings:
        worker_id = os.environ.get("JOB_WORKER_ID", "").strip()
        if not worker_id:
            worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
        return cls(
            worker_id=worker_id,
            poll_interval=float(os.environ.get("JOB_POLL_SECONDS", "1")),
            lease_duration=timedelta(seconds=float(os.environ.get("JOB_LEASE_SECONDS", "60"))),
            batch_size=int(os.environ.get("JOB_BATCH_SIZE", "10")),
            backoff_base=timedelta(seconds=float(os.environ.get("JOB_BACKOFF_SECONDS", "5"))),
            backoff_cap=timedelta(seconds=float(os.environ.get("JOB_BACKOFF_CAP_SECONDS", "3600"))),
            log_pseudonym_key=os.environ.get("CHAT_API_LOG_PSEUDONYM_KEY", "").strip() or None,
        )


def privacy_deletion_handler(store: PrivacyStore, audit: AuditStore) -> JobHandler:
    """Build the REL-003-owned production handler: privacy erasure."""

    async def handle(job: JobRecord) -> None:
        raw_request_id = job.payload.get("request_id")
        if not isinstance(raw_request_id, str):
            raise JobExecutionError("privacy_request_id_invalid", retryable=False)
        try:
            request_id = uuid.UUID(raw_request_id)
        except ValueError as exc:
            raise JobExecutionError("privacy_request_id_invalid", retryable=False) from exc

        records = await store.requests_for_tenant(job.tenant_id)
        request = next((item for item in records if item.request_id == request_id), None)
        if request is None:
            raise JobExecutionError("privacy_request_missing", retryable=False)
        # A worker can crash after completing the domain request and before
        # acknowledging this queue row. Completion is the idempotency receipt.
        if request.status == "completed":
            return
        if request.status != "pending":
            raise JobExecutionError("privacy_request_not_pending", retryable=False)
        await process_deletion_request(request, store, audit, now=datetime.now(UTC))

    return handle


def ingestion_job_handler(
    knowledge: PostgresKnowledgeStore,
    generations: IndexIntegrityStore,
    settings: Settings,
) -> JobHandler | None:
    """Build the RAG-002 ingestion handler, or ``None`` when not configured.

    Returns ``None`` (and the ingestion jobs then dead-letter with
    ``handler_not_registered``) rather than guessing at endpoints: a partial
    configuration is a deployment bug that must be visible as refused work,
    not as indexing into a defaulted URL.
    """
    if not (
        settings.ingestion_storage_root and settings.embedding_url and settings.elasticsearch_url
    ):
        return None
    dependencies = IngestionDependencies(
        knowledge=knowledge,
        generations=generations,
        storage=DiskObjectStore(Path(settings.ingestion_storage_root)),
        index=ElasticsearchSearchIndex(
            base_url=settings.elasticsearch_url,
            username=settings.elasticsearch_username,
            password=settings.elasticsearch_password,
            index_name=settings.elasticsearch_index,
        ),
        embedder=EmbeddingServiceClient(
            base_url=settings.embedding_url,
            token=settings.embedding_token,
        ),
        audit=PostgresAuditStore(knowledge.engine),
    )
    return ingestion_handler(dependencies)


async def _heartbeat(
    jobs: JobStore,
    job_id: uuid.UUID,
    settings: WorkerSettings,
    stopped: asyncio.Event,
) -> None:
    interval = max(settings.lease_duration.total_seconds() / 3, 0.05)
    while True:
        try:
            await asyncio.wait_for(stopped.wait(), timeout=interval)
            return
        except TimeoutError:
            await jobs.renew(
                job_id,
                worker_id=settings.worker_id,
                lease_for=settings.lease_duration,
            )


async def execute_job(
    jobs: JobStore,
    job: JobRecord,
    handlers: Mapping[JobKind, JobHandler],
    settings: WorkerSettings,
) -> None:
    """Execute and acknowledge one leased job, renewing its lease meanwhile."""
    stopped = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(jobs, job.job_id, settings, stopped))
    retryable = True
    error_code: str | None = None
    # The correlation context is the enqueuing request's when the payload
    # carries it (the enqueuer's trace); otherwise the job itself is the unit
    # of work and names its own trace. The request ID is the durable domain
    # request the enqueuer stored, so a handler's lines tie back to the
    # operator action that filed the work.
    payload = job.payload
    raw_trace = payload.get("trace_id")
    raw_request = payload.get("request_id")
    trace = raw_trace if isinstance(raw_trace, str) and raw_trace else str(job.job_id)
    request = raw_request if isinstance(raw_request, str) and raw_request else str(job.job_id)
    bind(
        CorrelationContext(
            request_id=request,
            trace_id=trace,
            tenant_id=job.tenant_id,
            tenant_pseudonym=tenant_pseudonym(job.tenant_id, key=settings.log_pseudonym_key),
        )
    )
    try:
        handler = handlers.get(job.kind)
        if handler is None:
            raise JobExecutionError("handler_not_registered", retryable=False)
        await handler(job)
    except JobExecutionError as exc:
        error_code = exc.error_code
        retryable = exc.retryable
    except Exception:
        # Handler exceptions can contain document content, contacts, provider
        # bodies, and credentials. The durable record gets only a stable code.
        logger.error(
            "background job handler failed",
            extra={
                "job_id": str(job.job_id),
                "error_code": "handler_unexpected",
                **context_extra(),
            },
        )
        error_code = "handler_unexpected"
    finally:
        stopped.set()
        await heartbeat

    if error_code is None:
        await jobs.succeed(job.job_id, worker_id=settings.worker_id)
        logger.info(
            "background job succeeded",
            extra={"job_id": str(job.job_id), **context_extra()},
        )
    else:
        await jobs.fail(
            job.job_id,
            worker_id=settings.worker_id,
            error_code=error_code,
            retryable=retryable,
            backoff_base=settings.backoff_base,
            backoff_cap=settings.backoff_cap,
        )
    reset()


async def run_once(
    jobs: JobStore,
    handlers: Mapping[JobKind, JobHandler],
    settings: WorkerSettings,
) -> int:
    """Lease one bounded batch and wait for every result to be committed."""
    leased = await jobs.lease(
        worker_id=settings.worker_id,
        limit=settings.batch_size,
        lease_for=settings.lease_duration,
    )
    await asyncio.gather(*(execute_job(jobs, job, handlers, settings) for job in leased))
    return len(leased)


async def run_worker(
    jobs: JobStore,
    handlers: Mapping[JobKind, JobHandler],
    settings: WorkerSettings,
    stop: asyncio.Event,
) -> None:
    """Poll until shutdown, then leave no newly leased work in process."""
    while not stop.is_set():
        try:
            processed = await run_once(jobs, handlers, settings)
        except Exception:
            # A database outage makes readiness fail and pauses delivery. It
            # must not turn into a process restart loop; any lease whose ack
            # failed is reclaimed after expiry and its effect deduplicates.
            logger.error(
                "background job polling paused",
                extra={"error_code": "job_store_unavailable", **context_extra()},
            )
            with suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval)
            continue
        if processed:
            continue
        with suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=settings.poll_interval)


async def _serve(app_settings: Settings, worker_settings: WorkerSettings) -> None:
    if not app_settings.database_url:
        raise ValueError("DATABASE_URL is required")
    if not app_settings.privacy_database_url:
        raise ValueError("PRIVACY_DATABASE_URL is required for the deletion handler")
    pool = DatabasePoolSettings(
        size=app_settings.database_pool_size,
        max_overflow=app_settings.database_max_overflow,
        timeout_seconds=app_settings.database_pool_timeout_seconds,
        recycle_seconds=app_settings.database_pool_recycle_seconds,
    )
    read = Database.connect(app_settings.database_url, pool)
    erasure = Database.connect(
        app_settings.privacy_database_url,
        DatabasePoolSettings(
            size=app_settings.privacy_database_pool_size,
            max_overflow=app_settings.privacy_database_max_overflow,
            timeout_seconds=app_settings.database_pool_timeout_seconds,
            recycle_seconds=app_settings.database_pool_recycle_seconds,
        ),
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for caught in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(caught, stop.set)
    try:
        privacy = PostgresPrivacyStore(read.engine, erasure.engine)
        handlers: dict[JobKind, JobHandler] = {
            JobKind.PRIVACY_DELETION: privacy_deletion_handler(
                privacy, PostgresAuditStore(read.engine)
            )
        }
        ingestion = ingestion_job_handler(
            PostgresKnowledgeStore(read.engine),
            PostgresIndexIntegrityStore(read.engine),
            app_settings,
        )
        if ingestion is not None:
            handlers[JobKind.INGESTION] = ingestion
        await run_worker(PostgresJobStore(read.engine), handlers, worker_settings, stop)
    finally:
        await read.dispose()
        await erasure.dispose()


async def _check(app_settings: Settings) -> None:
    """Readiness check: both databases are reachable; no job is leased."""
    if not app_settings.database_url or not app_settings.privacy_database_url:
        raise ValueError("DATABASE_URL and PRIVACY_DATABASE_URL are required")
    pool = DatabasePoolSettings(size=1, max_overflow=0, timeout_seconds=2, recycle_seconds=60)
    read = Database.connect(app_settings.database_url, pool)
    erasure = Database.connect(app_settings.privacy_database_url, pool)
    try:
        async with read.engine.begin() as connection:
            await connection.execute(text("SELECT 1"))
        async with erasure.engine.begin() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await read.dispose()
        await erasure.dispose()


def main() -> int:
    try:
        app_settings = Settings.from_environment()
        configure_logging(
            service=resolve_service("chat-job-worker"),
            environment=app_settings.app_env,
            level=app_settings.log_level,
            json_enabled=app_settings.log_json,
        )
        if sys.argv[1:] == ["--check"]:
            asyncio.run(_check(app_settings))
        else:
            asyncio.run(_serve(app_settings, WorkerSettings.from_environment()))
    except ValueError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
