"""Regression: a job UUID is not authority to inspect or control its tenant."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tenantchat.api.jobs import InMemoryJobStore, JobKind
from tenantchat.api.store import InMemoryMembershipStore


@pytest.mark.security
def test_known_job_uuid_cannot_cross_the_operator_tenant_boundary(
    client: TestClient,
    operator_headers: Callable[..., dict[str, str]],
    membership_store: InMemoryMembershipStore,
) -> None:
    async def arrange() -> str:
        await membership_store.assign(
            tenant_id="clearview", subject="operator-7", role="tenant_admin"
        )
        job = await cast(InMemoryJobStore, cast(FastAPI, client.app).state.job_store).enqueue(
            "apex",
            kind=JobKind.INGESTION,
            payload={"document_id": "document-7"},
            idempotency_key="document-7",
        )
        return str(job.job_id)

    job_id = asyncio.run(arrange())
    headers = operator_headers(role="tenant_admin")

    refused_list = client.get("/api/admin/jobs?tenant_id=apex", headers=headers)
    refused_detail = client.get(f"/api/admin/jobs/{job_id}?tenant_id=apex", headers=headers)
    disguised = client.get(f"/api/admin/jobs/{job_id}?tenant_id=clearview", headers=headers)

    assert refused_list.status_code == 403
    assert refused_list.json()["code"] == "forbidden"
    assert refused_detail.status_code == 403
    assert refused_detail.json()["code"] == refused_list.json()["code"]
    assert disguised.status_code == 404
    assert disguised.json()["code"] == "not_found"
