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
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from enum import StrEnum
from typing import Any, Final

from langgraph.types import interrupt

from tenantchat.core.budgets import (
    DEFAULT_TENANT_BUDGET,
    check_input,
    check_output,
)
from tenantchat.core.claims import ClaimVerdict, validate_sensitive_claims
from tenantchat.core.commands import BookingCommand, HandoffCommand, HandoffReason, LeadCommand
from tenantchat.core.errors import (
    BookingNotPermittedError,
    DomainError,
    MissingRequiredFieldsError,
    SlotUnavailableError,
    UnknownServiceError,
    ValidationError,
)
from tenantchat.core.guards import tool_permission
from tenantchat.core.metrics import (
    ROUTING_NONE_INTENT,
    ActionStatus,
    BlockReason,
    MetricName,
    Operation,
    ToolOutcome,
    TruncationKind,
    TurnOutcome,
)
from tenantchat.core.planning import ConversationTurn, RetrievalPlan, plan_query
from tenantchat.core.ports import (
    EvidenceBundle,
    EvidenceItem,
    EvidenceUnavailableError,
    IdempotencyKey,
)
from tenantchat.core.routing import (
    IntentName,
    RoutingDecision,
    RoutingOutcome,
    RoutingRule,
    clarify_question,
)
from tenantchat.core.tenant import TenantPolicy
from tenantchat.core.workflows import ToolResult, WorkflowState, WorkflowTransition
from tenantchat.orchestration.agents import AGENTS_VERSION, AgentSpec, AnswerBasis
from tenantchat.orchestration.dependencies import DispatchDependencies
from tenantchat.orchestration.model import MessageRole, ToolCall
from tenantchat.orchestration.otel import set_tenant_identity
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
    TranscriptEntry,
    assistant_entry,
    tool_entry,
)
from tenantchat.orchestration.state import TurnOutcome as TurnStatus
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

# Phrases that promise a callback or follow-up without a committed lead.
# Matched against the assistant's answer before publication; a match with
# no committed create_lead action means the answer must be refused.
_CALLBACK_PROMISE_RE = re.compile(
    r"\b(?:our team|a team member|someone)\s+will\s+(?:call|contact|reach out|follow up"
    r"|get back to|get in touch)",
    re.IGNORECASE,
)

# The tool label for a call whose name resolved to nothing: the model wrote a
# tool the graph does not know, and its free-text name must never become a
# label value.
UNKNOWN_TOOL_LABEL = "unknown"


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
    citation metadata resolved by the adapter, and pinned to the index
    generation and embedding model that produced it (`OBS-004`)."""
    return {
        "source_id": item.source_id,
        "title": item.title,
        "source_name": item.source_name,
        "location": item.location,
        "content": item.content,
        "document_id": str(item.document_id),
        "version_id": str(item.version_id),
        "generation_id": str(item.generation_id),
        "embedding_model": item.embedding_model,
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


def _routing_decision_dict(decision: RoutingDecision) -> dict[str, object]:
    """One routing decision as the checkpoint stores it: every candidate, not
    just the winner, because that is what distinguishes a misroute from a
    retrieval failure in the `OBS-004` taxonomy."""
    return {
        "policy_version": decision.policy_version,
        "outcome": decision.outcome.value,
        "rule": decision.rule.value,
        "chosen": decision.chosen.value if decision.chosen is not None else None,
        "confidence": decision.confidence,
        "direct_threshold": decision.direct_threshold,
        "clarify_threshold": decision.clarify_threshold,
        "conflict_gap": decision.conflict_gap,
        "candidates": [
            {
                "intent": candidate.intent.value,
                "score": candidate.score,
                "matched_signals": list(candidate.matched_signals),
            }
            for candidate in decision.candidates
        ],
    }


def _prompt_assembly_dict(outcome: AssemblyOutcome) -> dict[str, object]:
    """The one assembled prompt as the checkpoint stores it.

    ``messages`` mirrors the canonical form :attr:`AssembledPrompt.content_hash`
    hashes, so a stored turn record can reproduce the exact prompt the provider
    received and re-derive the hash from it (`OBS-004`).
    """
    return {
        "template_ref": outcome.prompt.template_ref,
        "content_hash": outcome.prompt.content_hash,
        "bindings": dict(outcome.prompt.bindings),
        "excluded": [
            {
                "kind": item.kind.value,
                "position": item.position,
                "reference": item.reference,
                "reason": item.reason.value,
                "tokens": item.tokens,
            }
            for item in outcome.excluded
        ],
        "messages": [
            {
                "role": message.role.value,
                "tool_call_id": message.tool_call_id,
                "tool_calls": [
                    {
                        "id": call.call_id,
                        "name": call.name,
                        "arguments": dict(call.arguments),
                    }
                    for call in message.tool_calls
                ],
                "segments": [
                    [segment.segment_id, segment.region.value, segment.text]
                    for segment in message.segments
                ],
            }
            for message in outcome.prompt.messages
        ],
    }


def _evidence_meta_dict(bundle: EvidenceBundle) -> dict[str, object]:
    """The retrieval manifest one turn was grounded in, as the checkpoint stores it.

    Everything here is safe metadata for the inference plane (`OBS-004`):
    versions, thresholds, budgets, and the index generation the candidates
    came from — never passage content.
    """
    return {
        "sufficient": bundle.sufficient,
        "retriever_version": bundle.retriever_version,
        "reranker": bundle.reranker,
        "min_evidence_score": bundle.min_evidence_score,
        "embedding_model": bundle.embedding_model,
        "generation_id": str(bundle.generation_id) if bundle.generation_id is not None else None,
        "filters": dict(bundle.filters),
        "budget": dict(bundle.budget),
        "parameters": dict(bundle.retriever_parameters),
    }


def _with_plan(meta: dict[str, object], plan: RetrievalPlan | None) -> dict[str, object]:
    """The evidence manifest plus the query that was actually issued (`RAG-006`).

    The plan carries the resolved standalone query, the mode, the carried
    entities, and how much history was consulted — content, so it rides the
    inference plane like the rest of the manifest. ``query`` duplicates
    ``plan.query`` so a reader that only wants the resolved query does not
    need to know the plan shape.
    """
    if plan is None:
        return meta
    return {**meta, "query": plan.query, "plan": plan.to_dict()}


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


def _claim_refusal_reply(policy: TenantPolicy) -> str:
    """The deterministic refusal when an answer's sensitive claims are unsupported.

    Like :func:`_abstention_reply`, server-written: the claim validator just
    proved the model's answer asserted something its own context cannot
    support, so the model's phrasing must not be republished in any form.
    """
    return (
        "I cannot confirm some of the details in what I was about to say, so I "
        f"will not say it. The team can confirm it — call {policy.phone}."
    )


def _uncommitted_promise_refusal(policy: TenantPolicy) -> str:
    """The reply when a callback promise was made without a committed lead.

    Server-written: the model promised someone would call, but the lead was
    not committed — publishing the promise would mislead the visitor into
    waiting for a call that will never come.
    """
    return (
        "I am not able to promise a callback right now. The team can still help — "
        f"please call {policy.phone}, or try again with your name and contact details "
        "so I can submit your request."
    )


def _callback_promise_uncommitted(content: str, committed: Sequence[CommittedAction]) -> bool:
    """Whether the answer promises a callback that was never committed."""
    if _CALLBACK_PROMISE_RE.search(content) is None:
        return False
    return all(action["action"] != ToolName.CREATE_LEAD.value for action in committed)


class DispatchNode(StrEnum):
    """Node names, closed so a routing function cannot invent an edge."""

    ROUTE = "route"
    MODEL = "model"
    TOOLS = "tools"
    CONFIRM_BOOKING = "confirm_booking"
    COMMIT_BOOKING = "commit_booking"
    CONFIRM_LEAD = "confirm_lead"
    COMMIT_LEAD = "commit_lead"
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


def conversation_history(state: DispatchState) -> list[ConversationTurn]:
    """The prior conversational turns, current message excluded (`RAG-006`).

    Everything before the current turn's message is history; the current turn
    is the last ``user`` entry, and any assistant/tool entries this turn has
    already produced come after it. Tool results are machine payloads, not
    conversational text, so only user and assistant turns are passed to the
    planner.
    """
    last_user = next(
        (
            len(state["transcript"]) - 1 - offset
            for offset, entry in enumerate(reversed(state["transcript"]))
            if entry["role"] == "user"
        ),
        None,
    )
    history: list[ConversationTurn] = []
    if last_user is None:
        return history
    for entry in state["transcript"][:last_user]:
        if entry["role"] in ("user", "assistant") and entry["content"].strip():
            history.append(ConversationTurn(role=entry["role"], content=entry["content"]))
    return history


def known_service_terms(policy: TenantPolicy) -> tuple[str, ...]:
    """The tenant's service vocabulary for query planning (`RAG-006`).

    Server-owned strings — slug, display name, and aliases — so the planner
    carries only approved terms out of history, never arbitrary visitor text.
    """
    return tuple(
        term for definition in policy.catalog.definitions for term in definition.match_keys()
    )


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


def _route_rule(state: DispatchState) -> RoutingRule | None:
    """The rule the router recorded for this turn, or ``None`` when it did not run.

    Unreadable is treated as "no rule" for the reason :func:`_agent_for` treats
    an unrecognized intent as no agent: the safe failure is an answer the graph
    can still defend, not a crash.
    """
    try:
        return RoutingRule(state["route_rule"])
    except ValueError:
        return None


def _requires_retrieved_evidence(agent: AgentSpec | None, rule: RoutingRule | None) -> bool:
    """Whether this turn's answer has to be grounded in retrieved passages.

    An agent that answers from tool results is never gated by retrieval: a
    booking is carried by its tools and its workflow record, and an empty
    knowledge base does not make it unbookable.

    A tenant-knowledge agent has two sources, and the routing rule says which
    one the message reached. ``MATCHED`` means the message carried evidence for
    the agent's own intent, and for the general agent that evidence set *is* the
    tenant's business facts — hours, opening times, pricing, phone, address,
    services — which are server-owned truth already bound into the prompt, so
    retrieval only enriches them (`BUG-009`). Any other rule placed the turn
    here without that evidence, which leaves approved documents as the only
    thing that can answer it.
    """
    if agent is None or agent.answers_from is not AnswerBasis.TENANT_KNOWLEDGE:
        return False
    return rule is not RoutingRule.MATCHED


def _confirmed_service_areas(transcript: Sequence[TranscriptEntry]) -> dict[str, bool]:
    """Every ZIP the service-area tool decided during the current turn.

    Scoped to the turn so a verdict cannot outlive the question it answered: a
    ZIP checked three turns ago must not silently ground a claim in this one.
    A call whose arguments or result will not parse is skipped, leaving the
    claim to fail rather than be admitted on a guess.
    """
    confirmed: dict[str, bool] = {}
    zip_by_call: dict[str, str] = {}
    for entry in _current_turn(transcript):
        for call in entry["tool_calls"]:
            if call["name"] != ToolName.CHECK_SERVICE_AREA.value:
                continue
            with suppress(json.JSONDecodeError, TypeError):
                arguments = json.loads(call["arguments_json"])
                if isinstance(arguments, Mapping):
                    zip_by_call[call["call_id"]] = str(arguments.get("zip", ""))
        if entry["role"] != "tool":
            continue
        zip_code = zip_by_call.get(entry["tool_call_id"])
        if not zip_code:
            continue
        with suppress(json.JSONDecodeError, TypeError):
            result = json.loads(entry["content"])
            if isinstance(result, Mapping) and isinstance(result.get("served"), bool):
                confirmed[zip_code] = bool(result["served"])
    return confirmed


def _current_turn(transcript: Sequence[TranscriptEntry]) -> list[TranscriptEntry]:
    """The entries written since the visitor's most recent message."""
    for index in range(len(transcript) - 1, -1, -1):
        if transcript[index]["role"] == "user":
            return list(transcript[index + 1 :])
    return list(transcript)


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

    def _observe(self, name: MetricName, value: float, *, labels: dict[str, str]) -> None:
        """Record one observation when a metrics port is composed.

        A replayed node re-observes the work it re-executes: observation is a
        count of executions, not a durable effect, so it has no idempotency
        key and never rides the domain services (`OBS-002`).
        """
        if self._deps.metrics is not None:
            self._deps.metrics.observe(name, value, labels=labels)

    def _observe_booking_commit(self, outcome: ToolOutcome, duration: float) -> None:
        """The booking commit as a tool execution, with its latency.

        The booking commit runs here rather than through the tools node, so
        the tool series for ``book_appointment`` is completed here: a
        committed attempt records ``succeeded`` and a refused one records
        ``refused``.
        """
        self._observe(
            MetricName.TOOL_CALLS,
            1,
            labels={"tool": ToolName.BOOK_APPOINTMENT.value, "outcome": outcome.value},
        )
        self._observe(
            MetricName.TOOL_LATENCY,
            duration,
            labels={"tool": ToolName.BOOK_APPOINTMENT.value, "outcome": outcome.value},
        )

    def _observe_lead_commit(self, outcome: ToolOutcome, duration: float) -> None:
        """The lead capture as a tool execution, with its latency.

        The capture runs here rather than through the tools node, so the tool
        series for ``create_lead`` is completed here: a committed attempt
        records ``succeeded`` and a refused one records ``refused``.
        """
        self._observe(
            MetricName.TOOL_CALLS,
            1,
            labels={"tool": ToolName.CREATE_LEAD.value, "outcome": outcome.value},
        )
        self._observe(
            MetricName.TOOL_LATENCY,
            duration,
            labels={"tool": ToolName.CREATE_LEAD.value, "outcome": outcome.value},
        )

    @staticmethod
    def _blocked_reply(reason: BlockReason, policy: TenantPolicy) -> str:
        """The deterministic reply for a request the policy refused.

        Server-written, never the model's: a policy block means the model must
        not see the input or its output must not be published, so quoting
        either would defeat the block. The phone number is the honest next
        step for every reason.
        """
        if reason is BlockReason.INPUT_TOO_LONG or reason is BlockReason.INPUT_BINARY:
            return (
                "I could not read that message. Please send a shorter text "
                f"message, or call {policy.phone}."
            )
        if reason is BlockReason.OUTPUT_TOO_LONG:
            return "I could not finish that answer. Please ask again, or call " f"{policy.phone}."
        return (
            "I have reached my usage limit for now, so I cannot keep answering. "
            f"The team can help — call {policy.phone}."
        )

    def _observe_policy_block(self, reason: BlockReason) -> None:
        self._observe(MetricName.POLICY_BLOCKS, 1, labels={"reason": reason.value})

    def _policy_blocked_update(
        self, state: DispatchState, policy: TenantPolicy, reason: BlockReason
    ) -> dict[str, Any]:
        """The turn update for a budget or content-policy refusal.

        Records the block as a measurable event and answers with the
        deterministic server reply. ``ANSWERED`` is the honest outcome class:
        the visitor did get an answer — the refusal — and the ``POLICY_BLOCKS``
        metric is what distinguishes it from a model-produced answer.
        """
        self._observe_policy_block(reason)
        logger.info(
            "assistant request blocked by policy",
            extra={"tenant_id": state["tenant_id"], "reason": reason.value},
        )
        return {
            "transcript": [assistant_entry(self._blocked_reply(reason, policy), [])],
            "rounds": state["rounds"] + 1,
            "turn_outcome": TurnStatus.ANSWERED.value,
        }

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
        # A tenant without booking does not offer it: booking and its
        # availability precursor leave the candidate set entirely, so the
        # recorded decision never shows them and the assistant is never put in
        # a position to solicit booking-only fields (`BUG-019`).
        policy = await self._deps.policies.policy(tenant_id)
        disabled_intents = (
            (IntentName.BOOKING, IntentName.AVAILABILITY) if not policy.booking_enabled else ()
        )
        decision = self._deps.routing.route(
            latest_visitor_message(state),
            previous_intent=previous,
            clarification_pending=clarification_pending,
            disabled_intents=disabled_intents,
        )
        self._observe(
            MetricName.ROUTING_DECISIONS,
            1,
            labels={
                "intent": (
                    decision.chosen.value if decision.chosen is not None else ROUTING_NONE_INTENT
                ),
                "outcome": decision.outcome.value,
                "rule": decision.rule.value,
            },
        )
        self._observe(MetricName.ROUTER_CONFIDENCE, decision.confidence, labels={})
        if decision.outcome is RoutingOutcome.CLARIFY:
            # A clarification ends the turn here: the question is the answer,
            # and the router declining to guess is a quality class of its own.
            self._observe(
                MetricName.TURN_OUTCOMES,
                1,
                labels={"outcome": TurnOutcome.CLARIFIED.value},
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
            "routing_decision": _routing_decision_dict(decision),
        }
        if decision.outcome is RoutingOutcome.CLARIFY:
            question = clarify_question(decision)
            return base | {
                "transcript": [assistant_entry(question, [])],
                "clarification_question": question,
                "turn_outcome": TurnStatus.CLARIFIED.value,
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
        set_tenant_identity(state["tenant_id"], state["session_id"])
        policy = await self._deps.policies.policy(state["tenant_id"])
        budget = policy.budgets or DEFAULT_TENANT_BUDGET
        ledger = self._deps.budgets
        tenant_id = state["tenant_id"]
        # `AI-002` input policy, checked before any retrieval or model spend: a
        # message outside the tenant's content limits must not reach a provider.
        input_verdict = check_input(latest_visitor_message(state), budget)
        if not input_verdict.allowed:
            return self._policy_blocked_update(state, policy, input_verdict.block_reason)
        # Pre-flight the token budget before spending retrieval work on a
        # tenant that is already out of money; the binding check happens again
        # at the model call, where concurrency is reserved.
        if ledger is not None:
            preflight = await ledger.check_token_budget(tenant_id, budget)
            if not preflight.allowed:
                return self._policy_blocked_update(state, policy, preflight.block_reason)
        agent = _agent_for(self._deps, state)
        rule = _route_rule(state)
        bundle, plan = await self._retrieve_evidence(state, policy)
        if self._should_abstain(agent, bundle, rule):
            self._observe(
                MetricName.TURN_OUTCOMES,
                1,
                labels={"outcome": TurnOutcome.ABSTAINED.value},
            )
            return self._abstention_update(state, policy, bundle, plan)
        evidence = tuple(
            PromptEvidence(source_id=item.source_id, title=item.title, content=item.content)
            for item in (bundle.items if bundle is not None else ())
        )
        outcome = _assemble_prompt(policy, state, evidence)
        for excluded in outcome.excluded:
            self._observe(
                MetricName.CONTEXT_TRUNCATION,
                1,
                labels={"kind": TruncationKind(excluded.kind.value).value},
            )
        if self._should_abstain_after_assembly(agent, bundle, outcome, rule):
            # The retrieval verdict passed, but the assembled prompt carried no
            # evidence segment — a budget cut, not a retriever failure. The
            # model must still not guess from an empty context.
            self._observe(
                MetricName.TURN_OUTCOMES,
                1,
                labels={"outcome": TurnOutcome.ABSTAINED.value},
            )
            return self._abstention_update(state, policy, bundle, plan)
        # The model is offered only the tools the routed agent may call; the
        # tools node enforces the same allowlist against whatever it sends.
        allowed = tuple(
            spec for spec in TOOL_SPECS if spec.name in (agent.tool_names if agent else ())
        )
        # `AI-002`: the model call is the spend that matters, so it is the one
        # thing that holds a concurrency slot, re-checking the budget at the
        # moment of spend and releasing the slot on every path out.
        if ledger is not None:
            entered = await ledger.enter_call(tenant_id, budget)
            if not entered.allowed:
                return self._policy_blocked_update(state, policy, entered.block_reason)
        else:
            entered = None
        try:
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
        finally:
            if ledger is not None and entered is not None:
                await ledger.exit_call(tenant_id)
        if ledger is not None:
            fired = await ledger.record_usage(
                tenant_id,
                budget,
                prompt_tokens=int(response.usage.get("prompt_tokens", 0)),
                completion_tokens=int(response.usage.get("completion_tokens", 0)),
            )
            for level in fired:
                logger.warning(
                    "tenant model-spend alert crossed",
                    extra={"tenant_id": tenant_id, "level": level.value},
                )
        # `AI-002` output policy: over-length model prose is refused whole,
        # never silently truncated — the visitor gets a server-written reply.
        if response.content.strip():
            output_verdict = check_output(response.content, budget)
            if not output_verdict.allowed:
                return self._policy_blocked_update(state, policy, output_verdict.block_reason)

        calls = tuple(response.tool_calls)
        if not calls and not response.content.strip():
            return {"failure": HandoffReason.UNRESOLVED.value, "rounds": state["rounds"] + 1}

        # An action is held for the customer's confirmation only when the
        # routed agent is allowed to perform it. A held call never passes
        # through the tools node, so allowing a hold for a tool outside the
        # agent's allowlist would let an injected call pause for (and commit)
        # an action the guard was meant to refuse.
        allowed_names = {spec.name for spec in allowed}
        booking = (
            next((call for call in calls if call.name == ToolName.BOOK_APPOINTMENT), None)
            if ToolName.BOOK_APPOINTMENT.value in allowed_names
            else None
        )
        # Only one action can await confirmation at a time, and the booking wins:
        # the tools node refuses a second lead in that response, so no consent
        # question is ever asked for a call that cannot be answered.
        lead = (
            next((call for call in calls if call.name == ToolName.CREATE_LEAD), None)
            if booking is None and ToolName.CREATE_LEAD.value in allowed_names
            else None
        )
        update: dict[str, Any] = {
            "transcript": [assistant_entry(response.content, [_store(call) for call in calls])],
            "rounds": state["rounds"] + 1,
            "model_name": response.model_name,
            "pending_booking": _store(booking) if booking is not None else None,
            "pending_lead": _store(lead) if lead is not None else None,
            "model_usage": dict(response.usage),
        }
        # The prompt of every model call is recorded, not only of the call that
        # produced content: a turn that spends its whole round budget on tool
        # calls and then escalates must still be reconstructible, and it had no
        # content-producing call to record. The last call's prompt wins, which
        # is the answering call whenever there was one.
        #
        # Evidence stays tied to the content-producing call on purpose: finalize
        # validates the published answer against the exact context it was
        # written from, so a later tool-only round must not replace it.
        update["prompt_assembly"] = _prompt_assembly_dict(outcome)
        produced_content = bool(response.content.strip())
        invocation: dict[str, object] = {
            "round": update["rounds"],
            "model_name": response.model_name,
            "usage": dict(response.usage),
            "prompt_assembly": update["prompt_assembly"],
            "produced_content": produced_content,
        }
        update["model_invocations"] = [invocation]
        if produced_content:
            update.update(self._evidence_update(bundle, outcome, plan))
        return update

    @staticmethod
    def _abstention_update(
        state: DispatchState,
        policy: TenantPolicy,
        bundle: EvidenceBundle | None,
        plan: RetrievalPlan | None,
    ) -> dict[str, Any]:
        update: dict[str, Any] = {
            "transcript": [assistant_entry(_abstention_reply(policy), [])],
            "rounds": state["rounds"] + 1,
            "turn_outcome": TurnStatus.ABSTAINED.value,
        }
        if bundle is not None:
            # The verdict that made the turn abstain must reach the trace even
            # though no model call ran: an `OBS-004` attribution reads "weak
            # candidates, insufficient" from the record alone.
            update["evidence"] = [_evidence_item_dict(item) for item in bundle.items]
            update["evidence_meta"] = _with_plan(_evidence_meta_dict(bundle), plan)
        return update

    async def _retrieve_evidence(
        self, state: DispatchState, policy: TenantPolicy
    ) -> tuple[EvidenceBundle | None, RetrievalPlan | None]:
        """The passages that may ground this turn, and the plan that found them.

        A retrieval failure is treated as "no evidence": an index that is down
        must make the assistant abstain for knowledge questions, never answer
        from nothing. The standalone query is the planner's resolution of the
        latest message against the authorized conversation state (`RAG-006`);
        the plan rides the evidence manifest so the turn is reconstructible.
        """
        source = self._deps.evidence
        if source is None:
            return None, None
        plan = plan_query(
            latest_visitor_message(state),
            tenant_id=state["tenant_id"],
            history=conversation_history(state),
            known_terms=known_service_terms(policy),
            workflow=state.get("routed_intent", ""),
        )
        try:
            bundle = await source.retrieve(tenant_id=state["tenant_id"], query=plan.query)
        except EvidenceUnavailableError:
            logger.warning(
                "retrieval unavailable for turn",
                extra={"tenant_id": state["tenant_id"], "turn_index": state["turn_index"]},
            )
            return (
                EvidenceBundle(
                    items=(),
                    sufficient=False,
                    retriever_version="unavailable",
                    reranker=None,
                    min_evidence_score=0.0,
                ),
                plan,
            )
        return bundle, plan

    @staticmethod
    def _should_abstain(
        agent: AgentSpec | None, bundle: EvidenceBundle | None, rule: RoutingRule | None
    ) -> bool:
        """Whether this turn must refuse rather than call the model.

        A turn that needs retrieved grounding and whose verdict says the pool
        holds nothing worth answering from gets the server-written refusal,
        never a model call over an empty context. A ``None`` bundle is a
        composition without retrieval, which answers as it did before
        `RAG-005`.
        """
        return (
            bundle is not None
            and not bundle.sufficient
            and _requires_retrieved_evidence(agent, rule)
        )

    @classmethod
    def _should_abstain_after_assembly(
        cls,
        agent: AgentSpec | None,
        bundle: EvidenceBundle | None,
        outcome: AssemblyOutcome,
        rule: RoutingRule | None,
    ) -> bool:
        """Whether admission dropped the evidence the verdict relied on.

        The verdict speaks about the retrieved pool; the model only sees what
        the prompt budget admitted. If nothing was admitted, the context is
        empty, and a turn that needs retrieved grounding must not be answered
        from it regardless of the verdict.
        """
        if bundle is None or not _requires_retrieved_evidence(agent, rule):
            return False
        return not any(
            segment.segment_id.startswith("evidence:")
            for segment in outcome.prompt.messages[0].segments
        )

    @staticmethod
    def _evidence_update(
        bundle: EvidenceBundle | None,
        outcome: AssemblyOutcome,
        plan: RetrievalPlan | None,
    ) -> dict[str, object]:
        """The evidence the answer this call produced was grounded in.

        Recorded only when the model produced content, so finalize validates
        the published answer against the context of the call that wrote it.
        ``evidence`` and ``evidence_ids`` name the *exact* context: the ids
        assembly admitted to the prompt, not the wider retrieved pool.
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
            "evidence_meta": _with_plan(_evidence_meta_dict(bundle), plan),
        }

    async def run_tools(self, state: DispatchState) -> dict[str, Any]:
        """Run every tool call except the one booking awaiting confirmation.

        Skipping is by call ID rather than by tool name. A model that proposed
        two bookings in one response gets one confirmation and an explicit
        refusal for the rest, instead of a second call that silently never
        receives a result.

        Execution is gated twice, both out of band from any prompt text
        (`RAG-007`): the tenant's server-owned policy (a booking-disabled
        tenant refuses the booking tools no matter what the model or an
        injected document says) and the routed agent's allowlist. A refused
        call is answered with a refusal payload and never executed, and the
        refusal code is recorded on the turn so enforcement is attributable.
        """
        policy = await self._deps.policies.policy(state["tenant_id"])
        pending_booking = state["pending_booking"]
        pending_lead = state["pending_lead"]
        awaiting = {
            call_id
            for call_id in (
                pending_booking["call_id"] if pending_booking is not None else None,
                pending_lead["call_id"] if pending_lead is not None else None,
            )
            if call_id is not None
        }
        agent = _agent_for(self._deps, state)
        entries = []
        committed: list[CommittedAction] = []
        results: list[ToolResult] = []
        fields: dict[str, str] = {}
        refused: list[str] = []

        for call in unanswered_tool_calls(state):
            if call["call_id"] in awaiting:
                continue
            tool = ToolName.resolve(call["name"])
            if tool is None:
                self._observe(
                    MetricName.TOOL_CALLS,
                    1,
                    labels={"tool": UNKNOWN_TOOL_LABEL, "outcome": ToolOutcome.REFUSED.value},
                )
                entries.append(
                    tool_entry(call["call_id"], _payload(error="unknown_tool", name=call["name"]))
                )
                continue
            verdict = tool_permission(
                call["name"],
                allowed_tools=agent.tool_names if agent is not None else (),
                policy=policy,
            )
            if not verdict.permitted:
                refused.append(verdict.refusal_code or "tool_refused")
                self._observe(
                    MetricName.TOOL_CALLS,
                    1,
                    labels={"tool": tool.value, "outcome": ToolOutcome.REFUSED.value},
                )
                entries.append(
                    tool_entry(
                        call["call_id"],
                        _payload(
                            error=verdict.refusal_code or "tool_refused",
                            name=call["name"],
                            allowed_tools=list(agent.tool_names) if agent is not None else [],
                        ),
                    )
                )
                continue
            if agent is None:
                # An agent-less composition gets an empty allowlist, so the
                # guard refused every tool above; nothing can reach here.
                continue
            started = time.monotonic()
            try:
                content, action, outcome = await self._run_one(state, policy, call)
            except Exception:
                # An unexpected tool failure escapes the node: the graph does
                # not catch it, and the metrics plane still records that it
                # happened before the turn dies.
                self._observe(
                    MetricName.TOOL_CALLS,
                    1,
                    labels={"tool": tool.value, "outcome": ToolOutcome.FAILED.value},
                )
                self._observe(
                    MetricName.TOOL_LATENCY,
                    time.monotonic() - started,
                    labels={"tool": tool.value, "outcome": ToolOutcome.FAILED.value},
                )
                raise
            duration = time.monotonic() - started
            self._observe(
                MetricName.TOOL_CALLS,
                1,
                labels={"tool": tool.value, "outcome": outcome.value},
            )
            self._observe(
                MetricName.TOOL_LATENCY,
                duration,
                labels={"tool": tool.value, "outcome": outcome.value},
            )
            entries.append(tool_entry(call["call_id"], content))
            results.append(ToolResult(call_id=call["call_id"], name=call["name"], result=content))
            fields.update(self._collected(agent, _arguments(call)))
            if action is not None:
                committed.append(action)
                if self._deps.budgets is not None:
                    await self._deps.budgets.record_action(
                        state["tenant_id"], turn_index=state["turn_index"]
                    )

        update: dict[str, Any] = {
            "transcript": entries,
            "committed": committed,
            "refused_tools": refused,
        }
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
            # The booking path never passes through the tools node, so the
            # permission guard cannot record its refusals; the domain's refusal
            # is the same enforcement event and belongs in the same record
            # (`RAG-007`).
            return {
                "transcript": [tool_entry(pending["call_id"], _error_payload(error))],
                "pending_booking": None,
                "refused_tools": [error.code],
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
            self._observe(
                MetricName.BUSINESS_ACTIONS,
                1,
                labels={
                    "operation": Operation.BOOKING.value,
                    "status": ActionStatus.DECLINED.value,
                },
            )
            return self._with_collected(
                {
                    "transcript": [tool_entry(call_id, _payload(status="declined_by_customer"))],
                    "pending_booking": None,
                },
                merged,
            )

        key = self._key(state, ToolName.BOOK_APPOINTMENT, call_id)
        started = time.monotonic()
        replay = await self._deps.bookings.find_replay(state["tenant_id"], key)
        if replay is not None:
            # The same attempt already booked this slot while the process was
            # away; re-validating would refuse it as "not offered" now that the
            # slot is taken by this very booking, so return the committed result.
            self._observe_booking_commit(ToolOutcome.SUCCEEDED, time.monotonic() - started)
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
                            key=str(key),
                        )
                    ],
                    "pending_booking": None,
                },
                merged,
            )

        try:
            command = await self._parse_booking(state, arguments=_arguments(pending))
            if self._deps.budgets is not None:
                # `AI-002`: a confirmed booking must not commit once the
                # tenant's action budget is spent. The refusal resumes the
                # workflow exactly like a slot that was taken while deciding.
                policy = await self._deps.policies.policy(tenant_id)
                action_verdict = await self._deps.budgets.check_action(
                    tenant_id,
                    policy.budgets or DEFAULT_TENANT_BUDGET,
                    turn_index=state["turn_index"],
                )
                if not action_verdict.allowed:
                    self._observe_booking_commit(ToolOutcome.REFUSED, time.monotonic() - started)
                    self._observe_policy_block(action_verdict.block_reason)
                    if workflow_id:
                        await self._deps.workflows.transition(
                            tenant_id=tenant_id,
                            session_id=session_id,
                            workflow_id=workflow_id,
                            transition=WorkflowTransition.RESUME,
                            payload={"error": "action_quota_exceeded"},
                            idempotency_key=self._workflow_key(state, "resume", call_id),
                        )
                    return self._with_collected(
                        {
                            "transcript": [
                                tool_entry(
                                    call_id,
                                    _payload(
                                        error="action_quota_exceeded",
                                        message="The tenant has reached its action limit.",
                                    ),
                                )
                            ],
                            "pending_booking": None,
                        },
                        merged,
                    )
            confirmation = await self._deps.bookings.confirm(
                command,
                session_id=state["session_id"],
                idempotency_key=key,
            )
        except DomainError as error:
            # Reachable when the slot was taken while the customer was deciding.
            self._observe_booking_commit(ToolOutcome.REFUSED, time.monotonic() - started)
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

        self._observe_booking_commit(ToolOutcome.SUCCEEDED, time.monotonic() - started)
        if self._deps.budgets is not None:
            await self._deps.budgets.record_action(tenant_id, turn_index=state["turn_index"])
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
                        key=str(key),
                    )
                ],
                "pending_booking": None,
            },
            merged,
        )

    async def confirm_lead(self, state: DispatchState) -> dict[str, Any]:
        """Validate the proposed lead, then pause for the customer's consent.

        A lead stores contact data, so `PRIV-001` gates it on a recorded grant
        and the pause is the consent question — the same shape as the booking
        confirmation, and the validation runs before the interrupt for the same
        reason: a lead that cannot be captured never reaches the customer as a
        question. The pause transition is keyed by the lead call ID so a replay
        is a no-op.
        """
        pending = state["pending_lead"]
        if pending is None:
            return {}

        arguments = _arguments(pending)
        policy = await self._deps.policies.policy(state["tenant_id"])
        try:
            LeadCommand.parse(
                policy,
                customer_name=text_argument(arguments, "customer_name"),
                contact=text_argument(arguments, "customer_phone_or_email"),
                service=text_argument(arguments, "service"),
                summary=text_argument(arguments, "summary"),
                address_or_zip=text_argument(arguments, "address_or_zip"),
                urgency=text_argument(arguments, "urgency"),
            )
        except DomainError as error:
            # The lead path never passes through the tools node, so the
            # permission guard cannot record its refusals; the domain's refusal
            # is the same enforcement event and belongs in the same record
            # (`RAG-007`).
            return {
                "transcript": [tool_entry(pending["call_id"], _error_payload(error))],
                "pending_lead": None,
                "refused_tools": [error.code],
            }

        confirmation = {
            "awaiting": "lead_confirmation",
            "service": str(arguments.get("service", "")),
            "customer_name": str(arguments.get("customer_name", "")),
            "contact": str(arguments.get("customer_phone_or_email", "")),
            "summary": str(arguments.get("summary", "")),
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
        return {"lead_approved": decision is BookingDecision.APPROVED}

    async def commit_lead(self, state: DispatchState) -> dict[str, Any]:
        """Capture the consented lead, exactly once."""
        pending = state["pending_lead"]
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

        if not state["lead_approved"]:
            # Declining is a normal turn, not an error: the workflow resumes and
            # the conversation keeps going.
            if workflow_id:
                await self._deps.workflows.transition(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    workflow_id=workflow_id,
                    transition=WorkflowTransition.RESUME,
                    payload={"decision": "declined"},
                    idempotency_key=self._workflow_key(state, "resume", call_id),
                )
            self._observe(
                MetricName.BUSINESS_ACTIONS,
                1,
                labels={
                    "operation": Operation.LEAD.value,
                    "status": ActionStatus.DECLINED.value,
                },
            )
            return self._with_collected(
                {
                    "transcript": [tool_entry(call_id, _payload(status="declined_by_customer"))],
                    "pending_lead": None,
                },
                merged,
            )

        started = time.monotonic()
        try:
            policy = await self._deps.policies.policy(tenant_id)
            if self._deps.budgets is not None:
                # `AI-002`: a consented lead must not capture once the tenant's
                # action budget is spent. The refusal resumes the workflow
                # exactly like a booking would.
                action_verdict = await self._deps.budgets.check_action(
                    tenant_id,
                    policy.budgets or DEFAULT_TENANT_BUDGET,
                    turn_index=state["turn_index"],
                )
                if not action_verdict.allowed:
                    self._observe_lead_commit(ToolOutcome.REFUSED, time.monotonic() - started)
                    self._observe_policy_block(action_verdict.block_reason)
                    if workflow_id:
                        await self._deps.workflows.transition(
                            tenant_id=tenant_id,
                            session_id=session_id,
                            workflow_id=workflow_id,
                            transition=WorkflowTransition.RESUME,
                            payload={"error": "action_quota_exceeded"},
                            idempotency_key=self._workflow_key(state, "resume", call_id),
                        )
                    return self._with_collected(
                        {
                            "transcript": [
                                tool_entry(
                                    call_id,
                                    _payload(
                                        error="action_quota_exceeded",
                                        message="The tenant has reached its action limit.",
                                    ),
                                )
                            ],
                            "pending_lead": None,
                        },
                        merged,
                    )
            content, action, outcome = await self._create_lead(
                state, policy, pending, _arguments(pending)
            )
        except DomainError as error:
            # Reachable when consent was withdrawn while the visitor was
            # deciding, or the contact was taken while deciding.
            self._observe_lead_commit(ToolOutcome.REFUSED, time.monotonic() - started)
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
                    "pending_lead": None,
                },
                merged,
            )

        self._observe_lead_commit(outcome, time.monotonic() - started)
        if self._deps.budgets is not None:
            await self._deps.budgets.record_action(tenant_id, turn_index=state["turn_index"])
        if workflow_id:
            await self._resume_and_complete(state, call_id, "approved")
        return self._with_collected(
            {
                "transcript": [tool_entry(call_id, content)],
                "committed": [action],
                "pending_lead": None,
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
        self._observe(
            MetricName.TURN_OUTCOMES,
            1,
            labels={"outcome": TurnOutcome.HANDED_OFF.value},
        )
        command = HandoffCommand.parse(
            policy,
            reason=reason.value,
            summary=(
                f"Assistant could not complete turn {state['turn_index']} "
                f"({reason.value}) after {state['rounds']} model calls."
            ),
        )
        key = self._key(state, ToolName.HANDOFF_TO_HUMAN, "escalation")
        ticket = await self._deps.handoffs.request(
            command,
            session_id=state["session_id"],
            idempotency_key=key,
        )
        # `AI-002`: a handoff is a business action and counts toward the
        # tenant's attribution, but it is deliberately never budget-gated — an
        # exhausted tenant must still reach a person.
        if self._deps.budgets is not None:
            await self._deps.budgets.record_action(
                state["tenant_id"], turn_index=state["turn_index"]
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
            # Recorded here rather than left to the trace: the round-budget
            # route into this node leaves `failure` empty, so a status derived
            # from residual state reads a handed-off turn as answered.
            "turn_outcome": TurnStatus.ESCALATED.value,
            "failure": reason.value,
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
                    key=str(key),
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

        Sensitive claims are validated deterministically (`RAG-007`): every
        dollar amount must appear in the admitted evidence or the tenant's
        approved prices, and every coverage/permit/insurance sentence must be
        substantially supported by an admitted passage. An answer that fails is
        refused whole — the model's prose is replaced with a server-written
        reply and the failing claims are recorded by kind and value only.

        Service-area claims are grounded on this turn's tool verdicts instead.
        No approved document states which ZIPs a tenant serves — the tenant's
        policy does, through ``check_service_area`` — so without them a true
        answer the tool had just produced was refused as unsupported.
        """
        for entry in reversed(state["transcript"]):
            if entry["role"] == "user":
                break
            if entry["role"] == "assistant" and entry["content"].strip():
                policy = await self._deps.policies.policy(state["tenant_id"])
                validation = validate_sensitive_claims(
                    entry["content"],
                    evidence_texts=[str(item["content"]) for item in state["evidence"]],
                    trusted_prices=[
                        price
                        for price in (policy.price_for(slug) for slug, _ in policy.approved_prices)
                        if price is not None
                    ],
                    confirmed_service_areas=_confirmed_service_areas(state["transcript"]),
                )
                if validation.verdict is ClaimVerdict.UNSUPPORTED:
                    # A refusal is its own quality class. Recording it here is
                    # what keeps the outcome metric a partition of every turn,
                    # and what lets `diagnose` attribute the turn at all: the
                    # model produced prose and the server would not publish it.
                    self._observe(
                        MetricName.TURN_OUTCOMES,
                        1,
                        labels={"outcome": TurnOutcome.ANSWER_REFUSED.value},
                    )
                    return {
                        "answer": _claim_refusal_reply(policy),
                        "turn_outcome": TurnStatus.REFUSED.value,
                        "citations": [],
                        "citation_invalid": [],
                        "claims_invalid": [
                            {"kind": claim.kind.value, "value": claim.value}
                            for claim in validation.unsupported
                        ],
                    }
                if _callback_promise_uncommitted(entry["content"], state["committed"]):
                    self._observe(
                        MetricName.TURN_OUTCOMES,
                        1,
                        labels={"outcome": TurnOutcome.ANSWER_REFUSED.value},
                    )
                    return {
                        "answer": _uncommitted_promise_refusal(policy),
                        "turn_outcome": TurnStatus.REFUSED.value,
                        "citations": [],
                        "citation_invalid": [],
                    }
                found = citation_ids(entry["content"])
                context = frozenset(state["evidence_ids"])
                by_id = {str(item["source_id"]): item for item in state["evidence"]}
                citations = [
                    _citation_dict(by_id[source_id])
                    for source_id in found
                    if source_id in context and source_id in by_id
                ]
                published: dict[str, Any] = {
                    "answer": strip_citation_markers(entry["content"]),
                    "citations": citations,
                    "citation_invalid": [
                        source_id for source_id in found if source_id not in context
                    ],
                }
                # A clarification question and the deterministic abstention
                # reply are answers too, but their outcome classes were
                # recorded where they were decided: recording `answered` here
                # as well would double-count the same turn, and overwriting
                # `turn_outcome` would relabel it.
                if not state["clarification_question"] and entry["content"] != _abstention_reply(
                    policy
                ):
                    self._observe(
                        MetricName.TURN_OUTCOMES,
                        1,
                        labels={"outcome": TurnOutcome.ANSWERED.value},
                    )
                    published["turn_outcome"] = TurnStatus.ANSWERED.value
                return published
        # No assistant content anywhere in this turn. The customer still gets a
        # reply, so the turn is answered — by the server rather than the model.
        # It is counted for the same reason every other terminal path is: an
        # outcome distribution with a hole in it is not a distribution.
        policy = await self._deps.policies.policy(state["tenant_id"])
        self._observe(
            MetricName.TURN_OUTCOMES,
            1,
            labels={"outcome": TurnOutcome.ANSWERED.value},
        )
        return {
            "answer": f"I can help with that — the team is on {policy.phone}.",
            "turn_outcome": TurnStatus.ANSWERED.value,
        }

    async def _run_one(
        self,
        state: DispatchState,
        policy: TenantPolicy,
        call: StoredToolCall,
    ) -> tuple[str, CommittedAction | None, ToolOutcome]:
        arguments = _arguments(call)
        tool = ToolName.resolve(call["name"])
        try:
            if tool is ToolName.CHECK_SERVICE_AREA:
                return self._check_service_area(policy, arguments), None, ToolOutcome.SUCCEEDED
            if tool is ToolName.GET_AVAILABILITY:
                return await self._get_availability(policy, arguments), None, ToolOutcome.SUCCEEDED
            if tool is ToolName.CREATE_LEAD:
                # A second lead in one response. Only one can await consent, and
                # capturing this one after the customer answered about the other
                # would store contact data nobody agreed to.
                return (
                    _payload(
                        error="lead_already_proposed",
                        message="Only one callback request can be confirmed at a time.",
                    ),
                    None,
                    ToolOutcome.REFUSED,
                )
            if tool is ToolName.HANDOFF_TO_HUMAN:
                return await self._handoff(state, policy, call, arguments)
            if tool is ToolName.BOOK_APPOINTMENT:
                # A second booking in one response. Only one can be confirmed,
                # and confirming this one after the customer answered about the
                # other would be a booking nobody agreed to.
                return (
                    _payload(
                        error="booking_already_proposed",
                        message="Only one booking can be confirmed at a time.",
                    ),
                    None,
                    ToolOutcome.REFUSED,
                )
        except DomainError as error:
            return _error_payload(error), None, ToolOutcome.REFUSED
        return _payload(error="unknown_tool", name=call["name"]), None, ToolOutcome.REFUSED

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
        slot_labels = [slot.label for slot in slots]
        formatted = "\n".join(f"{i}. {label}" for i, label in enumerate(slot_labels, 1))
        return _payload(
            service=service.display_name,
            slots=slot_labels,
            formatted=f"Available {service.display_name} slots:\n\n{formatted}",
        )

    async def _create_lead(
        self,
        state: DispatchState,
        policy: TenantPolicy,
        call: StoredToolCall,
        arguments: Mapping[str, object],
    ) -> tuple[str, CommittedAction | None, ToolOutcome]:
        command = LeadCommand.parse(
            policy,
            customer_name=text_argument(arguments, "customer_name"),
            contact=text_argument(arguments, "customer_phone_or_email"),
            service=text_argument(arguments, "service"),
            summary=text_argument(arguments, "summary"),
            address_or_zip=text_argument(arguments, "address_or_zip"),
            urgency=text_argument(arguments, "urgency"),
        )
        key = self._key(state, ToolName.CREATE_LEAD, call["call_id"])
        receipt = await self._deps.leads.capture(
            command,
            session_id=state["session_id"],
            idempotency_key=key,
        )
        return (
            _payload(status="created", lead_id=receipt.reference, phone=policy.phone),
            CommittedAction(
                action=ToolName.CREATE_LEAD.value,
                reference=receipt.reference,
                replayed=receipt.replayed,
                key=str(key),
            ),
            ToolOutcome.SUCCEEDED,
        )

    async def _handoff(
        self,
        state: DispatchState,
        policy: TenantPolicy,
        call: StoredToolCall,
        arguments: Mapping[str, object],
    ) -> tuple[str, CommittedAction | None, ToolOutcome]:
        command = HandoffCommand.parse(
            policy,
            reason=text_argument(arguments, "reason"),
            summary=text_argument(arguments, "summary"),
        )
        key = self._key(state, ToolName.HANDOFF_TO_HUMAN, call["call_id"])
        ticket = await self._deps.handoffs.request(
            command,
            session_id=state["session_id"],
            idempotency_key=key,
        )
        return (
            _payload(status="created", handoff_id=ticket.reference, phone=policy.phone),
            CommittedAction(
                action=ToolName.HANDOFF_TO_HUMAN.value,
                reference=ticket.reference,
                replayed=ticket.replayed,
                key=str(key),
            ),
            ToolOutcome.SUCCEEDED,
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

    # Everything except the actions awaiting confirmation runs first, so no call
    # is left without a result across an interrupt the customer may take minutes
    # to answer. Compared by call ID rather than tool name, because a second
    # booking or lead in the same response is one of the calls that has to run.
    pending_booking = state["pending_booking"]
    pending_lead = state["pending_lead"]
    awaiting = {
        call_id
        for call_id in (
            pending_booking["call_id"] if pending_booking is not None else None,
            pending_lead["call_id"] if pending_lead is not None else None,
        )
        if call_id is not None
    }
    if any(call["call_id"] not in awaiting for call in calls):
        return DispatchNode.TOOLS
    if pending_booking is not None:
        return DispatchNode.CONFIRM_BOOKING
    return DispatchNode.CONFIRM_LEAD


def route_after_tools(state: DispatchState) -> DispatchNode:
    """Confirm any proposed action before returning to the model."""
    if state["pending_booking"] is not None:
        return DispatchNode.CONFIRM_BOOKING
    if state["pending_lead"] is not None:
        return DispatchNode.CONFIRM_LEAD
    return DispatchNode.MODEL


def route_after_confirmation(state: DispatchState) -> DispatchNode:
    """A cleared ``pending_booking`` means validation refused it before asking."""
    if state["pending_booking"] is None:
        return DispatchNode.MODEL
    return DispatchNode.COMMIT_BOOKING


def route_after_lead_confirmation(state: DispatchState) -> DispatchNode:
    """A cleared ``pending_lead`` means validation refused it before asking."""
    if state["pending_lead"] is None:
        return DispatchNode.MODEL
    return DispatchNode.COMMIT_LEAD
