"""Liveness probe.

Liveness only: it answers "is this process serving HTTP", nothing more. A probe
that checks dependencies restarts a healthy pod when the database blips, turning
a partial outage into a total one. Dependency-aware readiness belongs to each
dependency's own task, per the backlog's definition of done.
"""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from tenantchat.api.schemas import HealthResponse

router = APIRouter(tags=["operations"])


@router.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=HealthResponse)
async def readyz(request: Request, response: Response) -> HealthResponse:
    """Dependency-aware readiness; failures remove the pod without restarting it."""
    try:
        database = request.app.state.database
        if database is not None:
            await database.ready()
        settings = request.app.state.settings
        evidence = request.app.state.evidence_source
        if settings.rag_required:
            if evidence is None:
                raise RuntimeError("required evidence source absent")
            first_tenant = next(iter(request.app.state.registry.all()))
            ready = getattr(evidence, "ready", None)
            if ready is None:
                raise RuntimeError("required evidence source has no readiness contract")
            await ready(tenant_id=first_tenant)
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return HealthResponse(status="unavailable")
    return HealthResponse(status="ready")
