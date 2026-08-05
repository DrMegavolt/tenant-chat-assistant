"""Request-scoped access to the objects wired up at startup.

The registry and stores are built once in the composition root
(:func:`tenantchat.api.app.create_app`) and reached through these dependencies,
so a test can substitute a store without importing the module that constructs
the real one.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from tenantchat.api.faults import ChatUnavailableError
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.settings import Settings
from tenantchat.api.store import (
    AuditStore,
    BookingStore,
    ConversationStore,
    LeadStore,
    MembershipStore,
)
from tenantchat.core.ports import ConversationRuntime


def get_registry(request: Request) -> TenantRegistry:
    registry: TenantRegistry = request.app.state.registry
    return registry


def get_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_composed_runtime(request: Request) -> ConversationRuntime | None:
    """The agent runtime, or ``None`` when this deployment composed none.

    For routes that can still do their job without one — reading a transcript
    does not need a model.
    """
    runtime: ConversationRuntime | None = request.app.state.conversation_runtime
    return runtime


def get_conversation_runtime(request: Request) -> ConversationRuntime:
    """The composed agent runtime.

    Raises:
        ChatUnavailableError: this deployment composed no runtime, because
            `AI-001` has not supplied the model adapter it is built over.
    """
    runtime = get_composed_runtime(request)
    if runtime is None:
        raise ChatUnavailableError
    return runtime


def get_booking_store(request: Request) -> BookingStore:
    store: BookingStore = request.app.state.booking_store
    return store


def get_lead_store(request: Request) -> LeadStore:
    store: LeadStore = request.app.state.lead_store
    return store


def get_conversation_store(request: Request) -> ConversationStore:
    store: ConversationStore = request.app.state.conversation_store
    return store


def get_membership_store(request: Request) -> MembershipStore:
    store: MembershipStore = request.app.state.membership_store
    return store


def get_audit_store(request: Request) -> AuditStore:
    store: AuditStore = request.app.state.audit_store
    return store


def get_request_id(request: Request) -> str:
    request_id: str = request.state.request_id
    return request_id


Registry = Annotated[TenantRegistry, Depends(get_registry)]
Bookings = Annotated[BookingStore, Depends(get_booking_store)]
Leads = Annotated[LeadStore, Depends(get_lead_store)]
Conversations = Annotated[ConversationStore, Depends(get_conversation_store)]
Memberships = Annotated[MembershipStore, Depends(get_membership_store)]
Audit = Annotated[AuditStore, Depends(get_audit_store)]
Runtime = Annotated[ConversationRuntime, Depends(get_conversation_runtime)]
ComposedRuntime = Annotated[ConversationRuntime | None, Depends(get_composed_runtime)]
Configuration = Annotated[Settings, Depends(get_settings)]
RequestId = Annotated[str, Depends(get_request_id)]
