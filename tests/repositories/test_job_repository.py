"""REL-003 durable delivery against a real PostgreSQL 16 database."""

from __future__ import annotations

import asyncio
import tempfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from tenantchat.api.app import create_app
from tenantchat.api.identity import (
    CSRF_HEADER,
    EMAIL_HEADER,
    GATEWAY_TOKEN_HEADER,
    ROLE_HEADER,
    SUBJECT_HEADER,
)
from tenantchat.api.job_worker import WorkerSettings, run_once
from tenantchat.api.jobs import JobKind, JobRecord, JobStatus
from tenantchat.api.persistence import Database, DatabasePoolSettings, PostgresJobStore
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.settings import Settings
from tenantchat.core.errors import ConflictError, NotFoundError

TEST_POOL = DatabasePoolSettings(size=2, max_overflow=1, timeout_seconds=5)


async def _database(database_url: str) -> Database:
    database = Database.connect(database_url, TEST_POOL)
    registry = TenantRegistry.seeded()
    await database.synchronize_tenants(
        (record.policy.tenant_id, record.policy.name) for record in registry.all().values()
    )
    return database


@pytest.mark.integration
def test_enqueue_is_idempotent_and_tenant_qualified(repository_database_url: str) -> None:
    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresJobStore(database.engine)
            first = await store.enqueue(
                "clearview",
                kind=JobKind.WEBHOOK,
                payload={"event_id": "evt-7"},
                idempotency_key="delivery-7",
            )
            duplicate = await store.enqueue(
                "clearview",
                kind=JobKind.WEBHOOK,
                payload={"event_id": "evt-7"},
                idempotency_key="delivery-7",
            )

            assert duplicate == first
            assert await store.for_tenant("apex") == ()
            with pytest.raises(NotFoundError):
                await store.get("apex", first.job_id)
            with pytest.raises(ConflictError):
                await store.enqueue(
                    "clearview",
                    kind=JobKind.WEBHOOK,
                    payload={"event_id": "different"},
                    idempotency_key="delivery-7",
                )
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_retry_backoff_dead_letter_and_operator_controls_are_audited(
    repository_database_url: str,
) -> None:
    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            store = PostgresJobStore(database.engine)
            queued = await store.enqueue(
                "clearview",
                kind=JobKind.NOTIFICATION,
                payload={"notification_id": "notice-1"},
                idempotency_key="notice-1",
                max_attempts=2,
            )
            first = (
                await store.lease(worker_id="worker-a", limit=1, lease_for=timedelta(seconds=30))
            )[0]
            retried = await store.fail(
                first.job_id,
                worker_id="worker-a",
                error_code="provider_timeout",
                retryable=True,
                backoff_base=timedelta(seconds=5),
                backoff_cap=timedelta(minutes=1),
            )
            assert retried.status is JobStatus.PENDING
            assert retried.available_at > first.available_at

            # Advance only this fixture's delivery time; sleeping would make a
            # deterministic repository test both slow and flaky.
            async with database.engine.begin() as connection:
                await connection.execute(
                    text("UPDATE background_jobs SET available_at = now() WHERE id = :job_id"),
                    {"job_id": queued.job_id},
                )
            second = (
                await store.lease(worker_id="worker-b", limit=1, lease_for=timedelta(seconds=30))
            )[0]
            dead = await store.fail(
                second.job_id,
                worker_id="worker-b",
                error_code="provider_timeout",
                retryable=True,
                backoff_base=timedelta(seconds=5),
                backoff_cap=timedelta(minutes=1),
            )
            assert dead.status is JobStatus.DEAD_LETTERED

            replay = await store.retry_dead_letter(
                "clearview",
                queued.job_id,
                actor_id="operator-7",
                request_id="request-retry",
            )
            assert replay.status is JobStatus.PENDING
            assert replay.attempt_count == 0
            assert replay.replay_count == 1
            cancelled = await store.cancel(
                "clearview",
                queued.job_id,
                actor_id="operator-7",
                request_id="request-cancel",
            )
            assert cancelled.status is JobStatus.CANCELLED

            events = await store.events("clearview", queued.job_id)
            assert [event.event.value for event in events] == [
                "enqueued",
                "leased",
                "retry_scheduled",
                "leased",
                "dead_lettered",
                "operator_retried",
                "operator_cancelled",
            ]
            assert events[-2].actor_id == "operator-7"
            assert events[-2].request_id == "request-retry"
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_competing_workers_cannot_lease_the_same_delivery(
    repository_database_url: str,
) -> None:
    async def scenario() -> None:
        database = await _database(repository_database_url)
        try:
            first_store = PostgresJobStore(database.engine)
            second_store = PostgresJobStore(database.engine)
            queued = await first_store.enqueue(
                "clearview",
                kind=JobKind.WEBHOOK,
                payload={"event_id": "race-1"},
                idempotency_key="race-1",
            )

            first, second = await asyncio.gather(
                first_store.lease(worker_id="worker-a", limit=1, lease_for=timedelta(seconds=30)),
                second_store.lease(worker_id="worker-b", limit=1, lease_for=timedelta(seconds=30)),
            )

            assert len(first) + len(second) == 1
            assert (first or second)[0].job_id == queued.job_id
        finally:
            await database.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_worker_restart_replays_delivery_with_exactly_one_business_effect(
    repository_database_url: str,
) -> None:
    """Crash after receiver commit, expire lease, then recover through dedupe."""

    async def scenario() -> None:
        first_process = await _database(repository_database_url)
        store = PostgresJobStore(first_process.engine)
        queued = await store.enqueue(
            "clearview",
            kind=JobKind.CRM_DELIVERY,
            payload={"lead_id": "lead-42"},
            idempotency_key="crm-lead-42",
        )
        leased = (
            await store.lease(
                worker_id="worker-before-restart",
                limit=1,
                lease_for=timedelta(minutes=5),
            )
        )[0]

        receiver_records: dict[str, str] = {}

        async def idempotent_receiver(job: JobRecord) -> None:
            key = job.idempotency_key
            receiver_records.setdefault(key, "external-record-1")

        # The external receiver commits, then the process dies before succeed().
        await idempotent_receiver(leased)
        await first_process.dispose()

        restarted = await _database(repository_database_url)
        try:
            async with restarted.engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE background_jobs SET lease_expires_at = now() - interval '1 second' "
                        "WHERE id = :job_id"
                    ),
                    {"job_id": queued.job_id},
                )
            restarted_store = PostgresJobStore(restarted.engine)
            processed = await run_once(
                restarted_store,
                {JobKind.CRM_DELIVERY: idempotent_receiver},
                WorkerSettings(
                    worker_id="worker-after-restart",
                    lease_duration=timedelta(seconds=30),
                    batch_size=1,
                ),
            )

            assert processed == 1
            assert receiver_records == {"crm-lead-42": "external-record-1"}
            assert (
                await restarted_store.get("clearview", queued.job_id)
            ).status is JobStatus.SUCCEEDED
        finally:
            await restarted.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_operator_routes_control_postgres_jobs_with_the_same_tenant_contract(
    repository_database_url: str,
) -> None:
    async def arrange() -> str:
        database = await _database(repository_database_url)
        try:
            store = PostgresJobStore(database.engine)
            job = await store.enqueue(
                "clearview",
                kind=JobKind.NOTIFICATION,
                payload={"contact": "private@example.com"},
                idempotency_key="operator-route-1",
                max_attempts=1,
            )
            leased = (
                await store.lease(
                    worker_id="worker-route", limit=1, lease_for=timedelta(seconds=30)
                )
            )[0]
            await store.fail(
                leased.job_id,
                worker_id="worker-route",
                error_code="provider_rejected",
                retryable=False,
                backoff_base=timedelta(seconds=1),
                backoff_cap=timedelta(seconds=5),
            )
            return str(job.job_id)
        finally:
            await database.dispose()

    job_id = asyncio.run(arrange())
    settings = Settings(
        allowed_origins=("https://widget.example",),
        max_request_bytes=2048,
        docs_enabled=False,
        database_url=repository_database_url,
        database_pool_size=2,
        database_max_overflow=0,
        admin_gateway_token="gateway-token-for-job-tests",
        admin_csrf_secret="csrf-secret-for-job-tests",
        visitor_credential_signing_key="visitor-signing-key-for-tests-" + "x" * 16,
        ingestion_storage_root=tempfile.mkdtemp(prefix="tenantchat-job-repository-"),
    )
    headers = {
        GATEWAY_TOKEN_HEADER: "gateway-token-for-job-tests",
        SUBJECT_HEADER: "platform-operator",
        EMAIL_HEADER: "operator@example.com",
        ROLE_HEADER: "platform_admin",
    }
    with TestClient(create_app(settings), raise_server_exceptions=False) as client:
        refused = client.get("/api/admin/jobs?tenant_id=clearview")
        assert refused.status_code == 401

        listed = client.get("/api/admin/jobs?tenant_id=clearview", headers=headers)
        assert listed.status_code == 200, listed.text
        assert listed.json()["jobs"][0]["job_id"] == job_id
        assert "private@example.com" not in listed.text

        wrong_tenant = client.get(f"/api/admin/jobs/{job_id}?tenant_id=apex", headers=headers)
        assert wrong_tenant.status_code == 404

        csrf = client.get("/api/admin/csrf-token", headers=headers).json()["csrf_token"]
        mutation_headers = headers | {CSRF_HEADER: csrf}
        replay = client.post(
            f"/api/admin/jobs/{job_id}/retry",
            json={"tenant_id": "clearview"},
            headers=mutation_headers,
        )
        assert replay.status_code == 200, replay.text
        cancelled = client.post(
            f"/api/admin/jobs/{job_id}/cancel",
            json={"tenant_id": "clearview"},
            headers=mutation_headers,
        )
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.json()["status"] == "cancelled"
