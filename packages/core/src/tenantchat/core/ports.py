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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from tenantchat.core.commands import BookingCommand, HandoffCommand, HandoffReason, LeadCommand
from tenantchat.core.errors import ValidationError
from tenantchat.core.slots import OfferedSlot
from tenantchat.core.tenant import TenantPolicy

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
    """

    answer: str
    committed: tuple[CommittedEffect, ...]
    pending: Mapping[str, object] | None
    model_name: str
    graph_version: str
    prompt_version: str

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
