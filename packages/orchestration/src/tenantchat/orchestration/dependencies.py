"""Everything the graph is allowed to reach.

One frozen bundle rather than six constructor arguments threaded through every
node, and — more usefully — an exhaustive list of the graph's capabilities. If a
node wants to do something that is not reachable from this object, the reviewer
sees a new field rather than a new import.

Every field is a ``Protocol`` from :mod:`tenantchat.core.ports` or
:mod:`tenantchat.orchestration.model`. The graph therefore has no idea whether it
is running against PostgreSQL and a hosted model or against six test doubles,
which is what makes the whole runtime testable without either.
"""

from __future__ import annotations

from dataclasses import dataclass

from tenantchat.core.metrics import MetricsReporter
from tenantchat.core.ports import (
    AvailabilityProvider,
    BookingService,
    EvidenceSource,
    HandoffService,
    LeadService,
    TenantPolicySource,
    WorkflowService,
)
from tenantchat.core.routing import RoutingPolicy
from tenantchat.orchestration.agents import AgentRegistry
from tenantchat.orchestration.model import ChatModel


@dataclass(frozen=True, slots=True)
class DispatchDependencies:
    """The ports one dispatcher graph runs against."""

    model: ChatModel
    policies: TenantPolicySource
    availability: AvailabilityProvider
    bookings: BookingService
    leads: LeadService
    handoffs: HandoffService
    workflows: WorkflowService
    routing: RoutingPolicy
    agents: AgentRegistry
    # `None` is a deployment that composed no retrieval: the graph then runs
    # exactly as before `RAG-005`, with no evidence, no abstention, and no
    # citations. A composition with a retrieval adapter never passes `None`.
    evidence: EvidenceSource | None = None
    # `None` is a composition that observes nothing: unit-test harnesses omit
    # it. Recording is an observation, not an effect — a replayed node re-observes
    # the work it re-executed — which is why the exactly-once business counts
    # are recorded by the idempotent services instead (`OBS-002`).
    metrics: MetricsReporter | None = None
