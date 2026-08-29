"""A whole dispatch graph with no model, no database, and no network.

The graph-level safety behaviors — what the record shows when a model call
fails, what a visitor receives when the model leaks tool-call syntax, what
survives a cancelled run — are only honest when tested against the real nodes
and the real runtime, so these tests compile the actual graph over fakes
rather than driving `DispatchNodes` directly.

The model is scripted, and the ports are the smallest fakes that satisfy the
`tenantchat.core.ports` Protocols: a policy source with one tenant, a workflow
service that records nothing, and a handoff service that keeps the commands it
was asked to open. Anything a flow should never reach raises, so a routing
change that drags a test into an unintended effect fails loudly instead of
passing quietly.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from tenantchat.core.budgets import TenantBudget
from tenantchat.core.catalog import ServiceCatalog, ServiceDefinition
from tenantchat.core.commands import BookingCommand, HandoffCommand, LeadCommand
from tenantchat.core.ports import (
    BookingConfirmation,
    HandoffTicket,
    IdempotencyKey,
    LeadReceipt,
    RoutingRecord,
)
from tenantchat.core.routing import (
    ROUTING_POLICY,
    IntentName,
    RoutingDecision,
)
from tenantchat.core.slots import OfferedSlot
from tenantchat.core.tenant import PricingPolicy, TenantPolicy
from tenantchat.core.workflows import (
    ToolResult,
    WorkflowState,
    WorkflowStatus,
    WorkflowTransition,
    transition_workflow,
)
from tenantchat.orchestration.agents import DEFAULT_AGENT_REGISTRY
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.dependencies import DispatchDependencies
from tenantchat.orchestration.graph import compile_dispatch_graph
from tenantchat.orchestration.model import AssembledPrompt, MessageRole, ModelResponse, ToolSpec
from tenantchat.orchestration.runtime import DispatchRuntime

TENANT_ID = "clearview"

TENANT_POLICY = TenantPolicy(
    tenant_id=TENANT_ID,
    name="Clearview Property Care",
    assistant_name="Clearview assistant",
    tagline="Pricing and booking enabled",
    phone="(555) 816-4420",
    address="480 Lakeview Avenue, Portland, OR 97205",
    hours="Daily 7:00 AM-7:00 PM",
    catalog=ServiceCatalog.from_definitions(
        [ServiceDefinition("hvac", "HVAC", frozenset({"heating", "cooling"}))]
    ),
    pricing_policy=PricingPolicy.FIXED,
    booking_enabled=True,
    lead_capture_enabled=True,
    proactive_lead_capture=True,
    served_zips=frozenset({"97205"}),
)

CLAIM_REFUSAL_REPLY = (
    "I cannot confirm some of the details in what I was about to say, so I "
    "will not say it. The team can confirm it — call (555) 816-4420."
)
LEAK_REFUSAL_REPLY = (
    "I was not able to finish that reply. Please ask again, or call (555) 816-4420."
)
UNCOMMITTED_PROMISE_REFUSAL = (
    "I am not able to promise a callback right now. The team can still help — "
    "please call (555) 816-4420, or try again with your name and contact details "
    "so I can submit your request."
)
ESCALATION_REPLY = (
    "I am not able to finish this myself, so I have passed it to the team. "
    "You can also reach them on (555) 816-4420."
)


class ProviderRejectedHistoryError(AssertionError):
    """The scripted model refused a history a real provider would reject."""


class ScriptedModel:
    """Replays a fixed list of responses, then repeats the last one.

    ``failure`` raises on every call, before the script is consulted, so a
    test can turn the provider into a socket error or a cancellation without
    changing the script. ``strict_history`` makes the model check what a real
    provider enforces — every tool call in the history already has its result
    — which is how a transcript left dangling by a crash is made to fail the
    way production would. ``hang`` is a call that never returns, so a test can
    cancel the request task the way an HTTP server cancels a disconnected
    visitor's.
    """

    def __init__(
        self,
        script: list[ModelResponse],
        *,
        failure: BaseException | None = None,
        strict_history: bool = False,
        hang: bool = False,
    ) -> None:
        self.script = script
        self.failure = failure
        self.strict_history = strict_history
        self.hang = hang
        self.calls: list[AssembledPrompt] = []

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        del tools
        self.calls.append(prompt)
        if self.hang:
            await asyncio.Event().wait()
        if self.strict_history:
            unanswered = self.unanswered_tool_calls(prompt)
            if unanswered:
                raise ProviderRejectedHistoryError(
                    f"provider would reject unanswered tool calls: {sorted(unanswered)}"
                )
        if self.failure is not None:
            raise self.failure
        index = min(len(self.calls) - 1, len(self.script) - 1)
        return self.script[index]

    @staticmethod
    def unanswered_tool_calls(prompt: AssembledPrompt) -> set[str]:
        """Call ids in the prompt's history that no tool result has answered."""
        open_calls: set[str] = set()
        for message in prompt.messages:
            if message.role is MessageRole.ASSISTANT:
                open_calls.update(call.call_id for call in message.tool_calls)
            elif message.role is MessageRole.TOOL and message.tool_call_id is not None:
                open_calls.discard(message.tool_call_id)
        return open_calls

    @property
    def call_count(self) -> int:
        return len(self.calls)


class _StaticPolicies:
    """One tenant, always the same policy."""

    def __init__(self, policy: TenantPolicy) -> None:
        self._policy = policy

    async def policy(self, tenant_id: str) -> TenantPolicy:
        del tenant_id
        return self._policy


class _RecordingHandoffs:
    """Keeps every handoff command it was asked to open, for the ticket text."""

    def __init__(self) -> None:
        self.commands: list[HandoffCommand] = []

    async def request(
        self,
        command: HandoffCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> HandoffTicket:
        del session_id, idempotency_key
        self.commands.append(command)
        return HandoffTicket(
            reference=f"h-{len(self.commands)}", reason=command.reason, replayed=False
        )


class _StaticAvailability:
    """Offers the slots a test installs, or raises — a down calendar is how a
    tools run is made to crash mid-superstep, the state a killed process
    leaves behind."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.slots: tuple[OfferedSlot, ...] = ()

    async def offered_slots(self, tenant_id: str, service_slug: str) -> tuple[OfferedSlot, ...]:
        del tenant_id, service_slug
        if self.error is not None:
            raise self.error
        return self.slots


def demo_slot(service_slug: str = "hvac") -> OfferedSlot:
    """One future, bookable slot with a stable id, for booking flows."""
    start = datetime.now(UTC).replace(hour=9, minute=0, second=0, microsecond=0) + timedelta(days=7)
    return OfferedSlot(
        id="slot-demo-1", service_slug=service_slug, start=start, end=start + timedelta(hours=1)
    )


class _MemoryWorkflows:
    """An in-memory workflow record driven by the domain's own state machine.

    Just enough for a booking pause: the router can start a workflow, the
    tools node can update it, and the confirmation nodes can pause and resume
    it. Nothing here is durable — these tests are about the turn, not the
    record — but the transitions are the real ones, so a flow that asks for an
    illegal transition fails the same way production would.
    """

    def __init__(self) -> None:
        self._by_session: dict[tuple[str, str], WorkflowState] = {}

    async def current(self, tenant_id: str, session_id: str) -> WorkflowState | None:
        return self._by_session.get((tenant_id, session_id))

    async def last_routing(self, tenant_id: str, session_id: str) -> RoutingRecord | None:
        del tenant_id, session_id
        return None

    async def record_routing(
        self,
        *,
        tenant_id: str,
        session_id: str,
        decision: RoutingDecision,
        agent_version: str,
        turn_index: int,
        idempotency_key: IdempotencyKey,
    ) -> None:
        del tenant_id, session_id, decision, agent_version, turn_index, idempotency_key

    async def start(
        self,
        *,
        tenant_id: str,
        session_id: str,
        intent: IntentName,
        agent_version: str,
        next_allowed_actions: tuple[str, ...],
        turn_index: int,
        idempotency_key: IdempotencyKey,
    ) -> WorkflowState:
        del idempotency_key
        existing = self._by_session.get((tenant_id, session_id))
        if existing is not None:
            return existing
        now = datetime.now(UTC)
        state = WorkflowState(
            workflow_id=f"wf-{session_id}-{turn_index}",
            tenant_id=tenant_id,
            session_id=session_id,
            intent=intent,
            agent_version=agent_version,
            status=WorkflowStatus.ACTIVE,
            collected_fields={},
            pending_confirmation=None,
            tool_results=(),
            next_allowed_actions=next_allowed_actions,
            turn_index=turn_index,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        self._by_session[(tenant_id, session_id)] = state
        return state

    async def update(
        self,
        *,
        tenant_id: str,
        session_id: str,
        workflow_id: str,
        collected_fields: Mapping[str, str],
        allowed_field_names: tuple[str, ...],
        tool_results: tuple[ToolResult, ...],
        next_allowed_actions: tuple[str, ...],
        turn_index: int,
        idempotency_key: IdempotencyKey,
    ) -> WorkflowState:
        del workflow_id, allowed_field_names, idempotency_key
        state = self._require(tenant_id, session_id)
        answered = {result.call_id for result in state.tool_results}
        merged = replace(
            state,
            collected_fields={**state.collected_fields, **dict(collected_fields)},
            tool_results=state.tool_results
            + tuple(result for result in tool_results if result.call_id not in answered),
            next_allowed_actions=next_allowed_actions,
            turn_index=turn_index,
            updated_at=datetime.now(UTC),
        )
        self._by_session[(tenant_id, session_id)] = merged
        return merged

    async def transition(
        self,
        *,
        tenant_id: str,
        session_id: str,
        workflow_id: str,
        transition: WorkflowTransition,
        payload: Mapping[str, object],
        idempotency_key: IdempotencyKey,
    ) -> WorkflowState:
        del workflow_id, idempotency_key
        state = self._require(tenant_id, session_id)
        moved = transition_workflow(state, transition, payload=payload)
        self._by_session[(tenant_id, session_id)] = moved
        return moved

    def _require(self, tenant_id: str, session_id: str) -> WorkflowState:
        state = self._by_session.get((tenant_id, session_id))
        if state is None:
            raise AssertionError("no workflow was started for this session")
        return state


class _UnusedBookings:
    async def confirm(
        self,
        command: BookingCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> BookingConfirmation:
        del command, session_id, idempotency_key
        raise AssertionError("these flows never commit a booking")

    async def find_replay(
        self, tenant_id: str, idempotency_key: IdempotencyKey
    ) -> BookingConfirmation | None:
        del tenant_id, idempotency_key
        raise AssertionError("these flows never commit a booking")


class _UnusedLeads:
    async def capture(
        self,
        command: LeadCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> LeadReceipt:
        del command, session_id, idempotency_key
        raise AssertionError("these flows never capture a lead")


@dataclass
class Harness:
    """One runtime plus the fakes a test reads back."""

    runtime: DispatchRuntime
    model: ScriptedModel
    handoffs: _RecordingHandoffs
    availability: _StaticAvailability


def build_harness(
    script: list[ModelResponse],
    *,
    policy: TenantPolicy = TENANT_POLICY,
    failure: BaseException | None = None,
    strict_history: bool = False,
    hang: bool = False,
) -> Harness:
    """Compose the real dispatch graph over the fakes, ready for one turn."""
    model = ScriptedModel(script, failure=failure, strict_history=strict_history, hang=hang)
    handoffs = _RecordingHandoffs()
    availability = _StaticAvailability()
    dependencies = DispatchDependencies(
        model=model,
        policies=_StaticPolicies(policy),
        availability=availability,
        bookings=_UnusedBookings(),
        leads=_UnusedLeads(),
        handoffs=handoffs,
        workflows=_MemoryWorkflows(),
        routing=ROUTING_POLICY,
        agents=DEFAULT_AGENT_REGISTRY,
    )
    return Harness(
        runtime=DispatchRuntime(compile_dispatch_graph(dependencies, InMemorySaver())),
        model=model,
        handoffs=handoffs,
        availability=availability,
    )


def overlong_budget_policy() -> TenantPolicy:
    """The tenant policy with an output cap every non-empty answer exceeds."""
    return replace(TENANT_POLICY, budgets=TenantBudget(max_output_chars=1))
