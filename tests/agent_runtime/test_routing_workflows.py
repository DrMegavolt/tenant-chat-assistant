"""The `AGENT-001` state machine, end to end: routing, workflows, and recovery.

Each test here is one acceptance criterion of `AGENT-001`, read against the
durable records rather than the checkpoint: the routing decision is persisted
whole, the workflow moves through its states, a replayed node commits nothing
twice, and low-confidence or conflicting intents clarify or hand off safely.
The stores are the in-memory fakes with the real idempotent services on top,
exactly like the rest of this suite.
"""

from __future__ import annotations

import asyncio
import json

from tenantchat.core.routing import IntentName, RoutingOutcome, RoutingRule
from tenantchat.orchestration.model import ModelResponse
from tenantchat.orchestration.nodes import DispatchNodes, _callback_promise_uncommitted
from tenantchat.orchestration.state import CommittedAction, initial_state
from tenantchat.orchestration.tools import ToolName
from tests.agent_runtime.conftest import (
    BOOKING_TENANT,
    LEAD_TENANT,
    OFFERED_SLOT,
    RuntimeHarness,
    booking_arguments,
    build_harness,
    tool_call,
)


def booking_script() -> list[ModelResponse]:
    return [
        ModelResponse(
            content="",
            tool_calls=(tool_call("book_appointment", **booking_arguments()),),
            model_name="scripted",
        ),
        ModelResponse(content="You are booked for Monday at 2pm.", model_name="scripted"),
    ]


def collecting_script() -> list[ModelResponse]:
    """A model that asks for the missing details instead of acting."""
    return [ModelResponse(content="Which service would you like to book?", model_name="scripted")]


def answering_model(answer: str = "We are open until 7pm.") -> list[ModelResponse]:
    return [ModelResponse(content=answer, model_name="scripted")]


def tool_payloads(harness: RuntimeHarness) -> list[dict[str, object]]:
    """Every distinct tool payload the graph handed back to the model."""
    seen: list[dict[str, object]] = []
    for prompt in harness.model.calls:
        for message in prompt.messages:
            if message.role == "tool":
                payload = json.loads(message.content)
                if payload not in seen:
                    seen.append(payload)
    return seen


# --- happy path: a booking routes, pauses, and completes --------------------


def test_a_booking_turn_records_the_whole_routing_decision() -> None:
    """The acceptance criterion for the record: everything the router decided.

    The record must be enough to diagnose a misroute on its own — the chosen
    intent, every candidate with its score, the confidence, the policy version,
    and the thresholds applied.
    """
    harness = build_harness(booking_script())

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-record", "book HVAC Monday")

        decisions = await harness.workflows.routing_decisions(BOOKING_TENANT, "s-record")
        assert len(decisions) == 1
        record = decisions[0]
        assert record.outcome is RoutingOutcome.DIRECT
        assert record.rule is RoutingRule.MATCHED
        assert record.chosen_intent is IntentName.BOOKING
        assert record.policy_version == "intent-routing@1"
        assert record.direct_threshold == 4.0
        assert record.confidence > 0
        # Every intent is a scored candidate, so a loser is diagnosable: it
        # either never had evidence (score 0) or was scored and beaten.
        assert {candidate.intent for candidate in record.candidates} == set(IntentName)
        booking = next(c for c in record.candidates if c.intent is IntentName.BOOKING)
        assert record.confidence == booking.score

    asyncio.run(scenario())


def test_a_booking_workflow_pauses_with_its_confirmation_and_completes() -> None:
    harness = build_harness(booking_script())

    async def scenario() -> None:
        paused = await harness.runtime.send(BOOKING_TENANT, "s-flow", "book HVAC Monday")
        assert paused.is_paused

        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-flow")
        assert len(workflows) == 1
        paused_row = workflows[0]
        assert paused_row.status.value == "paused"
        assert paused_row.pending_confirmation is not None
        assert paused_row.pending_confirmation["awaiting"] == "booking_confirmation"
        assert paused_row.intent is IntentName.BOOKING
        assert paused_row.next_allowed_actions == (
            ToolName.GET_AVAILABILITY.value,
            ToolName.BOOK_APPOINTMENT.value,
        )

        resumed = await harness.runtime.resume(BOOKING_TENANT, "s-flow", "approved")
        assert [action["action"] for action in resumed.committed] == ["book_appointment"]

        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-flow")
        assert len(workflows) == 1
        done = workflows[0]
        assert done.status.value == "completed"
        assert done.completed_at is not None
        assert done.pending_confirmation is None
        assert done.collected_fields["customer_name"] == "Dana Ruiz"
        assert done.collected_fields["slot"] == OFFERED_SLOT

        events = await harness.workflows.events(BOOKING_TENANT, done.workflow_id)
        assert [event.kind for event in events] == [
            "start",
            "pause",
            "update",
            "resume",
            "complete",
        ]
        assert len(await harness.bookings.for_tenant(BOOKING_TENANT)) == 1

    asyncio.run(scenario())


def test_a_declined_confirmation_resumes_the_workflow_without_completing_it() -> None:
    """Declining is a normal turn: the workflow goes back to active."""
    harness = build_harness(
        [
            booking_script()[0],
            ModelResponse(content="No problem — shall I look at Wednesday?", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-decline", "book HVAC Monday")
        result = await harness.runtime.resume(BOOKING_TENANT, "s-decline", "declined")

        assert result.committed == ()
        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-decline")
        assert len(workflows) == 1
        assert workflows[0].status.value == "active"
        assert workflows[0].pending_confirmation is None

    asyncio.run(scenario())


# --- replay safety -----------------------------------------------------------


def test_replaying_the_route_node_records_no_second_decision_or_workflow() -> None:
    """A crash between the route node's effects and its checkpoint write.

    The replay sees the workflow the first run opened and continues it: one
    routing record, one workflow row, one workflow ID on both passes.
    """
    harness = build_harness(answering_model("ok"))
    nodes = DispatchNodes(harness.dependencies)

    async def scenario() -> None:
        state = initial_state(BOOKING_TENANT, "s-replay-route", "book HVAC Monday")
        first = await nodes.route(state)
        replayed = await nodes.route(state)

        assert replayed["workflow_id"] == first["workflow_id"]
        decisions = await harness.workflows.routing_decisions(BOOKING_TENANT, "s-replay-route")
        assert len(decisions) == 1
        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-replay-route")
        assert len(workflows) == 1

    asyncio.run(scenario())


def test_replaying_the_commit_node_books_nothing_new_and_keeps_one_workflow() -> None:
    """`ADR-0001`'s promise, now checked against the workflow records too."""
    harness = build_harness(booking_script())

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-replay-commit", "book HVAC Monday")
        await harness.runtime.resume(BOOKING_TENANT, "s-replay-commit", "approved")

        state = await harness.checkpointed_state(BOOKING_TENANT, "s-replay-commit")
        replay: dict[str, object] = dict(state) | {
            "pending_booking": next(
                call
                for entry in reversed(state["transcript"])
                for call in entry["tool_calls"]
                if call["name"] == "book_appointment"
            ),
            "booking_approved": True,
        }
        await DispatchNodes(harness.dependencies).commit_booking(replay)  # type: ignore[arg-type]

        assert len(await harness.bookings.for_tenant(BOOKING_TENANT)) == 1
        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-replay-commit")
        assert len(workflows) == 1
        events = await harness.workflows.events(BOOKING_TENANT, workflows[0].workflow_id)
        assert [event.kind for event in events] == [
            "start",
            "pause",
            "update",
            "resume",
            "complete",
        ]

    asyncio.run(scenario())


def test_workflow_records_survive_a_restart_and_the_turn_completes_once() -> None:
    """The durable records are what a redeployed process still sees."""
    before = build_harness(booking_script())

    async def scenario() -> None:
        paused = await before.runtime.send(BOOKING_TENANT, "s-restart", "book HVAC Monday")
        assert paused.is_paused

        after = before.restarted()
        resumed = await after.runtime.resume(BOOKING_TENANT, "s-restart", "approved")
        assert [action["action"] for action in resumed.committed] == ["book_appointment"]

        workflows = await after.workflows.workflows(BOOKING_TENANT, "s-restart")
        assert len(workflows) == 1
        assert workflows[0].status.value == "completed"
        decisions = await after.workflows.routing_decisions(BOOKING_TENANT, "s-restart")
        assert len(decisions) == 1
        assert len(await after.bookings.for_tenant(BOOKING_TENANT)) == 1

    asyncio.run(scenario())


def test_deleting_every_checkpoint_loses_no_routing_or_workflow_record() -> None:
    """The checkpoint is a resume point; the records are not."""
    harness = build_harness(booking_script())

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-wipe", "book HVAC Monday")
        await harness.runtime.resume(BOOKING_TENANT, "s-wipe", "approved")

        wiped = build_harness(
            answering_model(),
            bookings=harness.bookings,
            leads=harness.leads,
            handoffs=harness.handoffs,
            idempotency=harness.idempotency,
            workflows=harness.workflows,
        )
        decisions = await wiped.workflows.routing_decisions(BOOKING_TENANT, "s-wipe")
        workflows = await wiped.workflows.workflows(BOOKING_TENANT, "s-wipe")
        assert len(decisions) == 1
        assert len(workflows) == 1
        assert workflows[0].status.value == "completed"
        assert len(await wiped.bookings.for_tenant(BOOKING_TENANT)) == 1
        assert await wiped.runtime.pending(BOOKING_TENANT, "s-wipe") is None

    asyncio.run(scenario())


# --- tool allowlists ---------------------------------------------------------


def test_a_specialized_agent_cannot_call_tools_outside_its_allowlist() -> None:
    """`AGENT-001`'s permission criterion: the refusal names the allowed set.

    A service-area turn whose model reaches for availability is refused
    without executing the tool, and the model is told what it may use.
    """
    harness = build_harness(
        [
            ModelResponse(
                content="",
                tool_calls=(tool_call("get_availability", service="HVAC"),),
                model_name="scripted",
            ),
            ModelResponse(content="Let me check that for you.", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-allowlist", "do you serve 97205?")

        refusal = tool_payloads(harness)[0]
        assert refusal["error"] == "tool_not_allowed"
        assert refusal["name"] == "get_availability"
        assert refusal["allowed_tools"] == ["check_service_area"]
        assert result.answer == "Let me check that for you."
        # No workflow exists for a single-turn intent, and nothing committed.
        assert await harness.workflows.workflows(BOOKING_TENANT, "s-allowlist") == ()
        assert result.committed == ()

    asyncio.run(scenario())


def test_the_model_is_only_offered_the_routed_agents_tools() -> None:
    """The allowlist is enforced at the boundary too: nothing else is offered."""
    harness = build_harness(answering_model())

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-offered", "do you serve 97205?")

        assert harness.model.offered_tools[0] == ("check_service_area",)

    asyncio.run(scenario())


# --- clarification and safe handoff -----------------------------------------


def test_an_ambiguous_message_asks_a_question_without_calling_the_model() -> None:
    harness = build_harness(answering_model("should never run"))

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-clarify", "when can I schedule")

        assert harness.model.call_count == 0
        assert "did you mean to" in result.answer
        decisions = await harness.workflows.routing_decisions(BOOKING_TENANT, "s-clarify")
        assert decisions[0].outcome is RoutingOutcome.CLARIFY
        assert decisions[0].chosen_intent is None

    asyncio.run(scenario())


def test_a_second_consecutive_ambiguity_hands_off_safely() -> None:
    """One question is generous; two in a row is a loop that ends in a person."""
    harness = build_harness(answering_model("should never run"))

    async def scenario() -> None:
        first = await harness.runtime.send(BOOKING_TENANT, "s-bounded", "when can I schedule")
        assert "did you mean to" in first.answer

        second = await harness.runtime.send(BOOKING_TENANT, "s-bounded", "when can I schedule")

        tickets = await harness.handoffs.for_tenant(BOOKING_TENANT)
        assert len(tickets) == 1
        assert tickets[0].reason.value == "unresolved"
        assert "passed it to the team" in second.answer
        decisions = await harness.workflows.routing_decisions(BOOKING_TENANT, "s-bounded")
        assert decisions[1].outcome is RoutingOutcome.HANDOFF
        assert decisions[1].rule is RoutingRule.BOUNDED_CLARIFY

    asyncio.run(scenario())


def test_an_answer_to_the_clarification_routes_and_proceeds() -> None:
    harness = build_harness(answering_model("Got it — booking it."))

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-answered", "when can I schedule")
        result = await harness.runtime.send(BOOKING_TENANT, "s-answered", "book it")

        assert harness.model.call_count == 1
        assert result.answer == "Got it — booking it."
        decisions = await harness.workflows.routing_decisions(BOOKING_TENANT, "s-answered")
        assert decisions[1].chosen_intent is IntentName.BOOKING

    asyncio.run(scenario())


# --- topic switching, cancellation, and continuation ------------------------


def test_a_topic_switch_suspends_the_active_workflow_with_its_state() -> None:
    """Hours question mid-booking: the booking is parked, not lost."""
    harness = build_harness(
        [
            ModelResponse(content="Which service would you like to book?", model_name="scripted"),
            ModelResponse(content="We are open until 7pm.", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-switch", "book HVAC")
        result = await harness.runtime.send(BOOKING_TENANT, "s-switch", "what are your hours?")

        assert result.answer == "We are open until 7pm."
        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-switch")
        assert len(workflows) == 1
        suspended = workflows[0]
        assert suspended.status.value == "suspended"
        assert suspended.intent is IntentName.BOOKING
        events = await harness.workflows.events(BOOKING_TENANT, suspended.workflow_id)
        assert events[-1].kind == "suspend"
        assert events[-1].payload["switched_to"] == "general"

    asyncio.run(scenario())


def test_a_booking_after_a_topic_switch_starts_a_fresh_workflow() -> None:
    """The suspended workflow stays suspended; the new booking is a new row."""
    harness = build_harness(
        [
            ModelResponse(content="Which service would you like to book?", model_name="scripted"),
            ModelResponse(content="We are open until 7pm.", model_name="scripted"),
            ModelResponse(content="Which day works for you?", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-resume-switch", "book HVAC")
        await harness.runtime.send(BOOKING_TENANT, "s-resume-switch", "what are your hours?")
        await harness.runtime.send(BOOKING_TENANT, "s-resume-switch", "book it for monday")

        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-resume-switch")
        assert len(workflows) == 2
        assert workflows[0].status.value == "suspended"
        assert workflows[1].status.value == "active"
        assert workflows[1].intent is IntentName.BOOKING

    asyncio.run(scenario())


def test_cancel_terminates_the_active_workflow_and_commits_nothing() -> None:
    harness = build_harness(
        [
            ModelResponse(content="Which service would you like to book?", model_name="scripted"),
            ModelResponse(content="Sure — nothing booked.", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-cancel", "book HVAC")
        result = await harness.runtime.send(BOOKING_TENANT, "s-cancel", "cancel the booking")

        assert result.committed == ()
        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-cancel")
        assert len(workflows) == 1
        assert workflows[0].status.value == "cancelled"
        events = await harness.workflows.events(BOOKING_TENANT, workflows[0].workflow_id)
        assert events[-1].kind == "cancel"
        assert await harness.bookings.for_tenant(BOOKING_TENANT) == ()

    asyncio.run(scenario())


def test_a_weak_followup_continues_the_active_workflow_and_says_so_in_the_record() -> None:
    """ "what about Tuesday?" mid-booking keeps the booking — and the record
    explains why: rule=continuation, chosen=booking, low confidence."""
    harness = build_harness(
        [
            ModelResponse(content="Which service would you like to book?", model_name="scripted"),
            ModelResponse(content="Tuesday is open — shall I book it?", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-continue", "book HVAC")
        result = await harness.runtime.send(BOOKING_TENANT, "s-continue", "what about tuesday?")

        assert result.answer == "Tuesday is open — shall I book it?"
        decisions = await harness.workflows.routing_decisions(BOOKING_TENANT, "s-continue")
        continuation = decisions[1]
        assert continuation.rule is RoutingRule.CONTINUATION
        assert continuation.chosen_intent is IntentName.BOOKING
        assert continuation.outcome is RoutingOutcome.DIRECT
        # One workflow, still active: continuation must not create a rival.
        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-continue")
        assert len(workflows) == 1
        assert workflows[0].status.value == "active"

    asyncio.run(scenario())


# --- failure recovery --------------------------------------------------------


def test_a_model_failure_fails_and_hands_off_the_active_workflow() -> None:
    harness = build_harness(
        [ModelResponse(content="Which service would you like to book?", model_name="scripted")]
    )
    harness.model.failure = TimeoutError("provider did not respond")

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-fail", "book HVAC")

        tickets = await harness.handoffs.for_tenant(BOOKING_TENANT)
        assert len(tickets) == 1
        assert tickets[0].reason.value == "tool_failure"
        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-fail")
        assert len(workflows) == 1
        assert workflows[0].status.value == "handed_off"
        events = await harness.workflows.events(BOOKING_TENANT, workflows[0].workflow_id)
        assert [event.kind for event in events] == ["start", "fail", "hand_off"]
        assert "(555) 816-4420" in result.answer

    asyncio.run(scenario())


def test_a_customer_requested_handoff_marks_the_workflow_handed_off() -> None:
    """A customer-requested handoff skips the failure mark: nothing failed."""
    harness = build_harness(
        [ModelResponse(content="Which service would you like to book?", model_name="scripted")]
    )

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-request", "book HVAC")
        routed = await harness.runtime.send(BOOKING_TENANT, "s-request", "let me talk to a person")

        assert "passed it to the team" in routed.answer
        tickets = await harness.handoffs.for_tenant(BOOKING_TENANT)
        assert len(tickets) == 1
        assert tickets[0].reason.value == "customer_request"
        workflows = await harness.workflows.workflows(BOOKING_TENANT, "s-request")
        assert workflows[0].status.value == "handed_off"
        events = await harness.workflows.events(BOOKING_TENANT, workflows[0].workflow_id)
        assert [event.kind for event in events] == ["start", "hand_off"]
        assert result.answer == "Which service would you like to book?"

    asyncio.run(scenario())


# --- leads -------------------------------------------------------------------


def test_a_lead_workflow_completes_with_the_captured_lead() -> None:
    harness = build_harness(
        [
            ModelResponse(
                content="",
                tool_calls=(
                    tool_call(
                        "create_lead",
                        customer_name="Dana Ruiz",
                        customer_phone_or_email="dana@example.com",
                        service="HVAC",
                        summary="Furnace is making a grinding noise.",
                        urgency="today",
                    ),
                ),
                model_name="scripted",
            ),
            ModelResponse(content="The team will call you back.", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        result = await harness.runtime.send(LEAD_TENANT, "s-lead", "have someone call me")

        assert [action["action"] for action in result.committed] == ["create_lead"]
        workflows = await harness.workflows.workflows(LEAD_TENANT, "s-lead")
        assert len(workflows) == 1
        done = workflows[0]
        assert done.status.value == "completed"
        assert done.intent is IntentName.LEAD
        assert done.collected_fields["customer_name"] == "Dana Ruiz"
        assert done.collected_fields["summary"] == "Furnace is making a grinding noise."
        assert len(done.tool_results) == 1
        events = await harness.workflows.events(LEAD_TENANT, done.workflow_id)
        assert [event.kind for event in events] == ["start", "update", "complete"]
        assert len(await harness.leads.for_tenant(LEAD_TENANT)) == 1

    asyncio.run(scenario())


def test_a_callback_request_with_service_nouns_routes_to_lead() -> None:
    """An explicit callback phrase wins over service-category words in routing.

    BUG-003: a message like "have someone call me about electrical repair"
    tied booking and lead scores because the service-category nouns ("electrical",
    "repair") weighed as much as the callback phrase. With the fix, the callback
    signal now outweighs the service nouns, so the message routes to lead and
    the agent collects the required fields.
    """
    harness = build_harness(
        [
            ModelResponse(
                content="",
                tool_calls=(
                    tool_call(
                        "create_lead",
                        customer_name="QA Tester",
                        customer_phone_or_email="qa-tester@example.invalid",
                        service="Electrical panel repair",
                        summary="Customer needs electrical panel repair and asked for a callback.",
                        urgency="unknown",
                    ),
                ),
                model_name="scripted",
            ),
            ModelResponse(content="Our team will contact you.", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        result = await harness.runtime.send(
            LEAD_TENANT,
            "s-callback",
            "Please have someone call QA Tester at qa-tester@example.invalid about "
            "an electrical panel repair.",
        )
        routing = await harness.workflows.last_routing(LEAD_TENANT, "s-callback")
        assert routing is not None
        assert routing.chosen_intent == IntentName.LEAD.value
        assert routing.rule == RoutingRule.MATCHED.value
        assert len(await harness.leads.for_tenant(LEAD_TENANT)) == 1
        assert [action["action"] for action in result.committed] == ["create_lead"]

    asyncio.run(scenario())


def test_callback_promise_without_committed_lead_is_refused() -> None:
    """An answer that promises a callback is refused when no lead was committed.

    BUG-003: the model can promise "our team will contact you" even when
    create_lead was not called. The finalize node must detect the uncommitted
    promise and replace the answer with a server-written refusal instead of
    misleading the visitor.
    """

    async def check(answer: str, committed: list[dict[str, object]], expect: bool) -> None:
        typed = [
            CommittedAction(
                action=str(c["action"]),
                reference=str(c.get("reference", "")),
                replayed=bool(c.get("replayed", False)),
                key=str(c.get("key", "")),
            )
            for c in committed
        ]
        assert _callback_promise_uncommitted(answer, tuple(typed)) is expect

    async def scenario() -> None:
        await check("Our team will contact you shortly.", [], True)
        await check("A team member will call you back.", [], True)
        await check("Someone will reach out to discuss your repair.", [], True)
        await check("Our team will contact you.", [{"action": "create_lead"}], False)
        await check("Our team will contact you.", [{"action": "handoff_to_human"}], True)
        await check("I can help with that.", [], False)
        await check("What is your phone number?", [], False)

    asyncio.run(scenario())
