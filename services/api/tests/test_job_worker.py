"""Hermetic worker failure and retry-policy specifications."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tenantchat.api.job_worker import WorkerSettings, run_once, run_worker
from tenantchat.api.jobs import InMemoryJobStore, JobKind, JobRecord, JobStatus, retry_delay


def test_backoff_is_exponential_and_caps_without_overflow() -> None:
    base = timedelta(seconds=5)
    cap = timedelta(hours=1)

    assert retry_delay(1, base, cap) == timedelta(seconds=5)
    assert retry_delay(4, base, cap) == timedelta(seconds=40)
    assert retry_delay(100, base, cap) == cap


def test_unexpected_handler_error_is_redacted_and_dead_lettered(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        store = InMemoryJobStore()
        job = await store.enqueue(
            "clearview",
            kind=JobKind.WEBHOOK,
            payload={"contact": "private@example.com"},
            idempotency_key="event-1",
            max_attempts=1,
            available_at=datetime.now(UTC),
        )

        async def broken_handler(_job: JobRecord) -> None:
            raise RuntimeError("provider leaked private@example.com token=secret")

        processed = await run_once(
            store,
            {JobKind.WEBHOOK: broken_handler},
            WorkerSettings(
                worker_id="worker-test",
                batch_size=1,
                lease_duration=timedelta(seconds=30),
            ),
        )

        assert processed == 1
        failed = await store.get("clearview", job.job_id)
        assert failed.status is JobStatus.DEAD_LETTERED
        assert failed.last_error_code == "handler_unexpected"
        events = await store.events("clearview", job.job_id)
        assert "private@example.com" not in repr(events)
        assert "token=secret" not in repr(events)

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert "private@example.com" not in caplog.text
    assert "token=secret" not in caplog.text


def test_job_without_a_registered_handler_is_a_permanent_failure() -> None:
    async def scenario() -> None:
        store = InMemoryJobStore()
        job = await store.enqueue(
            "clearview",
            kind=JobKind.INGESTION,
            payload={"document_id": "doc-1"},
            idempotency_key="doc-1",
        )
        await run_once(
            store,
            {},
            WorkerSettings(
                worker_id="worker-test",
                batch_size=1,
                lease_duration=timedelta(seconds=30),
            ),
        )

        failed = await store.get("clearview", job.job_id)
        assert failed.status is JobStatus.DEAD_LETTERED
        assert failed.last_error_code == "handler_not_registered"

    asyncio.run(scenario())


def test_database_outage_pauses_delivery_without_crashing_or_logging_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()

        class FlakyStore(InMemoryJobStore):
            calls = 0

            async def lease(
                self,
                *,
                worker_id: str,
                limit: int,
                lease_for: timedelta,
            ) -> tuple[JobRecord, ...]:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("postgres password=private")
                stop.set()
                return await super().lease(
                    worker_id=worker_id,
                    limit=limit,
                    lease_for=lease_for,
                )

        store = FlakyStore()
        job = await store.enqueue(
            "clearview",
            kind=JobKind.WEBHOOK,
            payload={"event_id": "event-after-outage"},
            idempotency_key="event-after-outage",
        )

        async def handler(_job: JobRecord) -> None:
            return None

        await run_worker(
            store,
            {JobKind.WEBHOOK: handler},
            WorkerSettings(
                worker_id="worker-test",
                poll_interval=0.001,
                batch_size=1,
                lease_duration=timedelta(seconds=30),
            ),
            stop,
        )
        assert (await store.get("clearview", job.job_id)).status is JobStatus.SUCCEEDED

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert any(
        getattr(record, "error_code", None) == "job_store_unavailable" for record in caplog.records
    )
    assert "password=private" not in caplog.text


def test_a_failed_heartbeat_does_not_prevent_settling_a_successful_job(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-26: the heartbeat runs in the handler's ``finally`` shadow — if its
    exception unwound there, a lease-renewal blip would skip the ack and the
    job would be redelivered after an effect that already committed."""
    lease_duration = timedelta(seconds=0.15)

    class BrokenRenewals(InMemoryJobStore):
        async def renew(
            self, job_id: uuid.UUID, *, worker_id: str, lease_for: timedelta
        ) -> JobRecord:
            raise RuntimeError("connection reset during renew")

    async def scenario() -> None:
        store = BrokenRenewals()
        job = await store.enqueue(
            "clearview",
            kind=JobKind.WEBHOOK,
            payload={"event_id": "event-1"},
            idempotency_key="event-1",
        )

        async def handler(_job: JobRecord) -> None:
            await asyncio.sleep(0.12)

        processed = await run_once(
            store,
            {JobKind.WEBHOOK: handler},
            WorkerSettings(
                worker_id="worker-test",
                batch_size=1,
                lease_duration=lease_duration,
            ),
        )
        assert processed == 1
        settled = await store.get("clearview", job.job_id)
        assert settled.status is JobStatus.SUCCEEDED

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert any(
        getattr(record, "error_code", None) == "heartbeat_failed" for record in caplog.records
    )


def test_a_failed_heartbeat_does_not_prevent_failing_the_job() -> None:
    """The failure path settles too: the handler's error must reach the store
    even when the lease could not be renewed meanwhile."""
    lease_duration = timedelta(seconds=0.15)

    class BrokenRenewals(InMemoryJobStore):
        async def renew(
            self, job_id: uuid.UUID, *, worker_id: str, lease_for: timedelta
        ) -> JobRecord:
            raise RuntimeError("connection reset during renew")

    async def scenario() -> None:
        store = BrokenRenewals()
        job = await store.enqueue(
            "clearview",
            kind=JobKind.WEBHOOK,
            payload={"event_id": "event-1"},
            idempotency_key="event-1",
            max_attempts=1,
        )

        async def broken_handler(_job: JobRecord) -> None:
            await asyncio.sleep(0.12)
            raise RuntimeError("receiver refused the webhook")

        await run_once(
            store,
            {JobKind.WEBHOOK: broken_handler},
            WorkerSettings(
                worker_id="worker-test",
                batch_size=1,
                lease_duration=lease_duration,
            ),
        )
        settled = await store.get("clearview", job.job_id)
        assert settled.status is JobStatus.DEAD_LETTERED
        assert settled.last_error_code == "handler_unexpected"

    asyncio.run(scenario())


def test_one_jobs_settlement_failure_does_not_orphan_the_batch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R-26: a bare ``gather`` would propagate the first settlement failure and
    strand the surviving tasks detached while the worker leases the next batch
    on top of them. Every job must settle, and the failure must be visible."""
    broken_job_id: uuid.UUID | None = None

    class BrokenAck(InMemoryJobStore):
        async def succeed(self, job_id: uuid.UUID, *, worker_id: str) -> JobRecord:
            if job_id == broken_job_id:
                raise RuntimeError("ack write failed")
            return await super().succeed(job_id, worker_id=worker_id)

    async def scenario() -> None:
        nonlocal broken_job_id
        store = BrokenAck()
        first = await store.enqueue(
            "clearview",
            kind=JobKind.WEBHOOK,
            payload={"event_id": "event-1"},
            idempotency_key="event-1",
        )
        second = await store.enqueue(
            "clearview",
            kind=JobKind.WEBHOOK,
            payload={"event_id": "event-2"},
            idempotency_key="event-2",
        )
        broken_job_id = first.job_id

        async def handler(_job: JobRecord) -> None:
            return None

        processed = await run_once(
            store,
            {JobKind.WEBHOOK: handler},
            WorkerSettings(
                worker_id="worker-test",
                batch_size=2,
                lease_duration=timedelta(seconds=30),
            ),
        )
        assert processed == 2
        assert (await store.get("clearview", second.job_id)).status is JobStatus.SUCCEEDED
        stranded = await store.get("clearview", first.job_id)
        assert stranded.status is not JobStatus.SUCCEEDED

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert any(
        getattr(record, "error_code", None) == "job_settlement_failed" for record in caplog.records
    )
