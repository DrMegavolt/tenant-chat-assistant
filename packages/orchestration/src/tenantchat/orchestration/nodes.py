"""The graph's nodes.

Read these against `ADR-0001`'s division of labour. A node decides *what to
attempt* and *what to tell the model about the result*; it never decides whether
an action is permitted, never validates a field, and never writes a row. Those
belong to :mod:`tenantchat.core.commands` and the services behind
:mod:`tenantchat.core.ports`, and they belong there because a node runs again
whenever a run resumes.

Two consequences show up repeatedly below:

**Every commit carries a derived idempotency key.** The parts are values the
checkpoint already holds — tenant, session, tool name, turn number, and the
provider's own call ID — so a replayed node derives the key it derived the first
time and the service recognizes the repeat. A key built from a fresh UUID here
would look correct and guarantee nothing.

**A rejected action is a tool result, not an exception.** A booking the tenant
does not permit, a service that did not resolve, a phone number missing its area
code: these are things the assistant should say out loud and work around, so
they travel back to the model as a JSON payload naming the domain's error code.
``DomainError.detail`` never joins them — it is operator context and may quote
the customer.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, Final

from langgraph.types import interrupt

from tenantchat.core.commands import BookingCommand, HandoffCommand, HandoffReason, LeadCommand
from tenantchat.core.errors import (
    BookingNotPermittedError,
    DomainError,
    MissingRequiredFieldsError,
    SlotUnavailableError,
    UnknownServiceError,
)
from tenantchat.core.ports import IdempotencyKey
from tenantchat.core.tenant import TenantPolicy
from tenantchat.orchestration.dependencies import DispatchDependencies
from tenantchat.orchestration.model import MessageRole, ModelMessage, ToolCall
from tenantchat.orchestration.prompts import build_system_prompt
from tenantchat.orchestration.state import (
    CommittedAction,
    DispatchState,
    StoredToolCall,
    assistant_entry,
    tool_entry,
)
from tenantchat.orchestration.tools import TOOL_SPECS, ToolName, text_argument

logger = logging.getLogger(__name__)

# Model calls one visitor message may spend. Four covers the deepest legitimate
# path — look up the service area, list availability, book, then answer — with
# one to spare. Past that the model is looping, and looping costs the customer
# time they would rather spend talking to a person.
MAX_TOOL_ROUNDS: Final = 4

# How much of the conversation each model call sees. Long enough to keep a
# multi-turn booking coherent, short enough that a long session cannot grow the
# prompt without bound. `RAG-006` replaces the window with conversation-aware
# retrieval.
TRANSCRIPT_WINDOW: Final = 24


class DispatchNode(StrEnum):
    """Node names, closed so a routing function cannot invent an edge."""

    MODEL = "model"
    TOOLS = "tools"
    CONFIRM_BOOKING = "confirm_booking"
    COMMIT_BOOKING = "commit_booking"
    ESCALATE = "escalate"
    FINALIZE = "finalize"


class BookingDecision(StrEnum):
    """What the customer answered when asked to confirm a booking."""

    APPROVED = "approved"
    DECLINED = "declined"

    @classmethod
    def of(cls, resumed: object) -> BookingDecision:
        """Read a resume value conservatively.

        Anything that is not an explicit approval declines. A resume value
        arrives from outside the graph, and the failure modes are not
        symmetrical: a wrongly declined booking costs one more exchange, while a
        wrongly approved one sends a van to a stranger's house.
        """
        if isinstance(resumed, bool):
            return cls.APPROVED if resumed else cls.DECLINED
        if isinstance(resumed, str) and resumed.strip().casefold() == cls.APPROVED.value:
            return cls.APPROVED
        if isinstance(resumed, Mapping):
            return cls.of(resumed.get("decision"))
        return cls.DECLINED


def _payload(**fields: object) -> str:
    return json.dumps(fields, separators=(",", ":"), sort_keys=True)


def _error_payload(error: DomainError) -> str:
    """Describe a refusal to the model without leaking operator context.

    ``code`` and ``message`` are the publishable contract. The typed fields are
    added by hand rather than by reflection so that adding a private field to a
    domain error cannot start publishing it.
    """
    fields: dict[str, object] = {"error": error.code, "message": error.message}
    if isinstance(error, MissingRequiredFieldsError):
        fields["missing_fields"] = [field.value for field in error.fields]
    if isinstance(error, UnknownServiceError):
        fields["offered_services"] = list(error.offered)
    if isinstance(error, SlotUnavailableError):
        fields["available_slots"] = list(error.offered)
    return _payload(**fields)


def _arguments(call: StoredToolCall) -> Mapping[str, object]:
    """Parse a stored call's arguments, treating unparseable JSON as empty.

    The model produced this string and providers do emit malformed JSON. Empty
    arguments make the command report every required field as missing, which is
    a question the assistant can ask; raising here would end the turn.
    """
    try:
        parsed: object = json.loads(call["arguments_json"])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _store(call: ToolCall) -> StoredToolCall:
    return {
        "call_id": call.call_id,
        "name": call.name,
        "arguments_json": json.dumps(call.arguments, separators=(",", ":"), sort_keys=True),
    }


def _restore(stored: StoredToolCall) -> ToolCall:
    return ToolCall(call_id=stored["call_id"], name=stored["name"], arguments=_arguments(stored))


def pending_tool_calls(state: DispatchState) -> tuple[StoredToolCall, ...]:
    """Tool calls from the most recent assistant message, if it made any."""
    for entry in reversed(state["transcript"]):
        if entry["role"] == "assistant":
            return tuple(entry["tool_calls"])
        if entry["role"] == "user":
            break
    return ()


def unanswered_tool_calls(state: DispatchState) -> tuple[StoredToolCall, ...]:
    """Tool calls the model made that nothing has replied to yet.

    Every provider requires a result for each call before the conversation may
    continue. A turn that gives up mid-loop would otherwise leave the dangling
    calls in the transcript for the *next* turn to send, where they are rejected
    by the provider rather than by anything this code would notice.
    """
    answered = set()
    for entry in reversed(state["transcript"]):
        if entry["role"] == "tool":
            answered.add(entry["tool_call_id"])
        elif entry["role"] == "assistant":
            return tuple(call for call in entry["tool_calls"] if call["call_id"] not in answered)
        elif entry["role"] == "user":
            break
    return ()


def _model_messages(policy: TenantPolicy, state: DispatchState) -> list[ModelMessage]:
    window = state["transcript"][-TRANSCRIPT_WINDOW:]
    messages = [
        ModelMessage(role=MessageRole.SYSTEM, content=build_system_prompt(policy)),
    ]
    messages.extend(
        ModelMessage(
            role=MessageRole(entry["role"]),
            content=entry["content"],
            tool_calls=tuple(_restore(call) for call in entry["tool_calls"]),
            tool_call_id=entry["tool_call_id"] or None,
        )
        for entry in window
    )
    return messages


class DispatchNodes:
    """The node implementations, bound to one set of ports."""

    def __init__(self, dependencies: DispatchDependencies) -> None:
        self._deps = dependencies

    async def call_model(self, state: DispatchState) -> dict[str, Any]:
        """Ask the model what to do next."""
        policy = await self._deps.policies.policy(state["tenant_id"])
        try:
            response = await self._deps.model.complete(
                _model_messages(policy, state), tools=TOOL_SPECS
            )
        except Exception:
            # Deliberately broad, and deliberately not retried. Whatever the
            # provider did, the customer is waiting; `REL-001` owns retry and
            # circuit breaking inside the client, and the honest thing left for
            # the graph to do is fetch a person.
            logger.exception(
                "model call failed",
                extra={"tenant_id": state["tenant_id"], "turn_index": state["turn_index"]},
            )
            return {"failure": HandoffReason.TOOL_FAILURE.value, "rounds": state["rounds"] + 1}

        calls = tuple(response.tool_calls)
        if not calls and not response.content.strip():
            return {"failure": HandoffReason.UNRESOLVED.value, "rounds": state["rounds"] + 1}

        booking = next((call for call in calls if call.name == ToolName.BOOK_APPOINTMENT), None)
        return {
            "transcript": [assistant_entry(response.content, [_store(call) for call in calls])],
            "rounds": state["rounds"] + 1,
            "model_name": response.model_name,
            "pending_booking": _store(booking) if booking is not None else None,
        }

    async def run_tools(self, state: DispatchState) -> dict[str, Any]:
        """Run every tool call except the one booking awaiting confirmation.

        Skipping is by call ID rather than by tool name. A model that proposed
        two bookings in one response gets one confirmation and an explicit
        refusal for the rest, instead of a second call that silently never
        receives a result.
        """
        policy = await self._deps.policies.policy(state["tenant_id"])
        pending = state["pending_booking"]
        awaiting = pending["call_id"] if pending is not None else None
        entries = []
        committed: list[CommittedAction] = []

        for call in unanswered_tool_calls(state):
            if call["call_id"] == awaiting:
                continue
            content, action = await self._run_one(state, policy, call)
            entries.append(tool_entry(call["call_id"], content))
            if action is not None:
                committed.append(action)

        return {"transcript": entries, "committed": committed}

    async def confirm_booking(self, state: DispatchState) -> dict[str, Any]:
        """Validate the proposed booking, then pause for the customer.

        The validation runs *before* the interrupt so that a booking which
        cannot succeed never reaches the customer as a question. It also runs
        again after the resume, because LangGraph re-executes the whole node —
        which is exactly why nothing in this node writes anything.
        """
        pending = state["pending_booking"]
        if pending is None:
            return {}

        try:
            command = await self._parse_booking(state, arguments=_arguments(pending))
        except DomainError as error:
            return {
                "transcript": [tool_entry(pending["call_id"], _error_payload(error))],
                "pending_booking": None,
            }

        decision = BookingDecision.of(
            interrupt(
                {
                    "awaiting": "booking_confirmation",
                    "service": command.service.display_name,
                    "slot": command.slot,
                    "customer_name": command.customer_name,
                    "address": command.address,
                }
            )
        )
        return {"booking_approved": decision is BookingDecision.APPROVED}

    async def commit_booking(self, state: DispatchState) -> dict[str, Any]:
        """Book the confirmed slot, exactly once."""
        pending = state["pending_booking"]
        if pending is None:
            return {}
        call_id = pending["call_id"]

        if not state["booking_approved"]:
            return {
                "transcript": [tool_entry(call_id, _payload(status="declined_by_customer"))],
                "pending_booking": None,
            }

        key = self._key(state, ToolName.BOOK_APPOINTMENT, call_id)
        replay = await self._deps.bookings.find_replay(state["tenant_id"], key)
        if replay is not None:
            # The same attempt already booked this slot while the process was
            # away; re-validating would refuse it as "not offered" now that the
            # slot is taken by this very booking, so return the committed result.
            return {
                "transcript": [
                    tool_entry(
                        call_id,
                        _payload(
                            status="confirmed",
                            confirmation_id=replay.reference,
                            service=replay.service_name,
                            slot=replay.slot,
                        ),
                    )
                ],
                "committed": [
                    CommittedAction(
                        action=ToolName.BOOK_APPOINTMENT.value,
                        reference=replay.reference,
                        replayed=True,
                    )
                ],
                "pending_booking": None,
            }

        try:
            command = await self._parse_booking(state, arguments=_arguments(pending))
            confirmation = await self._deps.bookings.confirm(
                command,
                session_id=state["session_id"],
                idempotency_key=key,
            )
        except DomainError as error:
            # Reachable when the slot was taken while the customer was deciding.
            return {
                "transcript": [tool_entry(call_id, _error_payload(error))],
                "pending_booking": None,
            }

        return {
            "transcript": [
                tool_entry(
                    call_id,
                    _payload(
                        status="confirmed",
                        confirmation_id=confirmation.reference,
                        service=confirmation.service_name,
                        slot=confirmation.slot,
                    ),
                )
            ],
            "committed": [
                CommittedAction(
                    action=ToolName.BOOK_APPOINTMENT.value,
                    reference=confirmation.reference,
                    replayed=confirmation.replayed,
                )
            ],
            "pending_booking": None,
        }

    async def escalate(self, state: DispatchState) -> dict[str, Any]:
        """Hand the conversation to a person and say so.

        The summary is written here rather than by the model, because every route
        into this node is one where the model has stopped being reliable: it
        failed, it returned nothing, or it spent the whole round budget without
        reaching an answer.
        """
        policy = await self._deps.policies.policy(state["tenant_id"])
        reason = HandoffReason.parse(state["failure"] or HandoffReason.UNRESOLVED.value)
        command = HandoffCommand.parse(
            policy,
            reason=reason.value,
            summary=(
                f"Assistant could not complete turn {state['turn_index']} "
                f"({reason.value}) after {state['rounds']} model calls."
            ),
        )
        ticket = await self._deps.handoffs.request(
            command,
            session_id=state["session_id"],
            idempotency_key=self._key(state, ToolName.HANDOFF_TO_HUMAN, "escalation"),
        )
        abandoned = _payload(error="turn_abandoned", message="The conversation was handed over.")
        return {
            "answer": (
                "I am not able to finish this myself, so I have passed it to the team. "
                f"You can also reach them on {policy.phone}."
            ),
            # The turn stops here, but the conversation may not: a customer can
            # keep typing while they wait for someone. Closing out the calls this
            # node walked away from is what keeps the next turn's transcript
            # something a provider will accept.
            "transcript": [
                tool_entry(call["call_id"], abandoned) for call in unanswered_tool_calls(state)
            ],
            "pending_booking": None,
            "committed": [
                CommittedAction(
                    action=ToolName.HANDOFF_TO_HUMAN.value,
                    reference=ticket.reference,
                    replayed=ticket.replayed,
                )
            ],
        }

    async def finalize(self, state: DispatchState) -> dict[str, Any]:
        """Publish the assistant's answer for this turn.

        The search stops at the visitor's message. Reading further back would
        find the *previous* turn's answer and republish it, which is worse than
        the fallback: a customer who asked a new question would be told the
        thing they were already told.
        """
        for entry in reversed(state["transcript"]):
            if entry["role"] == "user":
                break
            if entry["role"] == "assistant" and entry["content"].strip():
                return {"answer": entry["content"].strip()}
        policy = await self._deps.policies.policy(state["tenant_id"])
        return {"answer": f"I can help with that — the team is on {policy.phone}."}

    async def _run_one(
        self,
        state: DispatchState,
        policy: TenantPolicy,
        call: StoredToolCall,
    ) -> tuple[str, CommittedAction | None]:
        arguments = _arguments(call)
        tool = ToolName.resolve(call["name"])
        try:
            if tool is ToolName.CHECK_SERVICE_AREA:
                return self._check_service_area(policy, arguments), None
            if tool is ToolName.GET_AVAILABILITY:
                return await self._get_availability(policy, arguments), None
            if tool is ToolName.CREATE_LEAD:
                return await self._create_lead(state, policy, call, arguments)
            if tool is ToolName.HANDOFF_TO_HUMAN:
                return await self._handoff(state, policy, call, arguments)
            if tool is ToolName.BOOK_APPOINTMENT:
                # A second booking in one response. Only one can be confirmed,
                # and confirming this one after the customer answered about the
                # other would be a booking nobody agreed to.
                return _payload(
                    error="booking_already_proposed",
                    message="Only one booking can be confirmed at a time.",
                ), None
        except DomainError as error:
            return _error_payload(error), None
        return _payload(error="unknown_tool", name=call["name"]), None

    def _check_service_area(self, policy: TenantPolicy, arguments: Mapping[str, object]) -> str:
        zip_code = text_argument(arguments, "zip")
        return _payload(served=policy.serves_zip(zip_code), zip=zip_code, phone=policy.phone)

    async def _get_availability(self, policy: TenantPolicy, arguments: Mapping[str, object]) -> str:
        if not policy.booking_enabled:
            raise BookingNotPermittedError(detail=f"tenant {policy.tenant_id} has booking disabled")
        requested = text_argument(arguments, "service")
        service = policy.catalog.resolve(requested)
        if service is None:
            raise UnknownServiceError(
                offered=policy.catalog.offered_names(),
                detail=f"{requested!r} did not resolve for tenant {policy.tenant_id}",
            )
        slots = await self._deps.availability.offered_slots(policy.tenant_id, service.slug)
        return _payload(service=service.display_name, slots=[slot.label for slot in slots])

    async def _create_lead(
        self,
        state: DispatchState,
        policy: TenantPolicy,
        call: StoredToolCall,
        arguments: Mapping[str, object],
    ) -> tuple[str, CommittedAction | None]:
        command = LeadCommand.parse(
            policy,
            customer_name=text_argument(arguments, "customer_name"),
            contact=text_argument(arguments, "customer_phone_or_email"),
            service=text_argument(arguments, "service"),
            summary=text_argument(arguments, "summary"),
            address_or_zip=text_argument(arguments, "address_or_zip"),
            urgency=text_argument(arguments, "urgency"),
        )
        receipt = await self._deps.leads.capture(
            command,
            session_id=state["session_id"],
            idempotency_key=self._key(state, ToolName.CREATE_LEAD, call["call_id"]),
        )
        return (
            _payload(status="created", lead_id=receipt.reference, phone=policy.phone),
            CommittedAction(
                action=ToolName.CREATE_LEAD.value,
                reference=receipt.reference,
                replayed=receipt.replayed,
            ),
        )

    async def _handoff(
        self,
        state: DispatchState,
        policy: TenantPolicy,
        call: StoredToolCall,
        arguments: Mapping[str, object],
    ) -> tuple[str, CommittedAction | None]:
        command = HandoffCommand.parse(
            policy,
            reason=text_argument(arguments, "reason"),
            summary=text_argument(arguments, "summary"),
        )
        ticket = await self._deps.handoffs.request(
            command,
            session_id=state["session_id"],
            idempotency_key=self._key(state, ToolName.HANDOFF_TO_HUMAN, call["call_id"]),
        )
        return (
            _payload(status="created", handoff_id=ticket.reference, phone=policy.phone),
            CommittedAction(
                action=ToolName.HANDOFF_TO_HUMAN.value,
                reference=ticket.reference,
                replayed=ticket.replayed,
            ),
        )

    async def _parse_booking(
        self, state: DispatchState, *, arguments: Mapping[str, object]
    ) -> BookingCommand:
        """Build the booking command from model arguments and tenant policy.

        Raises:
            DomainError: any reason the booking is not permitted or not
                well-formed. See :meth:`BookingCommand.parse`.
        """
        policy = await self._deps.policies.policy(state["tenant_id"])
        if not policy.booking_enabled:
            raise BookingNotPermittedError(detail=f"tenant {policy.tenant_id} has booking disabled")
        requested = text_argument(arguments, "service")
        service = policy.catalog.resolve(requested)
        return BookingCommand.parse(
            policy,
            customer_name=text_argument(arguments, "customer_name"),
            contact=text_argument(arguments, "customer_phone_or_email"),
            address=text_argument(arguments, "address"),
            service=requested,
            slot=text_argument(arguments, "slot"),
            offered_slots=(
                await self._deps.availability.offered_slots(policy.tenant_id, service.slug)
                if service is not None
                else ()
            ),
        )

    @staticmethod
    def _key(state: DispatchState, tool: ToolName, distinguisher: str) -> IdempotencyKey:
        """Name this effect from values the checkpoint already holds.

        Every part is stable across a replay of the same attempt and different
        for a genuinely new one. ``distinguisher`` is normally the provider's
        tool-call ID, which distinguishes two calls to the same tool in one turn.
        """
        return IdempotencyKey.derive(
            state["tenant_id"],
            state["session_id"],
            tool.value,
            str(state["turn_index"]),
            distinguisher,
        )


def route_after_model(state: DispatchState) -> DispatchNode:
    """Decide what the model's response asked for."""
    if state["failure"]:
        return DispatchNode.ESCALATE

    calls = pending_tool_calls(state)
    if not calls:
        return DispatchNode.FINALIZE
    if state["rounds"] >= MAX_TOOL_ROUNDS:
        return DispatchNode.ESCALATE

    # Everything except the one booking awaiting confirmation runs first, so no
    # call is left without a result across an interrupt the customer may take
    # minutes to answer. Compared by call ID rather than tool name, because a
    # second booking in the same response is one of the calls that has to run.
    pending = state["pending_booking"]
    awaiting = pending["call_id"] if pending is not None else None
    if any(call["call_id"] != awaiting for call in calls):
        return DispatchNode.TOOLS
    return DispatchNode.CONFIRM_BOOKING


def route_after_tools(state: DispatchState) -> DispatchNode:
    """Confirm a proposed booking before returning to the model."""
    if state["pending_booking"] is not None:
        return DispatchNode.CONFIRM_BOOKING
    return DispatchNode.MODEL


def route_after_confirmation(state: DispatchState) -> DispatchNode:
    """A cleared ``pending_booking`` means validation refused it before asking."""
    if state["pending_booking"] is None:
        return DispatchNode.MODEL
    return DispatchNode.COMMIT_BOOKING
