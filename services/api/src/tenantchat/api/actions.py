"""Idempotent application services: the only way the graph changes anything.

`ADR-0001` puts authorization, validation, transactions, and idempotency here
rather than in a graph node, because a node is replayed and these operations
must not be. Each service claims the caller's idempotency key, performs the
write once, and returns the original result to every later attempt carrying the
same key.

The fingerprint guards the other direction. A key identifies an *attempt*, so
the same key arriving with materially different content is a caller bug — a
recycled key, a mutated command — and committing the second version under the
first one's identity would silently lose one of them. It is a hash rather than
the content because the fingerprint is stored, and the content includes a
customer's name, address, and phone number.

Nothing in this module imports the agent framework, and nothing in it is
specific to one. A worker or an HTTP handler calls the same services with a
key of its own.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from typing import Final

from tenantchat.api.store import (
    BookingAttempt,
    BookingRecord,
    BookingStore,
    ConsentStore,
    HandoffStore,
    IdempotencyStore,
    LeadStore,
    RoutingRow,
    WorkflowStore,
    _row_state,
)
from tenantchat.core.commands import (
    BookingCommand,
    ConsentGatedCommand,
    HandoffCommand,
    LeadCommand,
)
from tenantchat.core.errors import DomainError, SlotUnavailableError, ValidationError
from tenantchat.core.metrics import ActionStatus, MetricName, MetricsReporter, Operation
from tenantchat.core.ports import (
    AvailabilityProvider,
    BookingConfirmation,
    ConsentSource,
    HandoffTicket,
    IdempotencyKey,
    LeadReceipt,
    RoutingRecord,
)
from tenantchat.core.routing import IntentName, RoutingDecision
from tenantchat.core.workflows import ToolResult, WorkflowState, WorkflowTransition

BOOKING_SCOPE: Final = "booking"
LEAD_SCOPE: Final = "lead"
HANDOFF_SCOPE: Final = "handoff"

_SEPARATOR: Final = "\x00"


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256(_SEPARATOR.join(parts).encode("utf-8")).hexdigest()


async def _require_consent(
    consent: ConsentSource, command: ConsentGatedCommand, session_id: str
) -> None:
    """Refuse the action unless the session holds the consent the command needs.

    Checked before the idempotency claim, so a refusal leaves no claimed key
    behind and a retry after the visitor consents starts clean. The command
    carries the required purposes from the policy it was parsed against.
    """
    grant = await consent.consent_grant(command.tenant_id, session_id)
    grant.require(*command.require_consent)


class RecordedBookingService:
    """Commits a booking once per idempotency key.

    The claim, the slot reservation, and the booking write all happen inside the
    store's single transaction (`DATA-003`), so there is no window where a
    crashed attempt makes a retry wait. This service supplies the attempt's
    fingerprint and, when the reservation fails because a slot was just taken,
    refreshes the offered alternatives so the caller can re-prompt in one turn.

    Consent is checked before the claim, so a refusal leaves no reservation and
    no claimed key behind (`PRIV-001`).

    ``metrics`` is the `OBS-002` business-action recorder. The status is
    recorded *here* rather than in a graph node because this service knows the
    one thing a replayed node cannot: whether the effect was committed once,
    answered a duplicate key, or was refused — the exactly-once business count.
    """

    def __init__(
        self,
        bookings: BookingStore,
        availability: AvailabilityProvider,
        consent: ConsentStore,
        metrics: MetricsReporter | None = None,
    ) -> None:
        self._bookings = bookings
        self._availability = availability
        self._consent = consent
        self._metrics = metrics

    def _record(self, status: ActionStatus, started: float) -> None:
        if self._metrics is None:
            return
        labels = {"operation": Operation.BOOKING.value, "status": status.value}
        self._metrics.observe(MetricName.BUSINESS_ACTIONS, 1, labels=labels)
        self._metrics.observe(
            MetricName.BUSINESS_LATENCY,
            time.monotonic() - started,
            labels={"operation": Operation.BOOKING.value},
        )

    async def confirm(
        self,
        command: BookingCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> BookingConfirmation:
        """Book the slot, or return the booking this key already made.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
            ConflictError: the key was used for a materially different booking.
            ConsentRequiredError: the session has not granted every purpose the
                booking requires.
            SlotUnavailableError: the slot is past, reserved, or wrong tenant,
                carrying the current offers.
        """
        started = time.monotonic()
        try:
            await _require_consent(self._consent, command, session_id)
            attempt = BookingAttempt(
                tenant_id=command.tenant_id,
                scope=BOOKING_SCOPE,
                key=idempotency_key,
                request_hash=_fingerprint(
                    session_id,
                    command.customer_name,
                    command.contact.value,
                    command.address,
                    command.service.slug,
                    command.slot_id,
                ),
            )
            try:
                outcome = await self._bookings.confirm(
                    command, session_id=session_id, attempt=attempt
                )
            except SlotUnavailableError:
                refreshed = tuple(
                    slot.label
                    for slot in await self._availability.offered_slots(
                        command.tenant_id, command.service.slug
                    )
                )
                raise SlotUnavailableError(offered=refreshed) from None
        except DomainError:
            self._record(ActionStatus.REFUSED, started)
            raise
        status = ActionStatus.REPLAYED if outcome.replayed else ActionStatus.COMMITTED
        self._record(status, started)
        return _confirmation(outcome.record, replayed=outcome.replayed)

    async def find_replay(
        self, tenant_id: str, idempotency_key: IdempotencyKey
    ) -> BookingConfirmation | None:
        """Return the original confirmation if this key already booked, else ``None``.

        A caller checks this before re-validating the request: a replay names the
        slot the same attempt already took, so that slot is no longer offered and
        would otherwise be refused as if it were a brand-new booking.
        """
        record = await self._bookings.replay(tenant_id, BOOKING_SCOPE, idempotency_key.value)
        if record is None:
            return None
        return _confirmation(record, replayed=True)


def _confirmation(record: BookingRecord, *, replayed: bool) -> BookingConfirmation:
    """Project a stored booking onto the domain confirmation a replay needs."""
    return BookingConfirmation(
        reference=record.booking_id,
        service_slug=record.service_slug,
        service_name=record.service_name,
        slot=record.slot,
        customer_name=record.customer_name,
        contact=record.contact.display,
        address=record.address,
        created_at=record.created_at,
        replayed=replayed,
    )


class RecordedLeadService:
    """Captures a lead once per idempotency key."""

    def __init__(
        self,
        leads: LeadStore,
        idempotency: IdempotencyStore,
        consent: ConsentStore,
        metrics: MetricsReporter | None = None,
    ) -> None:
        self._leads = leads
        self._idempotency = idempotency
        self._consent = consent
        self._metrics = metrics

    def _record(self, status: ActionStatus, started: float) -> None:
        if self._metrics is None:
            return
        labels = {"operation": Operation.LEAD.value, "status": status.value}
        self._metrics.observe(MetricName.BUSINESS_ACTIONS, 1, labels=labels)
        self._metrics.observe(
            MetricName.BUSINESS_LATENCY,
            time.monotonic() - started,
            labels={"operation": Operation.LEAD.value},
        )

    async def capture(
        self,
        command: LeadCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> LeadReceipt:
        """Record the lead, or return the one this key already recorded.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
            ConsentRequiredError: the session has not granted every purpose the
                lead requires.
            ConflictError: an attempt with this key is in flight, or the key was
                used for a different lead.
        """
        started = time.monotonic()
        try:
            await _require_consent(self._consent, command, session_id)
            replay = await self._idempotency.begin(
                command.tenant_id,
                scope=LEAD_SCOPE,
                key=idempotency_key,
                fingerprint=_fingerprint(
                    session_id,
                    command.customer_name,
                    command.contact.value,
                    command.service,
                    command.summary,
                ),
            )
            if replay is not None:
                self._record(ActionStatus.REPLAYED, started)
                return LeadReceipt(reference=str(replay["reference"]), replayed=True)

            record = await self._leads.record(command, session_id=session_id)
            await self._idempotency.complete(
                command.tenant_id,
                scope=LEAD_SCOPE,
                key=idempotency_key,
                response={"reference": record.lead_id},
            )
        except DomainError:
            self._record(ActionStatus.REFUSED, started)
            raise
        self._record(ActionStatus.COMMITTED, started)
        return LeadReceipt(reference=record.lead_id, replayed=False)


class RecordedHandoffService:
    """Opens a handoff once per idempotency key.

    Duplicate handoffs are cheaper than duplicate bookings, but not free: each
    one lands in a staff queue, and a resumed run that opens a second ticket for
    the same stuck conversation is how that queue stops being trustworthy.
    """

    def __init__(
        self,
        handoffs: HandoffStore,
        idempotency: IdempotencyStore,
        metrics: MetricsReporter | None = None,
    ) -> None:
        self._handoffs = handoffs
        self._idempotency = idempotency
        self._metrics = metrics

    def _record(self, status: ActionStatus, started: float) -> None:
        if self._metrics is None:
            return
        labels = {"operation": Operation.HANDOFF.value, "status": status.value}
        self._metrics.observe(MetricName.BUSINESS_ACTIONS, 1, labels=labels)
        self._metrics.observe(
            MetricName.BUSINESS_LATENCY,
            time.monotonic() - started,
            labels={"operation": Operation.HANDOFF.value},
        )

    async def request(
        self,
        command: HandoffCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> HandoffTicket:
        """Open the handoff, or return the one this key already opened.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
            ConflictError: an attempt with this key is in flight, or the key was
                used for a different handoff.
        """
        started = time.monotonic()
        try:
            replay = await self._idempotency.begin(
                command.tenant_id,
                scope=HANDOFF_SCOPE,
                key=idempotency_key,
                fingerprint=_fingerprint(session_id, command.reason.value, command.summary),
            )
            if replay is not None:
                self._record(ActionStatus.REPLAYED, started)
                return HandoffTicket(
                    reference=str(replay["reference"]), reason=command.reason, replayed=True
                )

            record = await self._handoffs.record(command, session_id=session_id)
            await self._idempotency.complete(
                command.tenant_id,
                scope=HANDOFF_SCOPE,
                key=idempotency_key,
                response={"reference": record.handoff_id},
            )
        except DomainError:
            self._record(ActionStatus.REFUSED, started)
            raise
        self._record(ActionStatus.COMMITTED, started)
        return HandoffTicket(reference=record.handoff_id, reason=record.reason, replayed=False)


class RecordedWorkflowService:
    """The idempotent service behind the router and the workflow state machine.

    `ADR-0001` puts validation and idempotency here rather than in a graph
    node. The validation this service owns: collected fields may only carry
    names the agent's declared input schema permits, and the workflow's state
    machine is the domain's — a transition the machine refuses is an error the
    caller sees, never a row the node writes.

    Replay safety is the store's business (unique constraints on the natural
    and idempotency keys), so every mutation here simply forwards its key.
    """

    def __init__(self, workflows: WorkflowStore) -> None:
        self._workflows = workflows

    async def current(self, tenant_id: str, session_id: str) -> WorkflowState | None:
        """The session's active workflow, if any."""
        row = await self._workflows.current(tenant_id, session_id)
        return _row_state(row) if row is not None else None

    async def last_routing(self, tenant_id: str, session_id: str) -> RoutingRecord | None:
        """The most recent routing decision for the session, if any."""
        row = await self._workflows.last_routing(tenant_id, session_id)
        return _routing_record(row) if row is not None else None

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
        """Persist the whole decision, once per (session, turn)."""
        await self._workflows.record_routing(
            tenant_id=tenant_id,
            session_id=session_id,
            turn_index=turn_index,
            decision=decision,
            agent_version=agent_version,
            idempotency_key=idempotency_key,
        )

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
        """Open the intent's workflow, returning the active one on replay."""
        row = await self._workflows.start(
            tenant_id=tenant_id,
            session_id=session_id,
            intent=intent,
            agent_version=agent_version,
            next_allowed_actions=next_allowed_actions,
            turn_index=turn_index,
            idempotency_key=idempotency_key,
        )
        return _row_state(row)

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
        """Merge one turn's evidence, refusing undeclared field names.

        Raises:
            ValidationError: a collected field name is not in
                ``allowed_field_names``.
            NotFoundError: no workflow with that ID for this tenant and session.
        """
        unknown = sorted(set(collected_fields) - set(allowed_field_names))
        if unknown:
            raise ValidationError(detail=f"workflow update carried undeclared fields {unknown}")
        row = await self._workflows.update(
            tenant_id=tenant_id,
            session_id=session_id,
            workflow_id=workflow_id,
            collected_fields=collected_fields,
            tool_results=tool_results,
            next_allowed_actions=next_allowed_actions,
            turn_index=turn_index,
            idempotency_key=idempotency_key,
        )
        return _row_state(row)

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
        """Move the workflow through the state machine, exactly once per key.

        Raises:
            WorkflowTransitionError: the transition is not permitted from the
                workflow's current status.
            NotFoundError: no workflow with that ID for this tenant and session.
        """
        row = await self._workflows.transition(
            tenant_id=tenant_id,
            session_id=session_id,
            workflow_id=workflow_id,
            transition=transition,
            payload=payload,
            idempotency_key=idempotency_key,
        )
        return _row_state(row)


def _routing_record(row: RoutingRow) -> RoutingRecord:
    return RoutingRecord(
        turn_index=row.turn_index,
        policy_version=row.policy_version,
        agent_version=row.agent_version,
        outcome=row.outcome,
        rule=row.rule,
        chosen_intent=row.chosen_intent,
        confidence=row.confidence,
        candidates=row.candidates,
        direct_threshold=row.direct_threshold,
        clarify_threshold=row.clarify_threshold,
        conflict_gap=row.conflict_gap,
        created_at=row.created_at,
    )
