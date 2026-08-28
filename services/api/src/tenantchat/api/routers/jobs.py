"""Tenant-safe operator inspection and control of durable background jobs."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from tenantchat.api.dependencies import (
    Audit,
    Jobs,
    Memberships,
    RequestId,
    get_settings,
)
from tenantchat.api.identity import (
    AdminIdentity,
    authorize_tenant_access,
    require_role,
    tenant_scoped,
    verify_csrf,
)
from tenantchat.api.jobs import JobStatus
from tenantchat.api.schemas import (
    AdminJob,
    AdminJobDetailResponse,
    AdminJobEvent,
    AdminJobsResponse,
    JobControlRequest,
)
from tenantchat.api.store import AuditActorType, AuditEvent

router = APIRouter(tags=["admin-jobs"])

_tenant_read = tenant_scoped("tenant_admin")
_mutation_role = require_role("tenant_admin")

TenantReader = Annotated[AdminIdentity, Depends(_tenant_read)]
MutationIdentity = Annotated[AdminIdentity, Depends(_mutation_role)]
TenantIdQuery = Annotated[
    str, Query(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$", alias="tenant_id")
]
PageSize = Annotated[int, Query(ge=1, le=200)]


async def _authorize_mutation(
    request: Request,
    identity: AdminIdentity,
    memberships: Memberships,
    tenant_id: str,
) -> None:
    verify_csrf(request, identity, get_settings(request))
    await authorize_tenant_access(
        identity,
        memberships,
        tenant_id,
        minimum="tenant_admin",
        path=request.url.path,
    )


async def _audit_read(
    audit: Audit,
    *,
    identity: AdminIdentity,
    tenant_id: str,
    request_id: str,
    job_id: uuid.UUID | None = None,
) -> None:
    """Every privileged read self-audits; jobs were the one surface that did
    not (R-58). The row carries the filter's shape, never a payload."""
    await audit.record(
        AuditEvent(
            tenant_id=tenant_id,
            actor_type=AuditActorType.STAFF,
            principal_id=identity.subject,
            action="jobs.read",
            resource_type="background_job",
            resource_id=job_id,
            request_id=request_id,
            details={"job_id": str(job_id) if job_id is not None else None},
        )
    )


@router.get("/api/admin/jobs", response_model=AdminJobsResponse)
async def list_jobs(
    identity: TenantReader,
    tenant_id: TenantIdQuery,
    jobs: Jobs,
    audit: Audit,
    request_id: RequestId,
    status: JobStatus | None = None,
    limit: PageSize = 100,
) -> AdminJobsResponse:
    """Inspect safe metadata for one authorized tenant; payloads stay private."""
    records = await jobs.for_tenant(tenant_id, status=status, limit=limit)
    await _audit_read(audit, identity=identity, tenant_id=tenant_id, request_id=request_id)
    return AdminJobsResponse(jobs=[AdminJob.of(record) for record in records], limit=limit)


@router.get("/api/admin/jobs/{job_id}", response_model=AdminJobDetailResponse)
async def read_job(
    identity: TenantReader,
    job_id: uuid.UUID,
    tenant_id: TenantIdQuery,
    jobs: Jobs,
    audit: Audit,
    request_id: RequestId,
) -> AdminJobDetailResponse:
    """Inspect one job and its immutable lifecycle events, tenant-qualified."""
    record = await jobs.get(tenant_id, job_id)
    events = await jobs.events(tenant_id, job_id)
    await _audit_read(
        audit, identity=identity, tenant_id=tenant_id, request_id=request_id, job_id=job_id
    )
    return AdminJobDetailResponse(
        job=AdminJob.of(record),
        events=[AdminJobEvent.of(event) for event in events],
    )


@router.post("/api/admin/jobs/{job_id}/retry", response_model=AdminJob)
async def retry_job(
    request: Request,
    identity: MutationIdentity,
    job_id: uuid.UUID,
    payload: JobControlRequest,
    jobs: Jobs,
    memberships: Memberships,
    request_id: RequestId,
) -> AdminJob:
    """Replay a dead-lettered job from attempt zero, preserving its event trail."""
    await _authorize_mutation(request, identity, memberships, payload.tenant_id)
    record = await jobs.retry_dead_letter(
        payload.tenant_id,
        job_id,
        actor_id=identity.subject,
        request_id=request_id,
    )
    return AdminJob.of(record)


@router.post("/api/admin/jobs/{job_id}/cancel", response_model=AdminJob)
async def cancel_job(
    request: Request,
    identity: MutationIdentity,
    job_id: uuid.UUID,
    payload: JobControlRequest,
    jobs: Jobs,
    memberships: Memberships,
    request_id: RequestId,
) -> AdminJob:
    """Cancel pending or failed work; an active lease must first resolve."""
    await _authorize_mutation(request, identity, memberships, payload.tenant_id)
    record = await jobs.cancel(
        payload.tenant_id,
        job_id,
        actor_id=identity.subject,
        request_id=request_id,
    )
    return AdminJob.of(record)
