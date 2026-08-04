"""The prototype's tool loop, now expressed as graph nodes over domain services.

`server.py` decided policy inline: it checked ``bookingEnabled`` in the tool
handler, validated contacts with its own regex, and appended to a module-level
list. The behaviors these tests pin are the ones that had to survive that move —
a tenant's policy still refuses, an incomplete request still asks rather than
fails, and the record still lands in a store the graph does not own.
"""

from __future__ import annotations

import asyncio
import json

from tenantchat.orchestration.model import ModelResponse, ToolCall
from tenantchat.orchestration.nodes import MAX_TOOL_ROUNDS, unanswered_tool_calls
from tests.agent_runtime.conftest import (
    BOOKING_TENANT,
    LEAD_TENANT,
    OFFERED_SLOT,
    RuntimeHarness,
    booking_arguments,
    build_harness,
    tool_call,
)

ANSWER = ModelResponse(content="Anything else I can help with?", model_name="scripted")


def calling(*calls: object, content: str = "") -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),  # type: ignore[arg-type]
        model_name="scripted",
    )


def tool_payloads(harness: RuntimeHarness) -> list[dict[str, object]]:
    """Every distinct tool result the graph handed back to the model."""
    seen: list[dict[str, object]] = []
    for call in harness.model.calls:
        for message in call:
            if message.role == "tool":
                payload = json.loads(message.content)
                if payload not in seen:
                    seen.append(payload)
    return seen


def test_a_service_area_question_is_answered_from_tenant_policy() -> None:
    """The ZIP list is private configuration, so the tool answers yes or no only."""
    harness = build_harness([calling(tool_call("check_service_area", zip="97205")), ANSWER])

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-area", "do you serve 97205?")

        assert tool_payloads(harness) == [
            {"served": True, "zip": "97205", "phone": "(555) 816-4420"}
        ]

    asyncio.run(scenario())


def test_availability_returns_only_what_the_tenant_is_offering() -> None:
    harness = build_harness([calling(tool_call("get_availability", service="HVAC")), ANSWER])

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-avail", "when can you come?")

        payload = tool_payloads(harness)[0]
        assert payload["service"] == "HVAC"
        assert OFFERED_SLOT in payload["slots"]  # type: ignore[operator]

    asyncio.run(scenario())


def test_a_tenant_that_does_not_book_refuses_availability_with_its_domain_code() -> None:
    """The refusal comes from ``TenantPolicy``, not from a check in the node.

    The same rule refuses a direct API call and an operator action. A node that
    re-implemented it would be a fourth copy, and the one most likely to drift.
    """
    harness = build_harness([calling(tool_call("get_availability", service="HVAC")), ANSWER])

    async def scenario() -> None:
        await harness.runtime.send(LEAD_TENANT, "s-refused", "can I book?")

        assert tool_payloads(harness)[0]["error"] == "booking_not_permitted"

    asyncio.run(scenario())


def test_an_unknown_service_is_answered_with_the_ones_that_exist() -> None:
    """An unresolved service is a question to ask, not a turn to abandon."""
    harness = build_harness([calling(tool_call("get_availability", service="roofing")), ANSWER])

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-unknown", "book roofing")

        payload = tool_payloads(harness)[0]
        assert payload["error"] == "unknown_service"
        assert "HVAC" in payload["offered_services"]  # type: ignore[operator]

    asyncio.run(scenario())


def test_a_lead_is_captured_through_the_domain_service() -> None:
    harness = build_harness(
        [
            calling(
                tool_call(
                    "create_lead",
                    customer_name="Dana Ruiz",
                    customer_phone_or_email="dana@example.com",
                    service="HVAC",
                    summary="Furnace is making a grinding noise.",
                    urgency="today",
                )
            ),
            ANSWER,
        ]
    )

    async def scenario() -> None:
        result = await harness.runtime.send(LEAD_TENANT, "s-lead", "have someone call me")

        captured = await harness.leads.for_tenant(LEAD_TENANT)
        assert len(captured) == 1
        assert captured[0].contact.value == "dana@example.com"
        assert [action["action"] for action in result.committed] == ["create_lead"]

    asyncio.run(scenario())


def test_a_lead_missing_a_contact_asks_instead_of_failing() -> None:
    """An incomplete request is an ordinary conversational state.

    The domain reports every outstanding field at once, so the assistant can ask
    one question rather than discovering the next gap on the following turn.
    """
    harness = build_harness(
        [
            calling(
                tool_call(
                    "create_lead",
                    customer_name="Dana Ruiz",
                    service="HVAC",
                    summary="Furnace is making a grinding noise.",
                )
            ),
            ANSWER,
        ]
    )

    async def scenario() -> None:
        await harness.runtime.send(LEAD_TENANT, "s-partial", "have someone call me")

        payload = tool_payloads(harness)[0]
        assert payload["error"] == "missing_required_fields"
        assert payload["missing_fields"] == ["contact"]
        assert await harness.leads.for_tenant(LEAD_TENANT) == ()

    asyncio.run(scenario())


def test_a_domain_refusal_never_carries_operator_detail_to_the_model() -> None:
    """``DomainError.detail`` names tenants and quotes input; the model sees neither."""
    harness = build_harness([calling(tool_call("get_availability", service="roofing")), ANSWER])

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-detail", "book roofing")

        payload = tool_payloads(harness)[0]
        assert set(payload) == {"error", "message", "offered_services"}
        assert BOOKING_TENANT not in json.dumps(payload)

    asyncio.run(scenario())


def test_an_unrecognized_tool_name_is_reported_rather_than_raised() -> None:
    """A model can invent a tool; a graph that raised on one would end the turn."""
    harness = build_harness([calling(tool_call("cancel_everything")), ANSWER])

    async def scenario() -> None:
        result = await harness.runtime.send(LEAD_TENANT, "s-invented", "cancel it all")

        assert tool_payloads(harness)[0]["error"] == "unknown_tool"
        assert result.answer == ANSWER.content

    asyncio.run(scenario())


def test_a_handoff_request_opens_a_ticket() -> None:
    harness = build_harness(
        [
            calling(
                tool_call(
                    "handoff_to_human",
                    reason="customer_request",
                    summary="Customer asked for a person.",
                )
            ),
            ANSWER,
        ]
    )

    async def scenario() -> None:
        await harness.runtime.send(LEAD_TENANT, "s-handoff", "let me talk to someone")

        tickets = await harness.handoffs.for_tenant(LEAD_TENANT)
        assert len(tickets) == 1
        assert tickets[0].reason == "customer_request"

    asyncio.run(scenario())


def test_a_looping_model_is_stopped_and_escalated() -> None:
    """The round budget exists so a loop costs the customer one handoff, not a session."""
    harness = build_harness([calling(tool_call("check_service_area", zip="97205"))])

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-loop", "hello?")

        assert harness.model.call_count == MAX_TOOL_ROUNDS
        assert "(555) 816-4420" in result.answer
        tickets = await harness.handoffs.for_tenant(BOOKING_TENANT)
        assert len(tickets) == 1
        assert tickets[0].reason == "unresolved"

    asyncio.run(scenario())


def test_a_provider_failure_becomes_a_handoff_rather_than_a_retry() -> None:
    """`REL-001` owns retry. A graph that retried as well would multiply it."""
    harness = build_harness([ANSWER])
    harness.model.failure = TimeoutError("provider did not respond")

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-down", "are you open?")

        assert harness.model.call_count == 1
        tickets = await harness.handoffs.for_tenant(BOOKING_TENANT)
        assert len(tickets) == 1
        assert tickets[0].reason == "tool_failure"
        assert "(555) 816-4420" in result.answer

    asyncio.run(scenario())


def test_two_tenants_using_the_same_session_id_do_not_share_a_conversation() -> None:
    """Thread keys are tenant-qualified, so a guessed session ID reaches nothing.

    Until `SEC-002` issues visitor credentials the session ID is client-supplied,
    which makes a collision — accidental or deliberate — a question of when.
    """
    harness = build_harness([ModelResponse(content="First tenant answer.", model_name="scripted")])

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "shared", "hello")
        other = await harness.runtime.snapshot(LEAD_TENANT, "shared")

        assert other == {}

    asyncio.run(scenario())


def test_a_second_booking_in_one_response_is_refused_rather_than_ignored() -> None:
    """Only one booking can be confirmed, and the other call still needs an answer.

    A tool call the graph silently walked past stays in the transcript with no
    result, and the next turn sends it to a provider that rejects the whole
    request. The refusal is also the honest answer: confirming the second
    booking after the customer answered about the first books something nobody
    agreed to.
    """
    harness = build_harness(
        [
            calling(
                tool_call("book_appointment", **booking_arguments()),
                ToolCall(
                    call_id="call-second-booking",
                    name="book_appointment",
                    arguments=booking_arguments() | {"slot": "Tue Jul 2, 8:30 AM"},
                ),
            ),
            ANSWER,
        ]
    )

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-two-bookings", "book both")
        state = await harness.checkpointed_state(BOOKING_TENANT, "s-two-bookings")

        # Read from the checkpoint rather than from what the model was sent: the
        # run paused at the confirmation, so the refusal has been written but not
        # yet delivered.
        assert result.is_paused
        assert [
            json.loads(entry["content"]) for entry in state["transcript"] if entry["role"] == "tool"
        ] == [
            {
                "error": "booking_already_proposed",
                "message": "Only one booking can be confirmed at a time.",
            }
        ]
        assert unanswered_tool_calls(state) == (state["transcript"][1]["tool_calls"][0],)

    asyncio.run(scenario())


def test_an_abandoned_turn_leaves_no_tool_call_unanswered() -> None:
    """Escalating mid-loop must not poison the next turn's transcript.

    Every provider requires a result for each tool call before the conversation
    continues. The customer can keep typing after a handoff, so the calls this
    turn walked away from are closed out rather than left dangling.
    """
    harness = build_harness([calling(tool_call("check_service_area", zip="97205"))])

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-abandoned", "hello?")
        state = await harness.checkpointed_state(BOOKING_TENANT, "s-abandoned")

        assert unanswered_tool_calls(state) == ()
        assert any(
            entry["role"] == "tool" and "turn_abandoned" in entry["content"]
            for entry in state["transcript"]
        )

    asyncio.run(scenario())
