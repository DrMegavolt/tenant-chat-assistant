"""Liveness probe.

Liveness only: it answers "is this process serving HTTP", nothing more. A probe
that checks dependencies restarts a healthy pod when the database blips, turning
a partial outage into a total one. Dependency-aware readiness belongs to each
dependency's own task, per the backlog's definition of done.
"""

from __future__ import annotations

from fastapi import APIRouter

from tenantchat.api.schemas import HealthResponse

router = APIRouter(tags=["operations"])


@router.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(status="ok")
