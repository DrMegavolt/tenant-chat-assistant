"""Request-scoped access to the objects wired up at startup.

The registry and stores are built once in the composition root
(:func:`tenantchat.api.app.create_app`) and reached through these dependencies,
so a test can substitute a store without importing the module that constructs
the real one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from tenantchat.api.registry import TenantRegistry
from tenantchat.api.store import BookingStore, LeadStore


def get_registry(request: Request) -> TenantRegistry:
    registry: TenantRegistry = request.app.state.registry
    return registry


def get_booking_store(request: Request) -> BookingStore:
    store: BookingStore = request.app.state.booking_store
    return store


def get_lead_store(request: Request) -> LeadStore:
    store: LeadStore = request.app.state.lead_store
    return store


def get_request_id(request: Request) -> str:
    request_id: str = request.state.request_id
    return request_id


Registry = Annotated[TenantRegistry, Depends(get_registry)]
Bookings = Annotated[BookingStore, Depends(get_booking_store)]
Leads = Annotated[LeadStore, Depends(get_lead_store)]
RequestId = Annotated[str, Depends(get_request_id)]
