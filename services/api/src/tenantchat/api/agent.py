"""Composition root for the agent runtime.

One of the three places `ADR-0001` allows LangGraph: this module names the
adapters, wraps the stores in the idempotent services from
:mod:`tenantchat.api.actions`, and hands the result to the graph. Nothing else in
``services/api`` imports the framework, and
``tests/test_architecture_invariants.py`` enforces that by scanning everything
except the exemptions listed there.

The runtime is built but not yet served. `AI-001` supplies the
:class:`~tenantchat.orchestration.model.ChatModel` adapter — this package
deliberately contains no provider client — and `API-001` exposes the chat
endpoint over it. Assembling it here now is what makes the wiring testable, and
what keeps the graph honest about which adapters it is actually reachable from.
"""

from __future__ import annotations

from tenantchat.api.actions import (
    RecordedBookingService,
    RecordedHandoffService,
    RecordedLeadService,
)
from tenantchat.api.registry import (
    RegistryAvailabilityProvider,
    RegistryPolicySource,
    TenantRegistry,
)
from tenantchat.api.store import BookingStore, HandoffStore, IdempotencyStore, LeadStore
from tenantchat.orchestration.checkpoints import Checkpointer
from tenantchat.orchestration.dependencies import DispatchDependencies
from tenantchat.orchestration.graph import compile_dispatch_graph
from tenantchat.orchestration.model import ChatModel
from tenantchat.orchestration.runtime import DispatchRuntime


def build_dispatch_dependencies(
    *,
    registry: TenantRegistry,
    model: ChatModel,
    bookings: BookingStore,
    leads: LeadStore,
    handoffs: HandoffStore,
    idempotency: IdempotencyStore,
) -> DispatchDependencies:
    """Wrap this service's adapters in the ports the graph runs against."""
    return DispatchDependencies(
        model=model,
        policies=RegistryPolicySource(registry),
        availability=RegistryAvailabilityProvider(registry),
        bookings=RecordedBookingService(bookings, idempotency),
        leads=RecordedLeadService(leads, idempotency),
        handoffs=RecordedHandoffService(handoffs, idempotency),
    )


def build_dispatch_runtime(
    *,
    registry: TenantRegistry,
    model: ChatModel,
    bookings: BookingStore,
    leads: LeadStore,
    handoffs: HandoffStore,
    idempotency: IdempotencyStore,
    checkpointer: Checkpointer,
) -> DispatchRuntime:
    """Build the runtime one deployment will serve conversations from."""
    dependencies = build_dispatch_dependencies(
        registry=registry,
        model=model,
        bookings=bookings,
        leads=leads,
        handoffs=handoffs,
        idempotency=idempotency,
    )
    return DispatchRuntime(compile_dispatch_graph(dependencies, checkpointer))
