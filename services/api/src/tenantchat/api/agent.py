"""Composition root for the agent runtime.

One of the three places `ADR-0001` allows LangGraph: this module names the
adapters, wraps the stores in the idempotent services from
:mod:`tenantchat.api.actions`, and hands the result to the graph. Nothing else in
``services/api`` imports the framework, and
``tests/test_architecture_invariants.py`` enforces that by scanning everything
except the exemptions listed there.

The chat routes reach the runtime through
:class:`~tenantchat.core.ports.ConversationRuntime`, and
:class:`GraphConversationRuntime` below is the only thing that satisfies it. The
translation is small on purpose: it exists so the boundary is a value type from
the domain package rather than a LangGraph-shaped one, which is what keeps the
handlers testable without the framework and honest about what they depend on.

`AI-001` supplies the :class:`~tenantchat.orchestration.model.ChatModel` adapter
— this package deliberately contains no provider client — so a deployment
without one composes no runtime and answers no chat turns.
"""

from __future__ import annotations

from collections.abc import Mapping

from tenantchat.api.actions import (
    RecordedBookingService,
    RecordedHandoffService,
    RecordedLeadService,
    RecordedWorkflowService,
)
from tenantchat.api.registry import (
    DemoAvailabilityProvider,
    RegistryPolicySource,
    TenantRegistry,
)
from tenantchat.api.store import (
    BookingStore,
    ConsentStore,
    HandoffStore,
    IdempotencyStore,
    LeadStore,
    WorkflowStore,
)
from tenantchat.core.metrics import MetricsReporter
from tenantchat.core.ports import (
    AssistantTurn,
    AvailabilityProvider,
    CommittedEffect,
    EvidenceSource,
)
from tenantchat.core.routing import ROUTING_POLICY
from tenantchat.orchestration.agents import DEFAULT_AGENT_REGISTRY
from tenantchat.orchestration.checkpoints import Checkpointer
from tenantchat.orchestration.dependencies import DispatchDependencies
from tenantchat.orchestration.graph import compile_dispatch_graph
from tenantchat.orchestration.model import ChatModel
from tenantchat.orchestration.runtime import DispatchRuntime, TurnResult


def build_dispatch_dependencies(
    *,
    registry: TenantRegistry,
    model: ChatModel,
    bookings: BookingStore,
    leads: LeadStore,
    handoffs: HandoffStore,
    idempotency: IdempotencyStore,
    consent: ConsentStore,
    workflows: WorkflowStore,
    availability: AvailabilityProvider | None = None,
    evidence: EvidenceSource | None = None,
    metrics: MetricsReporter | None = None,
) -> DispatchDependencies:
    """Wrap this service's adapters in the ports the graph runs against.

    ``availability`` defaults to the in-process demo provider so a test harness
    can omit it; the production composition passes the database-backed provider
    explicitly rather than silently falling back to a fake.

    ``evidence`` is the `RAG-005` retrieval port. It is optional on purpose: a
    deployment without it answers as the pre-`RAG-005` graph did, with no
    abstention and no citations, so the feature adds risk only where it is
    composed in.

    ``metrics`` is the `OBS-002` metrics port. ``None`` is a harness that
    observes nothing; the production composition passes the Prometheus
    adapter.
    """
    source = availability or DemoAvailabilityProvider(registry)
    return DispatchDependencies(
        model=model,
        policies=RegistryPolicySource(registry),
        availability=source,
        bookings=RecordedBookingService(bookings, source, consent, metrics=metrics),
        leads=RecordedLeadService(leads, idempotency, consent, metrics=metrics),
        handoffs=RecordedHandoffService(handoffs, idempotency, metrics=metrics),
        workflows=RecordedWorkflowService(workflows),
        routing=ROUTING_POLICY,
        agents=DEFAULT_AGENT_REGISTRY,
        evidence=evidence,
        metrics=metrics,
    )


def build_dispatch_runtime(
    *,
    registry: TenantRegistry,
    model: ChatModel,
    bookings: BookingStore,
    leads: LeadStore,
    handoffs: HandoffStore,
    idempotency: IdempotencyStore,
    consent: ConsentStore,
    workflows: WorkflowStore,
    checkpointer: Checkpointer,
    availability: AvailabilityProvider | None = None,
    evidence: EvidenceSource | None = None,
    metrics: MetricsReporter | None = None,
) -> DispatchRuntime:
    """Build the runtime one deployment will serve conversations from."""
    dependencies = build_dispatch_dependencies(
        registry=registry,
        model=model,
        bookings=bookings,
        leads=leads,
        handoffs=handoffs,
        idempotency=idempotency,
        availability=availability,
        consent=consent,
        workflows=workflows,
        evidence=evidence,
        metrics=metrics,
    )
    return DispatchRuntime(compile_dispatch_graph(dependencies, checkpointer))


def _turn(result: TurnResult) -> AssistantTurn:
    return AssistantTurn(
        answer=result.answer,
        committed=tuple(
            CommittedEffect(
                action=action["action"],
                reference=action["reference"],
                replayed=action["replayed"],
            )
            for action in result.committed
        ),
        pending=result.pending,
        model_name=result.model_name,
        graph_version=result.graph_version,
        prompt_version=result.prompt_version,
        citations=result.citations,
        citation_invalid=result.citation_invalid,
        refused_tools=result.refused_tools,
        claims_invalid=result.claims_invalid,
        retrieval=result.retrieval,
        trace=result.trace,
    )


class GraphConversationRuntime:
    """Serves :class:`~tenantchat.core.ports.ConversationRuntime` from the graph.

    The one place a LangGraph turn becomes a domain value. Everything upstream —
    routers, schemas, tests — sees only the port.
    """

    def __init__(self, runtime: DispatchRuntime) -> None:
        self._runtime = runtime

    async def send(self, tenant_id: str, session_id: str, message: str) -> AssistantTurn:
        """Deliver a visitor message and run until an answer or a question.

        Raises:
            ValueError: the identifiers cannot name a checkpoint thread.
        """
        return _turn(await self._runtime.send(tenant_id, session_id, message))

    async def resume(self, tenant_id: str, session_id: str, *, approved: bool) -> AssistantTurn:
        """Answer the pending question and run the turn to completion.

        Raises:
            ValueError: the identifiers cannot name a checkpoint thread.
        """
        return _turn(await self._runtime.resume(tenant_id, session_id, approved))

    async def pending(self, tenant_id: str, session_id: str) -> Mapping[str, object] | None:
        """The question this conversation is waiting on, if any."""
        return await self._runtime.pending(tenant_id, session_id)


def build_conversation_runtime(
    *,
    registry: TenantRegistry,
    model: ChatModel,
    bookings: BookingStore,
    leads: LeadStore,
    handoffs: HandoffStore,
    idempotency: IdempotencyStore,
    consent: ConsentStore,
    workflows: WorkflowStore,
    checkpointer: Checkpointer,
    availability: AvailabilityProvider | None = None,
    evidence: EvidenceSource | None = None,
    metrics: MetricsReporter | None = None,
) -> GraphConversationRuntime:
    """Build the runtime the HTTP layer serves conversations from."""
    return GraphConversationRuntime(
        build_dispatch_runtime(
            registry=registry,
            model=model,
            bookings=bookings,
            leads=leads,
            handoffs=handoffs,
            idempotency=idempotency,
            consent=consent,
            workflows=workflows,
            checkpointer=checkpointer,
            availability=availability,
            evidence=evidence,
            metrics=metrics,
        )
    )
