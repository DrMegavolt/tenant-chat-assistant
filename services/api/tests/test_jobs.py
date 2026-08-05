"""Operator controls and worker state-machine behavior for REL-003."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenantchat.api.identity import CSRF_HEADER
from tenantchat.api.jobs import InMemoryJobStore, JobKind, JobStatus
from tenantchat.api.store import InMemoryMembershipStore


def _csrf(client: TestClient, headers: dict[str, str]) -> str:
    response = client.get("/api/admin/csrf-token", headers=headers)
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def test_operator_can_inspect_retry_and_cancel_without_seeing_payload(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    async def arrange() -> str:
        await membership_store.assign(
            tenant_id="clearview", subject="operator-7", role="tenant_admin"
        )
        store = cast(InMemoryJobStore, cast(FastAPI, client.app).state.job_store)
        job = await store.enqueue(
            "clearview",
            kind=JobKind.WEBHOOK,
            payload={"contact": "private@example.com"},
            idempotency_key="webhook-1",
            max_attempts=1,
        )
        leased = (
            await store.lease(worker_id="worker-1", limit=1, lease_for=timedelta(seconds=30))
        )[0]
        await store.fail(
            leased.job_id,
            worker_id="worker-1",
            error_code="receiver_rejected",
            retryable=False,
            backoff_base=timedelta(seconds=1),
            backoff_cap=timedelta(seconds=5),
        )
        return str(job.job_id)

    job_id = asyncio.run(arrange())
    headers = operator_headers(role="tenant_admin")

    listed = client.get("/api/admin/jobs?tenant_id=clearview", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()["jobs"][0]["status"] == JobStatus.DEAD_LETTERED.value
    assert "payload" not in listed.text
    assert "private@example.com" not in listed.text

    detail = client.get(f"/api/admin/jobs/{job_id}?tenant_id=clearview", headers=headers)
    assert detail.status_code == 200, detail.text
    assert [event["event"] for event in detail.json()["events"]] == [
        "enqueued",
        "leased",
        "dead_lettered",
    ]

    mutation_headers = headers | {CSRF_HEADER: _csrf(client, headers)}
    retried = client.post(
        f"/api/admin/jobs/{job_id}/retry",
        json={"tenant_id": "clearview"},
        headers=mutation_headers,
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["status"] == "pending"

    cancelled = client.post(
        f"/api/admin/jobs/{job_id}/cancel",
        json={"tenant_id": "clearview"},
        headers=mutation_headers,
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"


def test_job_routes_require_an_authenticated_operator(client: TestClient) -> None:
    response = client.get("/api/admin/jobs?tenant_id=clearview")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
