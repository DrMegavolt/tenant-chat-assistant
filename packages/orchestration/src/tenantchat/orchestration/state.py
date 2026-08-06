"""The graph's resumable state.

Everything here is written to a checkpoint after every node, so two properties
matter more than convenience.

**It round-trips through JSON.** Only ``str``, ``int``, ``bool``, ``list``, and
``dict`` appear. A domain object in the state would have to survive a
serializer, a schema change, and a rolling deploy that resumes a run started by
the previous release — and would drag the domain's types into the framework's
migration problem.

**It is not the system of record.** `ADR-0001` requires that deleting every
checkpoint lose no conversation, booking, lead, or handoff. What lives here is
resumable *execution* state: how far the tool loop has run, which booking is
waiting on the customer's confirmation, what has already been committed. The
authoritative rows are written by the domain services in
:mod:`tenantchat.core.ports` and outlive any checkpoint.

``transcript`` and ``committed`` accumulate with ``operator.add`` so that a node
returns only what it added. A node that returned the whole list would rewrite it
on every checkpoint, and two nodes running in the same superstep would silently
discard one another's entries.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict


class StoredToolCall(TypedDict):
    """A model tool call, flattened for the checkpoint.

    Arguments are held as JSON text rather than a nested mapping: the shape is
    model-controlled, so a typed field here would be a promise the model has not
    made. It is parsed once, at the point of use, inside a ``try``.
    """

    call_id: str
    name: str
    arguments_json: str


class TranscriptEntry(TypedDict):
    """One message in the model-facing conversation.

    ``tool_calls`` is populated on assistant entries and ``tool_call_id`` on tool
    results; both are always present, empty when they do not apply, because an
    optional key in checkpointed state is a ``KeyError`` waiting for the first
    resume across a release that added it.
    """

    role: str
    content: str
    tool_calls: list[StoredToolCall]
    tool_call_id: str


class CommittedAction(TypedDict):
    """A domain action this run has already caused.

    Kept so a resumed run can see what it did before it was interrupted without
    querying the domain, and so a caller can report the booking reference back to
    the customer. ``replayed`` records that the idempotency key matched an
    existing record rather than creating one.
    """

    action: str
    reference: str
    replayed: bool


class DispatchState(TypedDict):
    """State for one dispatcher conversation thread."""

    tenant_id: str
    # The domain conversation this thread belongs to. Distinct from LangGraph's
    # thread ID, which is a checkpoint key and carries no authorization meaning.
    session_id: str
    # Counts visitor messages, and is part of every idempotency key: a replayed
    # turn reuses its key while a genuinely new turn derives a fresh one. The
    # adding reducer is what lets a caller start a turn without first reading the
    # thread back to find out which number it is. No node may return this field.
    turn_index: Annotated[int, operator.add]
    transcript: Annotated[list[TranscriptEntry], operator.add]
    # Model calls spent on this turn, against `MAX_TOOL_ROUNDS`.
    rounds: int
    answer: str
    committed: Annotated[list[CommittedAction], operator.add]
    # The booking the customer is being asked to confirm, or ``None``.
    pending_booking: StoredToolCall | None
    # The customer's answer to that question, carried across the interrupt.
    booking_approved: bool
    # Set when the turn cannot continue on its own: the run escalates instead of
    # retrying, because `REL-001` owns retry policy for dependency clients and a
    # graph that retries as well would multiply it.
    failure: str
    model_name: str
    # The AGENT-001 routing outcome, mirrored from the durable routing record
    # so the model-facing prompt can bind the agent context. The durable record
    # in the workflow service is authoritative; these fields are the turn's
    # working copy.
    routing_outcome: str
    routed_intent: str
    route_rule: str
    workflow_id: str
    clarification_question: str
    # The fields collected so far by the active workflow agent, mirrored from
    # the durable workflow row for the same reason. Replaced wholesale by the
    # tools node, never accumulated.
    collected_fields: dict[str, str]


def visitor_entry(content: str) -> TranscriptEntry:
    return {"role": "user", "content": content, "tool_calls": [], "tool_call_id": ""}


def assistant_entry(content: str, tool_calls: list[StoredToolCall]) -> TranscriptEntry:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls,
        "tool_call_id": "",
    }


def tool_entry(call_id: str, content: str) -> TranscriptEntry:
    return {"role": "tool", "content": content, "tool_calls": [], "tool_call_id": call_id}


def initial_state(tenant_id: str, session_id: str, message: str) -> DispatchState:
    """The state a brand-new conversation starts from."""
    return {
        "tenant_id": tenant_id,
        "session_id": session_id,
        "turn_index": 1,
        "transcript": [visitor_entry(message)],
        "rounds": 0,
        "answer": "",
        "committed": [],
        "pending_booking": None,
        "booking_approved": False,
        "failure": "",
        "model_name": "",
        "routing_outcome": "",
        "routed_intent": "",
        "route_rule": "",
        "workflow_id": "",
        "clarification_question": "",
        "collected_fields": {},
    }


def next_turn(message: str) -> dict[str, object]:
    """The update that starts a follow-up turn on an existing thread.

    Only the fields a new turn resets appear. ``transcript`` and ``committed``
    accumulate, so the history and everything already committed survive.
    """
    return {
        "turn_index": 1,
        "transcript": [visitor_entry(message)],
        "rounds": 0,
        "answer": "",
        "pending_booking": None,
        "booking_approved": False,
        "failure": "",
        # The route node re-decides every turn; the previous turn's decision
        # must not leak into the next one.
        "routing_outcome": "",
        "routed_intent": "",
        "route_rule": "",
        "workflow_id": "",
        "clarification_question": "",
        "collected_fields": {},
    }
