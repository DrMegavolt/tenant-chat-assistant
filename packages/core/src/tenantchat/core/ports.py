"""The only way an agent graph is allowed to change the world.

A graph node is replayed. LangGraph re-executes the nodes after the last
checkpoint whenever a run resumes from an interrupt, a crash, or a deployment,
and a node that books an appointment by writing a row books it again every time.
`ADR-0001` answers that by refusing to put the guard in the node: every effect
goes through one of the services below, each of which takes an
:class:`IdempotencyKey` and is required to return the *original* result when it
sees that key a second time.

The services are ``Protocol`` ports because the domain decides what an effect
means and the service that owns the I/O decides how it lands. Nothing here
implies a database, an HTTP call, or a framework — a test double that satisfies
these signatures is a complete implementation.

Return types say what happened, not what to render. ``replayed`` is the one
piece of execution history worth surfacing: it is how a caller distinguishes "the
booking is confirmed" from "the booking was already confirmed and this was a
retry", and how a test proves a forced replay committed nothing twice.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from tenantchat.core.citations import Citation
from tenantchat.core.commands import BookingCommand, HandoffCommand, HandoffReason, LeadCommand
from tenantchat.core.errors import ValidationError
from tenantchat.core.privacy import ConsentGrant
from tenantchat.core.routing import (
    IntentCandidate,
    IntentName,
    RoutingDecision,
    RoutingOutcome,
    RoutingRule,
)
from tenantchat.core.slots import OfferedSlot
from tenantchat.core.tenant import TenantPolicy
from tenantchat.core.workflows import ToolResult, WorkflowState, WorkflowTransition

# Wide enough for a caller-supplied key from an HTTP `Idempotency-Key` header,
# narrow enough that the value is safe in a URL, a log line, and a SQL literal.
_SUPPLIED_KEY = re.compile(r"\A[A-Za-z0-9._:-]{8,200}\Z")

_DERIVATION_SEPARATOR = "\x00"


@dataclass(frozen=True, slots=True)
class IdempotencyKey:
    """A stable name for one intended effect.

    Two calls carrying the same key are the same attempt at the same action, and
    the second must not commit anything. That makes the *derivation* the load
    bearing part: a key generated inside a node is fresh on every replay and
    guarantees nothing. Build one from values the checkpoint already holds — the
    session, the action, the turn — via :meth:`derive`.
    """

    value: str

    @classmethod
    def parse(cls, raw: str) -> IdempotencyKey:
        """Accept a key supplied by a caller.

        Raises:
            ValidationError: the key is too short, too long, or contains
                characters outside ``[A-Za-z0-9._:-]``.
        """
        candidate = raw.strip()
        if not _SUPPLIED_KEY.fullmatch(candidate):
            raise ValidationError(
                detail=f"idempotency key of length {len(candidate)} is not 8-200 safe characters"
            )
        return cls(candidate)

    @classmethod
    def derive(cls, *parts: str) -> IdempotencyKey:
        """Derive a key from the inputs that identify this attempt.

        The parts are hashed rather than concatenated for two reasons. They are
        arbitrary-length — a lead summary is one of them — and a key is a natural
        thing to log, so a readable key would carry the customer's own words into
        the operational plane that `ADR-0010` keeps content out of.

        Raises:
            ValueError: no parts were supplied, which would derive one key for
                every action in the system.
        """
        if not parts:
            raise ValueError("an idempotency key needs at least one distinguishing part")
        joined = _DERIVATION_SEPARATOR.join(parts)
        return cls(hashlib.sha256(joined.encode("utf-8")).hexdigest())

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class BookingConfirmation:
    """A booking that is committed and can be quoted back to the customer.

    Carries the customer-facing echo (service name, contact, address) so that a
    replay of a committed key can re-present the *original* confirmation without
    re-validating the request — the slot a replay names was already taken by
    that same attempt, so it is no longer offered.
    """

    reference: str
    service_slug: str
    service_name: str
    slot: str
    customer_name: str
    contact: str
    address: str
    created_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class LeadReceipt:
    """A captured follow-up request, ready for a human to work."""

    reference: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class HandoffTicket:
    """An open request for a person to take the conversation over."""

    reference: str
    reason: HandoffReason
    replayed: bool


class BookingService(Protocol):
    """Commits bookings. `DATA-003` adds the calendar reservation behind this."""

    async def confirm(
        self,
        command: BookingCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> BookingConfirmation:
        """Book the slot the command names, exactly once per key.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
            ConflictError: a different action already used this key.
        """
        ...

    async def find_replay(
        self, tenant_id: str, idempotency_key: IdempotencyKey
    ) -> BookingConfirmation | None:
        """Return the committed confirmation if this key already booked one.

        Checked *before* a caller re-validates the request: a replay names the
        slot the same attempt already took, so it is no longer offered and would
        otherwise be refused as if it were a fresh booking.
        """
        ...


class LeadService(Protocol):
    """Captures follow-up requests."""

    async def capture(
        self,
        command: LeadCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> LeadReceipt:
        """Record the lead, exactly once per key.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
            ConflictError: a different action already used this key.
        """
        ...


class HandoffService(Protocol):
    """Opens human-takeover requests. `FEAT-004` builds the staff side."""

    async def request(
        self,
        command: HandoffCommand,
        *,
        session_id: str,
        idempotency_key: IdempotencyKey,
    ) -> HandoffTicket:
        """Open a handoff, exactly once per key.

        Raises:
            NotFoundError: the conversation does not belong to this tenant.
            ConflictError: a different action already used this key.
        """
        ...


class TenantPolicySource(Protocol):
    """Where a tenant's server-owned policy comes from.

    A port rather than a direct lookup because `FEAT-006` moves these records out
    of source code and into the database with a draft/publish workflow. The
    caller only ever needs "the policy in force for this tenant right now".
    """

    async def policy(self, tenant_id: str) -> TenantPolicy:
        """The tenant's current policy.

        Raises:
            NotFoundError: no such tenant, worded so it cannot be used to
                enumerate which tenants exist.
        """
        ...


class ConsentSource(Protocol):
    """What a session has agreed to, read where the effect happens.

    `PRIV-001` gates every effect that stores contact data on a grant recorded
    for that session. The gate lives in the idempotent services, so a replayed
    node re-checks the same grant rather than re-recording it; this port is how
    they read the grant without knowing where it lives.
    """

    async def consent_grant(self, tenant_id: str, session_id: str) -> ConsentGrant:
        """The grant recorded for this session, empty when none was.

        Never raises for a missing grant: an absent grant is a refusal, which
        is the normal state before the visitor agrees.
        """
        ...


@dataclass(frozen=True, slots=True)
class CommittedEffect:
    """A domain action one conversation has already caused."""

    action: str
    reference: str
    replayed: bool


@dataclass(frozen=True, slots=True)
class AssistantTurn:
    """What one conversation turn produced.

    ``pending`` is the question the runtime stopped to ask, and its presence
    means ``answer`` is not final: nothing has been committed for it yet and the
    turn finishes only when :meth:`ConversationRuntime.resume` supplies a
    decision. Its contents are whatever the runtime needs answered, so it may
    quote the customer — it belongs in a response to that customer and in the
    inference plane, never in a log or a metric label.

    The three version fields pin the answer to the components that produced it,
    which is what `OBS-004` reconstructs a turn from.

    ``citations`` are the grounded claims' sources, built only from the
    evidence that was in the model's context (`RAG-005`). ``citation_invalid``
    lists the source identifiers the model wrote that were not in that context
    — the verdict an operator and the inference plane need; the public schema
    never publishes them. ``retrieval`` is the safe metadata of the retrieval
    that grounded the turn (sufficiency verdict and retriever versions), for
    the inference plane only.

    ``trace`` is the `OBS-004` inference trace of the turn as JSON-safe data:
    the router decision, the retrieval that ran, the assembled prompt
    reference and content hash, model usage, verdicts, tool effects, the
    component manifest, and the auto-detected diagnoses. It is content and
    belongs to the inference plane only, exactly like ``pending``.
    """

    answer: str
    committed: tuple[CommittedEffect, ...]
    pending: Mapping[str, object] | None
    model_name: str
    graph_version: str
    prompt_version: str
    citations: tuple[Citation, ...] = ()
    citation_invalid: tuple[str, ...] = ()
    retrieval: Mapping[str, object] | None = None
    trace: Mapping[str, object] | None = None

    @property
    def is_paused(self) -> bool:
        return self.pending is not None


class ConversationRuntime(Protocol):
    """Runs assistant conversations, one visitor message at a time.

    A port rather than a direct call because the runtime is an agent framework
    and `ADR-0001` keeps that framework out of everything except orchestration,
    the checkpoint adapter, and the composition root. An HTTP handler written
    against this Protocol is testable, and stays written when the runtime behind
    it is replaced.

    Conversation identity is the caller's: ``session_id`` names a conversation
    the caller has already authorized, and the runtime neither issues nor
    validates it.
    """

    async def send(self, tenant_id: str, session_id: str, message: str) -> AssistantTurn:
        """Deliver a visitor message and run until an answer or a question.

        Raises:
            ValueError: the identifiers cannot name a conversation.
        """
        ...

    async def resume(self, tenant_id: str, session_id: str, *, approved: bool) -> AssistantTurn:
        """Answer the pending question and run the turn to completion.

        Resuming a conversation with nothing pending is not an error: it runs
        the graph forward from wherever it stopped, which for a finished turn
        changes nothing.

        Raises:
            ValueError: the identifiers cannot name a conversation.
        """
        ...

    async def pending(self, tenant_id: str, session_id: str) -> Mapping[str, object] | None:
        """The question this conversation is waiting on, if any.

        Lets a returning visitor be shown the confirmation they abandoned rather
        than a conversation that appears to have stopped mid-sentence.
        """
        ...


class AvailabilityProvider(Protocol):
    """What a tenant is currently offering for one service.

    Separate from :class:`BookingService` because reading availability commits
    nothing and needs no idempotency key. `DATA-003` swaps the seeded label lists
    behind this for a live calendar without the graph noticing.
    """

    async def offered_slots(self, tenant_id: str, service_slug: str) -> tuple[OfferedSlot, ...]:
        """Slots currently bookable for one service, empty when it has none."""
        ...


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One retrieved passage, with everything citation validation needs.

    ``source_id`` is the identifier the model is asked to cite — the index
    chunk id admitted to the prompt context. ``revision`` and ``effective_at``
    are the published version's, resolved by the adapter from the knowledge
    system of record; a version that is no longer retrievable for a visitor is
    not offered as evidence at all (`RAG-005`).

    ``generation_id`` and ``embedding_model`` pin the passage to the index
    generation and embedder that produced it, so `OBS-004` can attribute a
    retrieval regression to an index rebuild or a model change.
    """

    source_id: str
    title: str
    source_name: str
    location: str
    content: str
    document_id: uuid.UUID
    version_id: uuid.UUID
    generation_id: uuid.UUID
    embedding_model: str
    score: float
    revision: int
    effective_at: datetime


@dataclass(frozen=True, slots=True)
class EvidenceBundle:
    """One retrieval result: the passages, the verdict, and what produced them.

    ``retriever_version``, ``reranker``, and ``min_evidence_score`` pin the
    retrieval that grounded the turn, for the inference plane (`OBS-004`);
    they are safe metadata and carry no content. ``filters``, ``budget``, and
    ``retriever_parameters`` are the exact values the adapter ran with, so a
    stored trace can say which filter or budget cut a candidate out.

    ``sufficient`` is the abstention verdict: when it is ``False`` the graph
    refuses to call the model for a tool-less answer instead of letting it
    guess.
    """

    items: tuple[EvidenceItem, ...]
    sufficient: bool
    retriever_version: str
    reranker: str | None
    min_evidence_score: float
    embedding_model: str = ""
    generation_id: uuid.UUID | None = None
    retriever_parameters: Mapping[str, object] = field(default_factory=dict)
    filters: Mapping[str, object] = field(default_factory=dict)
    budget: Mapping[str, object] = field(default_factory=dict)


class EvidenceSource(Protocol):
    """The retrieval a turn may ground itself in, scoped to one tenant.

    Implemented by the application service that owns the index, the embedding
    provider, and the knowledge store; the graph never sees those. Every item
    is tenant-scoped by the adapter at retrieval time, so the passage set a
    citation may reference can never include another tenant's document — the
    graph's validation against :attr:`EvidenceBundle.items` checks the
    adapter's promise rather than hoping for it.
    """

    async def retrieve(self, *, tenant_id: str, query: str) -> EvidenceBundle:
        """Retrieve the passages that may ground one answer.

        The adapter returns only passages a visitor of ``tenant_id`` may be
        told, in rank order, bounded by its configured budget; it also decides
        :attr:`EvidenceBundle.sufficient`.

        Raises:
            EvidenceUnavailableError: retrieval could not run at all. The
                graph treats this as insufficient evidence, so a failed index
                makes the assistant abstain rather than answer ungrounded.
        """
        ...


class EvidenceUnavailableError(Exception):
    """Retrieval could not run for this turn."""


@dataclass(frozen=True, slots=True)
class RoutingRecord:
    """One persisted routing decision, as `OBS-004` will read it.

    The whole decision, not the winner: every candidate with its score, the
    chosen intent, the confidence, the policy version, and the thresholds that
    were applied. ``rule`` is what makes a misroute diagnosable from this
    record alone — whether the correct intent was never a candidate (a
    ``FALLBACK`` or a candidate missing entirely), was scored and lost (present
    in ``candidates`` below the winner), or lost to a threshold (``CLARIFY``
    with ``chosen=None``).
    """

    turn_index: int
    policy_version: str
    agent_version: str
    outcome: RoutingOutcome
    rule: RoutingRule
    chosen_intent: IntentName | None
    confidence: float
    candidates: tuple[IntentCandidate, ...]
    direct_threshold: float
    clarify_threshold: float
    conflict_gap: float
    created_at: datetime


class WorkflowService(Protocol):
    """The durable record of routing decisions and agent workflows.

    `ADR-0001` keeps the system of record in domain tables, so this service
    exists: every routing decision, workflow, and workflow transition is written
    here and outlives any checkpoint. The graph is the driver and the
    checkpoint is the resume point; this service is what a restart cannot lose.

    Every mutation takes an :class:`IdempotencyKey` derived from checkpoint
    values and returns the original result on replay, so a node that runs again
    after a crash records nothing twice.
    """

    async def current(self, tenant_id: str, session_id: str) -> WorkflowState | None:
        """The session's active workflow (``ACTIVE`` or ``PAUSED``), if any.

        Read-only, so no idempotency key: the router uses it as the previous
        intent for continuation decisions.
        """
        ...

    async def last_routing(self, tenant_id: str, session_id: str) -> RoutingRecord | None:
        """The most recent routing decision for the session, if any.

        Read-only. The router uses it to bound clarification: a second
        consecutive ambiguity hands off instead of asking again.
        """
        ...

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
        """Persist one routing decision. Replays rewrite nothing.

        The natural key is ``(session, turn_index)``; the idempotency key is
        derived from the same parts, so a replayed route node lands on the same
        row it already wrote. ``agent_version`` pins which agent registry the
        decision dispatched under.
        """
        ...

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
        """Open an ``ACTIVE`` workflow for the intent.

        Exactly one active workflow may exist per session; starting again while
        one is active returns it rather than creating a rival.
        """
        ...

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
        """Merge one turn's collected fields, tool results, and allowed actions.

        Content-merge semantics make a replay write nothing twice: fields are
        overwritten per name, tool results per call ID.

        Raises:
            ValidationError: a collected field name is not in
                ``allowed_field_names``.
            NotFoundError: no workflow with that ID for this tenant and session.
        """
        ...

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

        ``PAUSE`` persists the pending confirmation from ``payload``; every
        other transition clears it.

        Raises:
            WorkflowTransitionError: the transition is not permitted from the
                workflow's current status.
            NotFoundError: no workflow with that ID for this tenant and session.
        """
        ...
