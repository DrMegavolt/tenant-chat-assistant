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
import re
from collections.abc import Mapping, Sequence
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
    ValidationError,
)
from tenantchat.core.ports import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceUnavailableError,
    IdempotencyKey,
)
from tenantchat.core.routing import IntentName, RoutingOutcome, clarify_question
from tenantchat.core.tenant import TenantPolicy
from tenantchat.core.workflows import ToolResult, WorkflowState, WorkflowTransition
from tenantchat.orchestration.agents import AGENTS_VERSION, AgentSpec
from tenantchat.orchestration.dependencies import DispatchDependencies
from tenantchat.orchestration.model import MessageRole, ToolCall
from tenantchat.orchestration.prompts import (
    DEFAULT_BUDGET,
    DEFAULT_REGISTRY,
    DISPATCH_SYSTEM_TEMPLATE_ID,
    AssemblyOutcome,
    HistoryTurn,
    PromptEvidence,
    assemble_prompt,
)
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

# The citation marker the `dispatch-system@3` prompt asks for:
# [evidence:<source_id>]. The source id is an index chunk id, so the same
# charset as an Elasticsearch document id.
_CITATION_RE = re.compile(r"\[evidence:([A-Za-z0-9][A-Za-z0-9._:-]{0,199})\]")


def citation_ids(text: str) -> tuple[str, ...]:
    """The source ids an answer cites, in the order written, deduplicated."""
    seen: set[str] = set()
    ids: list[str] = []
    for match in _CITATION_RE.finditer(text):
        if match.group(1) not in seen:
            seen.add(match.group(1))
            ids.append(match.group(1))
    return tuple(ids)


def strip_citation_markers(text: str) -> str:
    """Remove citation markers from an answer before it is published.

    The widget renders the curated citation list, never the raw marker, so an
    answer is delivered without the labels — including for a citation that
    failed validation, which must not surface as a dangling reference.
    """
    return _CITATION_RE.sub("", text).strip()


def _evidence_item_dict(item: EvidenceItem) -> dict[str, object]:
    """One passage as the checkpoint stores it: JSON-safe, with its curated
    citation metadata resolved by the adapter."""
    return {
        "source_id": item.source_id,
        "title": item.title,
        "source_name": item.source_name,
        "location": item.location,
        "content": item.content,
        "document_id": str(item.document_id),
        "version_id": str(item.version_id),
        "score": item.score,
        "revision": item.revision,
        "effective_at": item.effective_at.isoformat(),
    }


def _citation_dict(item: dict[str, object]) -> dict[str, object]:
    """The curated citation metadata of one verified passage."""
    return {
        "source_id": item["source_id"],
        "title": item["title"],
        "source_name": item["source_name"],
        "location": item["location"],
        "revision": item["revision"],
        "effective_at": item["effective_at"],
    }


def _abstention_reply(policy: TenantPolicy) -> str:
    """The deterministic refusal for a question no approved material answers.

    Written by the server, not the model: the retrieval verdict that produces
    it means the model has nothing trustworthy to say, so letting the model
    improvise the refusal would defeat the abstention. Quotes nothing from the
    question, which is content and stays in the inference plane.
    """
    return (
        "I do not have approved material to answer that yet, so I will not "
        f"guess. Ask me about hours, services, or pricing — or call {policy.phone}."
    )


class DispatchNode(StrEnum):
    """Node names, closed so a routing function cannot invent an edge."""

    ROUTE = "route"
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


def latest_visitor_message(state: DispatchState) -> str:
    """The message that started this turn, which is what the router scores."""
    for entry in reversed(state["transcript"]):
        if entry["role"] == "user":
            return entry["content"]
    return ""


def _agent_for(deps: DispatchDependencies, state: DispatchState) -> AgentSpec | None:
    """The agent the routed intent dispatched to, or ``None`` when none applies.

    An unrecognized value is treated as "no agent" rather than raised on: the
    safe failure is a refusal, not a graph crash.
    """
    raw = state.get("routed_intent", "")
    if not raw:
        return None
    try:
        intent = IntentName(raw)
    except ValueError:
        return None
    return deps.agents.for_intent(intent)


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


def _assemble_prompt(
    policy: TenantPolicy,
    state: DispatchState,
    evidence: Sequence[PromptEvidence] = (),
) -> AssemblyOutcome:
    """Build the one prompt a model call may receive (`AI-003`).

    The whole transcript goes in; the assembly budget decides what fits and
    returns what it excluded rather than truncating silently. Evidence is the
    retrieval this turn grounded the call in (`RAG-005`); assembly admits it
    as untrusted segments and reports what the budget left out.
    """
    return assemble_prompt(
        DEFAULT_REGISTRY.current(DISPATCH_SYSTEM_TEMPLATE_ID),
        policy=policy,
        workflow=dict(state),
        history=[
            HistoryTurn(
                role=MessageRole(entry["role"]),
                content=entry["content"],
                tool_calls=tuple(_restore(call) for call in entry["tool_calls"]),
                tool_call_id=entry["tool_call_id"] or None,
            )
            for entry in state["transcript"]
        ],
        evidence=evidence,
        budget=DEFAULT_BUDGET,
    )


class DispatchNodes:
    """The node implementations, bound to one set of ports."""

    def __init__(self, dependencies: DispatchDependencies) -> None:
        self._deps = dependencies

    async def route(self, state: DispatchState) -> dict[str, Any]:
        """Decide the turn's intent, record the decision, and open the workflow.

        Every effect here is deterministic and keyed: the decision is a pure
        function of the message, the active workflow, and the versioned policy,
        and the routing record and workflow writes carry idempotency keys
        derived from checkpoint values — so a node that runs again after a
        crash records the same decision and opens no second workflow.

        The workflow is the conversation's durable state, not the checkpoint:
        when the turn switches topic the old workflow is suspended with its
        collected fields intact, and a handoff or cancellation is recorded as a
        transition on it.
        """
        tenant_id = state["tenant_id"]
        session_id = state["session_id"]
        turn = state["turn_index"]

        current = await self._deps.workflows.current(tenant_id, session_id)
        previous = current.intent if current is not None else None
        last = await self._deps.workflows.last_routing(tenant_id, session_id)
        clarification_pending = (
            last is not None
            and last.turn_index == turn - 1
            and last.outcome is RoutingOutcome.CLARIFY
        )
        decision = self._deps.routing.route(
            latest_visitor_message(state),
            previous_intent=previous,
            clarification_pending=clarification_pending,
        )
        await self._deps.workflows.record_routing(
            tenant_id=tenant_id,
            session_id=session_id,
            decision=decision,
            agent_version=AGENTS_VERSION,
            turn_index=turn,
            idempotency_key=self._routing_key(state),
        )

        base: dict[str, Any] = {
            "routing_outcome": decision.outcome.value,
            "route_rule": decision.rule.value,
        }
        if decision.outcome is RoutingOutcome.CLARIFY:
            question = clarify_question(decision)
            return base | {
                "transcript": [assistant_entry(question, [])],
                "clarification_question": question,
            }
        if decision.outcome is RoutingOutcome.HANDOFF:
            # The escalation node closes out an active workflow, so it must be
            # told which one this handoff belongs to.
            return base | {
                "failure": HandoffReason.UNRESOLVED.value,
                "workflow_id": current.workflow_id if current is not None else "",
            }

        chosen = decision.chosen
        if chosen is None:
            raise RuntimeError(f"{decision.rule.value} routing decision chose no intent")
        if chosen is IntentName.HANDOFF:
            return base | {
                "routed_intent": chosen.value,
                "failure": HandoffReason.CUSTOMER_REQUEST.value,
                "workflow_id": current.workflow_id if current is not None else "",
            }
        if chosen is IntentName.CANCEL:
            if current is not None:
                await self._deps.workflows.transition(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    workflow_id=current.workflow_id,
                    transition=WorkflowTransition.CANCEL,
                    payload={},
                    idempotency_key=self._workflow_key(state, "cancel"),
                )
            return base | {"routed_intent": chosen.value, "collected_fields": {}}

        agent = self._deps.agents.for_intent(chosen)
        if agent is None:
            raise RuntimeError(f"no agent registered for intent {chosen.value}")

        if not agent.workflow:
            # A single-turn agent needs no workflow row; a previous workflow is
            # suspended so the durable record still holds it.
            if current is not None:
                await self._deps.workflows.transition(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    workflow_id=current.workflow_id,
                    transition=WorkflowTransition.SUSPEND,
                    payload={"switched_to": chosen.value},
                    idempotency_key=self._workflow_key(state, "suspend"),
                )
            return base | {"routed_intent": chosen.value, "collected_fields": {}}

        if current is not None and current.intent is chosen:
            return base | {
                "routed_intent": chosen.value,
                "workflow_id": current.workflow_id,
            }

        if current is not None:
            await self._deps.workflows.transition(
                tenant_id=tenant_id,
                session_id=session_id,
                workflow_id=current.workflow_id,
                transition=WorkflowTransition.SUSPEND,
                payload={"switched_to": chosen.value},
                idempotency_key=self._workflow_key(state, "suspend"),
            )
        started = await self._deps.workflows.start(
            tenant_id=tenant_id,
            session_id=session_id,
            intent=chosen,
            agent_version=AGENTS_VERSION,
            next_allowed_actions=agent.tool_names,
            turn_index=turn,
            idempotency_key=self._workflow_key(state, "start"),
        )
        return base | {
            "routed_intent": chosen.value,
            "workflow_id": started.workflow_id,
            "collected_fields": {},
        }

    async def call_model(self, state: DispatchState) -> dict[str, Any]:
        """Ask the model what to do next, grounded in this turn's evidence."""
        policy = await self._deps.policies.policy(state["tenant_id"])
        agent = _agent_for(self._deps, state)
        bundle = await self._retrieve_evidence(state)
        if self._should_abstain(agent, bundle):
            return self._abstention_update(state, policy)
        evidence = tuple(
            PromptEvidence(source_id=item.source_id, title=item.title, content=item.content)
            for item in (bundle.items if bundle is not None else ())
        )
        outcome = _assemble_prompt(policy, state, evidence)
        if self._should_abstain_after_assembly(agent, bundle, outcome):
            # The retrieval verdict passed, but the assembled prompt carried no
            # evidence segment — a budget cut, not a retriever failure. The
            # model must still not guess from an empty context.
            return self._abstention_update(state, policy)
        # The model is offered only the tools the routed agent may call; the
        # tools node enforces the same allowlist against whatever it sends.
        allowed = tuple(
            spec for spec in TOOL_SPECS if spec.name in (agent.tool_names if agent else ())
        )
        try:
            response = await self._deps.model.complete(outcome.prompt, tools=allowed)
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
        update: dict[str, Any] = {
            "transcript": [assistant_entry(response.content, [_store(call) for call in calls])],
            "rounds": state["rounds"] + 1,
            "model_name": response.model_name,
            "pending_booking": _store(booking) if booking is not None else None,
        }
        if response.content.strip():
            update.update(self._evidence_update(bundle, outcome))
        return update

    @staticmethod
    def _abstention_update(state: DispatchState, policy: TenantPolicy) -> dict[str, Any]:
        return {
            "transcript": [assistant_entry(_abstention_reply(policy), [])],
            "rounds": state["rounds"] + 1,
        }

    async def _retrieve_evidence(self, state: DispatchState) -> EvidenceBundle | None:
        """The passages that may ground this turn, or ``None`` without retrieval.

        A retrieval failure is treated as "no evidence": an index that is down
        must make the assistant abstain for knowledge questions, never answer
        from nothing.
        """
        source = self._deps.evidence
        if source is None:
            return None
        try:
            return await source.retrieve(
                tenant_id=state["tenant_id"],
                query=latest_visitor_message(state),
            )
        except EvidenceUnavailableError:
            logger.warning(
                "retrieval unavailable for turn",
                extra={"tenant_id": state["tenant_id"], "turn_index": state["turn_index"]},
            )
            return EvidenceBundle(
                items=(),
                sufficient=False,
                retriever_version="unavailable",
                reranker=None,
                min_evidence_score=0.0,
            )

    @staticmethod
    def _should_abstain(agent: AgentSpec | None, bundle: EvidenceBundle | None) -> bool:
        """Whether this turn must refuse rather than call the model.

        Only the general-knowledge agent abstains: every other agent answers
        from tool results or a workflow, which evidence does not gate. ``None``
        is a composition without retrieval, which answers as it did before
        `RAG-005`.
        """
        return (
            agent is not None
            and agent.intent is IntentName.GENERAL
            and bundle is not None
            and not bundle.sufficient
        )

    @classmethod
    def _should_abstain_after_assembly(
        cls, agent: AgentSpec | None, bundle: EvidenceBundle | None, outcome: AssemblyOutcome
    ) -> bool:
        """Whether admission dropped the evidence the verdict relied on.

        The verdict speaks about the retrieved pool; the model only sees what
        the prompt budget admitted. If nothing was admitted, the context is
        empty and the model must not be called regardless of the verdict.
        """
        if agent is None or agent.intent is not IntentName.GENERAL or bundle is None:
            return False
        return not any(
            segment.segment_id.startswith("evidence:")
            for segment in outcome.prompt.messages[0].segments
        )

    @staticmethod
    def _evidence_update(
        bundle: EvidenceBundle | None, outcome: AssemblyOutcome
    ) -> dict[str, object]:
        """The evidence the answer this call produced was grounded in.

        Recorded only when the model produced content, so finalize validates
        the published answer against the context of the call that wrote it.
        ``evidence_ids`` is the *exact* context: the ids assembly admitted to
        the prompt, not the wider retrieved pool.
        """
        admitted = [
            segment.segment_id.removeprefix("evidence:")
            for segment in outcome.prompt.messages[0].segments
            if segment.segment_id.startswith("evidence:")
        ]
        if bundle is None:
            return {"evidence_ids": admitted, "evidence_sufficient": False, "evidence_meta": {}}
        by_id = {item.source_id: item for item in bundle.items}
        admitted_items = [by_id[source_id] for source_id in admitted if source_id in by_id]
        return {
            "evidence": [_evidence_item_dict(item) for item in admitted_items],
            "evidence_ids": admitted,
            "evidence_sufficient": bundle.sufficient,
            "evidence_meta": {
                "sufficient": bundle.sufficient,
                "retriever_version": bundle.retriever_version,
                "reranker": bundle.reranker,
                "min_evidence_score": bundle.min_evidence_score,
            },
        }

    async def run_tools(self, state: DispatchState) -> dict[str, Any]:
        """Run every tool call except the one booking awaiting confirmation.

        Skipping is by call ID rather than by tool name. A model that proposed
        two bookings in one response gets one confirmation and an explicit
        refusal for the rest, instead of a second call that silently never
        receives a result.

        The allowlist is enforced here, deterministically: a call to a tool the
        routed agent may not use is answered with a refusal and never executed,
        and the refusal names the allowed set so the model can recover.
        """
        policy = await self._deps.policies.policy(state["tenant_id"])
        pending = state["pending_booking"]
        awaiting = pending["call_id"] if pending is not None else None
        agent = _agent_for(self._deps, state)
        entries = []
        committed: list[CommittedAction] = []
        results: list[ToolResult] = []
        fields: dict[str, str] = {}
        completed_lead = False

        for call in unanswered_tool_calls(state):
            if call["call_id"] == awaiting:
                continue
            tool = ToolName.resolve(call["name"])
            if tool is None:
                entries.append(
                    tool_entry(call["call_id"], _payload(error="unknown_tool", name=call["name"]))
                )
                continue
            if agent is None or tool not in agent.tools:
                entries.append(
                    tool_entry(
                        call["call_id"],
                        _payload(
                            error="tool_not_allowed",
                            name=call["name"],
                            allowed_tools=list(agent.tool_names) if agent is not None else [],
                        ),
                    )
                )
                continue
            content, action = await self._run_one(state, policy, call)
            entries.append(tool_entry(call["call_id"], content))
            results.append(ToolResult(call_id=call["call_id"], name=call["name"], result=content))
            fields.update(self._collected(agent, _arguments(call)))
            if action is not None:
                committed.append(action)
                if action["action"] == ToolName.CREATE_LEAD.value:
                    completed_lead = True

        update: dict[str, Any] = {"transcript": entries, "committed": committed}
        if agent is not None and agent.workflow and state["workflow_id"]:
            merged = await self._deps.workflows.update(
                tenant_id=state["tenant_id"],
                session_id=state["session_id"],
                workflow_id=state["workflow_id"],
                collected_fields=fields,
                allowed_field_names=tuple(field.name for field in agent.input_fields),
                tool_results=tuple(results),
                next_allowed_actions=agent.tool_names,
                turn_index=state["turn_index"],
                idempotency_key=self._workflow_key(state, "update", "tools"),
            )
            update["collected_fields"] = dict(merged.collected_fields)
            if completed_lead:
                await self._deps.workflows.transition(
                    tenant_id=state["tenant_id"],
                    session_id=state["session_id"],
                    workflow_id=state["workflow_id"],
                    transition=WorkflowTransition.COMPLETE,
                    payload={},
                    idempotency_key=self._workflow_key(state, "complete", "lead"),
                )
        return update

    async def confirm_booking(self, state: DispatchState) -> dict[str, Any]:
        """Validate the proposed booking, then pause for the customer.

        The validation runs *before* the interrupt so that a booking which
        cannot succeed never reaches the customer as a question. It also runs
        again after the resume, because LangGraph re-executes the whole node —
        which is exactly why the pause transition, the one effect here, is
        keyed by the booking call ID so the replay is a no-op.
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

        confirmation = {
            "awaiting": "booking_confirmation",
            "service": command.service.display_name,
            "slot": command.slot,
            "customer_name": command.customer_name,
            "address": command.address,
        }
        if state["workflow_id"]:
            await self._deps.workflows.transition(
                tenant_id=state["tenant_id"],
                session_id=state["session_id"],
                workflow_id=state["workflow_id"],
                transition=WorkflowTransition.PAUSE,
                payload=confirmation,
                idempotency_key=self._workflow_key(state, "pause", pending["call_id"]),
            )
        decision = BookingDecision.of(interrupt(confirmation))
        return {"booking_approved": decision is BookingDecision.APPROVED}

    async def commit_booking(self, state: DispatchState) -> dict[str, Any]:
        """Book the confirmed slot, exactly once."""
        pending = state["pending_booking"]
        if pending is None:
            return {}
        call_id = pending["call_id"]
        tenant_id = state["tenant_id"]
        session_id = state["session_id"]
        workflow_id = state["workflow_id"]
        agent = _agent_for(self._deps, state)

        if workflow_id and agent is not None and agent.workflow:
            merged = await self._deps.workflows.update(
                tenant_id=tenant_id,
                session_id=session_id,
                workflow_id=workflow_id,
                collected_fields=self._collected(agent, _arguments(pending)),
                allowed_field_names=tuple(field.name for field in agent.input_fields),
                tool_results=(),
                next_allowed_actions=agent.tool_names,
                turn_index=state["turn_index"],
                idempotency_key=self._workflow_key(state, "update", call_id),
            )
        else:
            merged = None

        if not state["booking_approved"]:
            # Declining is a normal turn, not an error: the workflow resumes and
            # the conversation keeps looking for another slot.
            if workflow_id:
                await self._deps.workflows.transition(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    workflow_id=workflow_id,
                    transition=WorkflowTransition.RESUME,
                    payload={"decision": "declined"},
                    idempotency_key=self._workflow_key(state, "resume", call_id),
                )
            return self._with_collected(
                {
                    "transcript": [tool_entry(call_id, _payload(status="declined_by_customer"))],
                    "pending_booking": None,
                },
                merged,
            )

        key = self._key(state, ToolName.BOOK_APPOINTMENT, call_id)
        replay = await self._deps.bookings.find_replay(state["tenant_id"], key)
        if replay is not None:
            # The same attempt already booked this slot while the process was
            # away; re-validating would refuse it as "not offered" now that the
            # slot is taken by this very booking, so return the committed result.
            if workflow_id:
                await self._resume_and_complete(state, call_id, "approved")
            return self._with_collected(
                {
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
                },
                merged,
            )

        try:
            command = await self._parse_booking(state, arguments=_arguments(pending))
            confirmation = await self._deps.bookings.confirm(
                command,
                session_id=state["session_id"],
                idempotency_key=key,
            )
        except DomainError as error:
            # Reachable when the slot was taken while the customer was deciding.
            if workflow_id:
                await self._deps.workflows.transition(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    workflow_id=workflow_id,
                    transition=WorkflowTransition.RESUME,
                    payload={"error": error.code},
                    idempotency_key=self._workflow_key(state, "resume", call_id),
                )
            return self._with_collected(
                {
                    "transcript": [tool_entry(call_id, _error_payload(error))],
                    "pending_booking": None,
                },
                merged,
            )

        if workflow_id:
            await self._resume_and_complete(state, call_id, "approved")
        return self._with_collected(
            {
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
            },
            merged,
        )

    async def _resume_and_complete(self, state: DispatchState, call_id: str, decision: str) -> None:
        """Move a paused booking workflow through its final transitions.

        Two transitions rather than one: the customer's decision is the resume,
        and the committed booking is the completion — a workflow that books
        without ever being answered is a workflow that never paused.
        """
        await self._deps.workflows.transition(
            tenant_id=state["tenant_id"],
            session_id=state["session_id"],
            workflow_id=state["workflow_id"],
            transition=WorkflowTransition.RESUME,
            payload={"decision": decision},
            idempotency_key=self._workflow_key(state, "resume", call_id),
        )
        await self._deps.workflows.transition(
            tenant_id=state["tenant_id"],
            session_id=state["session_id"],
            workflow_id=state["workflow_id"],
            transition=WorkflowTransition.COMPLETE,
            payload={},
            idempotency_key=self._workflow_key(state, "complete", call_id),
        )

    @staticmethod
    def _with_collected(result: dict[str, Any], merged: WorkflowState | None) -> dict[str, Any]:
        """Mirror the persisted collected fields into the checkpointed state."""
        if merged is not None:
            result["collected_fields"] = dict(merged.collected_fields)
        return result

    async def escalate(self, state: DispatchState) -> dict[str, Any]:
        """Hand the conversation to a person and say so.

        The summary is written here rather than by the model, because every route
        into this node is one where the model has stopped being reliable: it
        failed, it returned nothing, or it spent the whole round budget without
        reaching an answer — or the customer asked for a person outright.

        An active workflow ends here too: a failure is recorded and the workflow
        is handed off, so the durable record says what the workflow's last
        moment was. A customer-requested handoff skips the failure mark — the
        workflow did not fail, the customer left it.
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
        if state["workflow_id"]:
            if reason is not HandoffReason.CUSTOMER_REQUEST:
                await self._deps.workflows.transition(
                    tenant_id=state["tenant_id"],
                    session_id=state["session_id"],
                    workflow_id=state["workflow_id"],
                    transition=WorkflowTransition.FAIL,
                    payload={"failure": reason.value},
                    idempotency_key=self._workflow_key(state, "fail"),
                )
            await self._deps.workflows.transition(
                tenant_id=state["tenant_id"],
                session_id=state["session_id"],
                workflow_id=state["workflow_id"],
                transition=WorkflowTransition.HAND_OFF,
                payload={"reason": reason.value},
                idempotency_key=self._workflow_key(state, "hand_off"),
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
        """Publish the assistant's answer for this turn, with its citations.

        The search stops at the visitor's message. Reading further back would
        find the *previous* turn's answer and republish it, which is worse than
        the fallback: a customer who asked a new question would be told the
        thing they were already told.

        Citations are validated against the exact evidence context the answer's
        model call was assembled from (`RAG-005`): a source id the model wrote
        that is not in that context — fabricated, stale, or another tenant's —
        is dropped from what is published and reported as an invalid citation
        for the inference plane. Markers are stripped from the answer either
        way, so the widget never renders a dangling reference.
        """
        for entry in reversed(state["transcript"]):
            if entry["role"] == "user":
                break
            if entry["role"] == "assistant" and entry["content"].strip():
                found = citation_ids(entry["content"])
                context = frozenset(state["evidence_ids"])
                by_id = {str(item["source_id"]): item for item in state["evidence"]}
                citations = [
                    _citation_dict(by_id[source_id])
                    for source_id in found
                    if source_id in context and source_id in by_id
                ]
                return {
                    "answer": strip_citation_markers(entry["content"]),
                    "citations": citations,
                    "citation_invalid": [
                        source_id for source_id in found if source_id not in context
                    ],
                }
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

    @staticmethod
    def _collected(agent: AgentSpec, arguments: Mapping[str, object]) -> dict[str, str]:
        """The collectible fields a tool call carried, for the workflow record.

        Fields are picked only from the agent's declared input schema, so the
        model can never introduce a field name the registry did not approve.
        An overlong value is skipped here; the command that runs the tool
        enforces the real bounds.
        """
        fields: dict[str, str] = {}
        for field in agent.input_fields:
            try:
                value = text_argument(arguments, field.name)
            except ValidationError:
                continue
            if value:
                fields[field.name] = value
        return fields

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

    @staticmethod
    def _routing_key(state: DispatchState) -> IdempotencyKey:
        """The key one turn's routing record is written under, once."""
        return IdempotencyKey.derive(
            state["tenant_id"], state["session_id"], "routing", str(state["turn_index"])
        )

    @staticmethod
    def _workflow_key(state: DispatchState, kind: str, distinguisher: str = "") -> IdempotencyKey:
        """The key one workflow effect is written under, once per kind and turn.

        ``kind`` separates the effects a turn can perform (start, suspend,
        pause, resume, complete, cancel, fail, hand_off, update) and
        ``distinguisher`` separates repeated effects of one kind — two update
        calls in one turn, or the same transition across two workflows.
        """
        return IdempotencyKey.derive(
            state["tenant_id"],
            state["session_id"],
            "workflow",
            kind,
            str(state["turn_index"]),
            distinguisher,
        )


def route_after_routing(state: DispatchState) -> DispatchNode:
    """Where a routed turn goes: clarify ends as a question, handoff escalates."""
    if state["routing_outcome"] == RoutingOutcome.CLARIFY.value:
        return DispatchNode.FINALIZE
    if state["routing_outcome"] == RoutingOutcome.HANDOFF.value:
        return DispatchNode.ESCALATE
    if state["routed_intent"] == IntentName.HANDOFF.value:
        return DispatchNode.ESCALATE
    return DispatchNode.MODEL


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
