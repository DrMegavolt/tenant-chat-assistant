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
from tenantchat.api.index_integrity import IndexIntegrityStore
from tenantchat.api.jobs import JobStore
from tenantchat.api.registry import TenantRegistry
from tenantchat.api.search import SearchIndex
from tenantchat.api.settings import Settings
from tenantchat.api.storage import ObjectStore
from tenantchat.api.store import (
    AuditStore,
    BookingStore,
    ConsentStore,
    ConversationStore,
    KnowledgeStore,
    LeadStore,
    MembershipStore,
    PrivacyStore,
    TraceAccessStore,
    TurnRecordStore,
)
from tenantchat.core.ports import AvailabilityProvider, BookingService, ConversationRuntime


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


def get_booking_service(request: Request) -> BookingService:
    service: BookingService = request.app.state.booking_service
    return service


def get_availability_provider(request: Request) -> AvailabilityProvider:
    provider: AvailabilityProvider = request.app.state.availability_provider
    return provider


def get_lead_store(request: Request) -> LeadStore:
    store: LeadStore = request.app.state.lead_store
    return store


def get_conversation_store(request: Request) -> ConversationStore:
    store: ConversationStore = request.app.state.conversation_store
    return store


def get_membership_store(request: Request) -> MembershipStore:
    store: MembershipStore = request.app.state.membership_store
    return store


def get_consent_store(request: Request) -> ConsentStore:
    consent: ConsentStore = request.app.state.consent_store
    return consent


def get_privacy_store(request: Request) -> PrivacyStore:
    store: PrivacyStore = request.app.state.privacy_store
    return store


def get_audit_store(request: Request) -> AuditStore:
    store: AuditStore = request.app.state.audit_store
    return store


def get_turn_record_store(request: Request) -> TurnRecordStore:
    store: TurnRecordStore = request.app.state.turn_record_store
    return store


def get_trace_access_store(request: Request) -> TraceAccessStore:
    store: TraceAccessStore = request.app.state.trace_access_store
    return store


def get_job_store(request: Request) -> JobStore:
    store: JobStore = request.app.state.job_store
    return store


def get_knowledge_store(request: Request) -> KnowledgeStore:
    store: KnowledgeStore = request.app.state.knowledge_store
    return store


def get_object_store(request: Request) -> ObjectStore:
    store: ObjectStore = request.app.state.object_store
    return store


def get_generation_findings(request: Request) -> IndexIntegrityStore:
    store: IndexIntegrityStore = request.app.state.generation_findings
    return store


def get_search_index(request: Request) -> SearchIndex | None:
    """The retrieval index, or ``None`` when the deployment composed none.

    ``None`` is a configuration state, not an error: the API can serve uploads
    and findings without the index, and only the surface that must read it (the
    integrity check) refuses.
    """
    index: SearchIndex | None = request.app.state.search_index
    return index


def get_request_id(request: Request) -> str:
    request_id: str = request.state.request_id
    return request_id


Registry = Annotated[TenantRegistry, Depends(get_registry)]
Bookings = Annotated[BookingStore, Depends(get_booking_store)]
BookingActions = Annotated[BookingService, Depends(get_booking_service)]
Availability = Annotated[AvailabilityProvider, Depends(get_availability_provider)]
Leads = Annotated[LeadStore, Depends(get_lead_store)]
Conversations = Annotated[ConversationStore, Depends(get_conversation_store)]
Memberships = Annotated[MembershipStore, Depends(get_membership_store)]
Consent = Annotated[ConsentStore, Depends(get_consent_store)]
Privacy = Annotated[PrivacyStore, Depends(get_privacy_store)]
Audit = Annotated[AuditStore, Depends(get_audit_store)]
TurnRecords = Annotated[TurnRecordStore, Depends(get_turn_record_store)]
TraceAccess = Annotated[TraceAccessStore, Depends(get_trace_access_store)]
Jobs = Annotated[JobStore, Depends(get_job_store)]
Knowledge = Annotated[KnowledgeStore, Depends(get_knowledge_store)]
ObjectStores = Annotated[ObjectStore, Depends(get_object_store)]
GenerationFindings = Annotated[IndexIntegrityStore, Depends(get_generation_findings)]
SearchIndexes = Annotated[SearchIndex | None, Depends(get_search_index)]
Runtime = Annotated[ConversationRuntime, Depends(get_conversation_runtime)]
ComposedRuntime = Annotated[ConversationRuntime | None, Depends(get_composed_runtime)]
Configuration = Annotated[Settings, Depends(get_settings)]
RequestId = Annotated[str, Depends(get_request_id)]
