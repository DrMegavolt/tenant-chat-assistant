"""Hermetic worker failure and retry-policy specifications."""

from __future__ import annotations

import asyncio
import logging
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
