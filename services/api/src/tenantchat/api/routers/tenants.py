"""Tenant branding and availability, as the embedded widget sees them."""

from __future__ import annotations

from fastapi import APIRouter

from tenantchat.api.dependencies import Registry
from tenantchat.api.schemas import AvailabilityResponse, TenantsResponse, TenantSummary
from tenantchat.core.errors import UnknownServiceError

router = APIRouter(tags=["tenants"])


@router.get("/api/tenants", response_model=TenantsResponse)
def list_tenants(registry: Registry) -> TenantsResponse:
    """Every tenant this deployment serves, public fields only.

    Unauthenticated: the widget needs branding before a visitor has any identity.
    Nothing private is reachable, because the response is projected from
    ``PublicTenantView``.
    """
    return TenantsResponse(
        tenants={
            tenant_id: TenantSummary.of(record.policy.public_view())
            for tenant_id, record in registry.all().items()
        }
    )


@router.get("/api/tenants/{tenant_id}/availability", response_model=AvailabilityResponse)
def get_availability(tenant_id: str, service: str, registry: Registry) -> AvailabilityResponse:
    """Slots currently offered for one service.

    The same list the booking endpoint validates against, so a slot shown here is
    a slot that will be accepted.

    Raises:
        NotFoundError: no such tenant.
        UnknownServiceError: the service did not resolve against the catalog.
    """
    record = registry.get(tenant_id)
    resolved = record.policy.catalog.resolve(service)
    if resolved is None:
        raise UnknownServiceError(
            offered=record.policy.catalog.offered_names(),
            detail=f"{service!r} did not resolve for tenant {tenant_id}",
        )

    return AvailabilityResponse(
        service=resolved.display_name,
        slots=list(record.offered_slots(resolved.slug)),
    )
