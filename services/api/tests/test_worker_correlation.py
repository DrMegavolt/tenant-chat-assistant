"""OBS-001: background job execution continues the enqueuing request's trace.

The enqueuer puts its trace in the job payload; the fingerprint treats the
trace as attribution rather than work, so a retried enqueue with a fresh trace
still deduplicates; and the worker binds the payload's request ID, trace, and
tenant pseudonym for the duration of one job execution.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any, cast

import pytest

from tenantchat.api.correlation import CorrelationContext, current, tenant_pseudonym
from tenantchat.api.job_worker import WorkerSettings, run_once
from tenantchat.api.jobs import InMemoryJobStore, JobKind, JobRecord, payload_fingerprint

TEST_KEY = "worker-test-pseudonym-key"


class TestFingerprint:
    def test_the_trace_is_attribution_not_work(self) -> None:
        with_trace_a = payload_fingerprint({"request_id": "req-9", "trace_id": "trace-a"})
        with_trace_b = payload_fingerprint({"request_id": "req-9", "trace_id": "trace-b"})
        different_work = payload_fingerprint({"request_id": "req-8", "trace_id": "trace-a"})

        assert with_trace_a == with_trace_b
        assert with_trace_a != different_work

    def test_a_retried_enqueue_with_a_fresh_trace_deduplicates(self) -> None:
        store = InMemoryJobStore()

        async def scenario() -> tuple[uuid.UUID, uuid.UUID]:
            first = await store.enqueue(
                "clearview",
                kind=JobKind.WEBHOOK,
                payload={"request_id": "req-9", "trace_id": "trace-a"},
                idempotency_key="event-1",
            )
            retried = await store.enqueue(
                "clearview",
                kind=JobKind.WEBHOOK,
                payload={"request_id": "req-9", "trace_id": "trace-b"},
                idempotency_key="event-1",
            )
            return first.job_id, retried.job_id

        first, retried = asyncio.run(scenario())
        assert retried == first


class TestWorkerContext:
    def test_the_handler_runs_under_the_enqueuing_requests_context(self) -> None:
        store = InMemoryJobStore()
        seen: list[CorrelationContext | None] = []

        async def scenario() -> None:
            await store.enqueue(
                "clearview",
                kind=JobKind.WEBHOOK,
                payload={"request_id": "req-1", "trace_id": "trace-9"},
                idempotency_key="event-1",
            )

            async def recording_handler(_job: JobRecord) -> None:
                seen.append(current())

            await run_once(
                store,
                {JobKind.WEBHOOK: recording_handler},
                WorkerSettings(
                    worker_id="worker-test",
                    batch_size=1,
                    lease_duration=timedelta(seconds=30),
                    log_pseudonym_key=TEST_KEY,
                ),
            )

        asyncio.run(scenario())
        context = seen[0]
        assert context is not None
        assert context.request_id == "req-1"
        assert context.trace_id == "trace-9"
        assert context.tenant_id == "clearview"
        assert context.tenant_pseudonym == tenant_pseudonym("clearview", key=TEST_KEY)

    def test_without_a_payload_trace_the_job_names_itself(self) -> None:
        store = InMemoryJobStore()
        seen: list[CorrelationContext | None] = []

        async def scenario() -> uuid.UUID:
            job = await store.enqueue(
                "clearview",
                kind=JobKind.INGESTION,
                payload={"document_id": "doc-1"},
                idempotency_key="doc-1",
            )

            async def recording_handler(_job: JobRecord) -> None:
                seen.append(current())

            await run_once(
                store,
                {JobKind.INGESTION: recording_handler},
                WorkerSettings(
                    worker_id="worker-test",
                    batch_size=1,
                    lease_duration=timedelta(seconds=30),
                ),
            )
            return job.job_id

        job_id = asyncio.run(scenario())
        context = seen[0]
        assert context is not None
        assert context.trace_id == str(job_id)
        assert context.request_id == str(job_id)

    def test_the_success_event_carries_the_correlation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = InMemoryJobStore()

        async def scenario() -> uuid.UUID:
            job = await store.enqueue(
                "clearview",
                kind=JobKind.WEBHOOK,
                payload={"request_id": "req-1", "trace_id": "trace-9"},
                idempotency_key="event-1",
            )

            async def quiet_handler(_job: JobRecord) -> None:
                return None

            await run_once(
                store,
                {JobKind.WEBHOOK: quiet_handler},
                WorkerSettings(
                    worker_id="worker-test",
                    batch_size=1,
                    lease_duration=timedelta(seconds=30),
                    log_pseudonym_key=TEST_KEY,
                ),
            )
            return job.job_id

        with caplog.at_level(logging.INFO):
            job_id = asyncio.run(scenario())

        events = [record for record in caplog.records if record.msg == "background job succeeded"]
        assert len(events) == 1
        record = cast(Any, events[0])
        assert record.job_id == str(job_id)
        assert record.request_id == "req-1"
        assert record.trace_id == "trace-9"

    def test_the_failure_event_still_carries_the_correlation(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        store = InMemoryJobStore()

        async def scenario() -> None:
            await store.enqueue(
                "clearview",
                kind=JobKind.WEBHOOK,
                payload={"request_id": "req-1", "trace_id": "trace-9"},
                idempotency_key="event-1",
            )

            async def broken_handler(_job: JobRecord) -> None:
                raise RuntimeError("boom")

            await run_once(
                store,
                {JobKind.WEBHOOK: broken_handler},
                WorkerSettings(
                    worker_id="worker-test",
                    batch_size=1,
                    lease_duration=timedelta(seconds=30),
                    log_pseudonym_key=TEST_KEY,
                ),
            )

        with caplog.at_level(logging.ERROR):
            asyncio.run(scenario())

        events = [
            record for record in caplog.records if record.msg == "background job handler failed"
        ]
        assert len(events) == 1
        failed = cast(Any, events[0])
        assert failed.request_id == "req-1"
        assert failed.trace_id == "trace-9"
