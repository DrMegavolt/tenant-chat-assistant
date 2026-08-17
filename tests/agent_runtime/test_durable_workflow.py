"""What `ADR-0001` promised the checkpointer would buy.

Each test here corresponds to an ARCH-001 acceptance criterion, and they are
worth reading as the specification of the runtime's durability guarantees: a
workflow pauses and survives a deployment, a replayed node commits nothing
twice, and the business records do not depend on the checkpoint store existing
at all.
"""

from __future__ import annotations

import asyncio

from tenantchat.orchestration.model import ModelResponse
from tenantchat.orchestration.nodes import DispatchNodes
from tenantchat.orchestration.state import CommittedAction, DispatchState, StoredToolCall
from tests.agent_runtime.conftest import (
    BOOKING_TENANT,
    LEAD_TENANT,
    OFFERED_SLOT,
    RuntimeHarness,
    booking_arguments,
    build_harness,
    tool_call,
)


def booking_script(**overrides: object) -> list[ModelResponse]:
    """A model that proposes one booking, then reports it in prose."""
    return [
        ModelResponse(
            content="",
            tool_calls=(tool_call("book_appointment", **(booking_arguments() | overrides)),),
            model_name="scripted",
        ),
        ModelResponse(content="You are booked for Monday at 2pm.", model_name="scripted"),
    ]


def lead_script() -> list[ModelResponse]:
    """A model that proposes one lead, then reports it in prose."""
    return [
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


def test_a_booking_pauses_for_the_customer_before_anything_is_committed() -> None:
    """The interrupt is the only thing standing between a model and a van."""
    harness = build_harness(booking_script())

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-pause", "book HVAC Monday")

        assert result.is_paused
        assert result.pending is not None
        assert result.pending["awaiting"] == "booking_confirmation"
        assert result.pending["slot"] == OFFERED_SLOT
        assert await harness.bookings.for_tenant(BOOKING_TENANT) == ()

    asyncio.run(scenario())


def test_a_paused_booking_survives_a_restart_and_completes_once() -> None:
    """The whole point of the checkpointer, stated as one scenario.

    The resume runs on a runtime built after the pause, from a graph, nodes, and
    service objects the paused run never touched. Only the checkpointer and the
    stores cross the boundary, which is what a redeployed process actually
    keeps.
    """
    before = build_harness(booking_script())

    async def scenario() -> None:
        paused = await before.runtime.send(BOOKING_TENANT, "s-restart", "book HVAC Monday")
        assert paused.is_paused

        after = before.restarted()
        assert await after.runtime.pending(BOOKING_TENANT, "s-restart") is not None

        resumed = await after.runtime.resume(BOOKING_TENANT, "s-restart", "approved")

        assert not resumed.is_paused
        assert resumed.answer == "You are booked for Monday at 2pm."
        assert [action["action"] for action in resumed.committed] == ["book_appointment"]

        booked = await after.bookings.for_tenant(BOOKING_TENANT)
        assert len(booked) == 1
        assert booked[0].slot == OFFERED_SLOT

    asyncio.run(scenario())


def test_replaying_the_commit_node_books_nothing_new() -> None:
    """A forced replay commits nothing, because the key is derived, not fresh.

    Driving the node directly rather than the graph is deliberate: LangGraph
    will not resume a thread that has finished, so the only way to prove the
    *node* is replay-safe is to hand it the state it already committed from —
    which is exactly what a crash between the domain write and the checkpoint
    write would leave behind.
    """
    harness = build_harness(booking_script())

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-replay", "book HVAC Monday")
        first = await harness.runtime.resume(BOOKING_TENANT, "s-replay", "approved")

        replayed = await _replay_commit_node(harness, "s-replay")

        assert len(await harness.bookings.for_tenant(BOOKING_TENANT)) == 1
        assert replayed[0]["reference"] == first.committed[0]["reference"]
        assert replayed[0]["replayed"] is True
        assert first.committed[0]["replayed"] is False

    asyncio.run(scenario())


async def _replay_commit_node(harness: RuntimeHarness, session_id: str) -> list[CommittedAction]:
    """Re-run ``commit_booking`` against the state it already committed from."""
    state = await harness.checkpointed_state(BOOKING_TENANT, session_id)
    replay: DispatchState = state | {
        "pending_booking": _booking_call(state),
        "booking_approved": True,
    }
    result = await DispatchNodes(harness.dependencies).commit_booking(replay)
    return list(result["committed"])


# --- lead confirmation, booked to the same durability contract ----------------


def test_a_lead_pauses_for_the_customer_before_anything_is_committed() -> None:
    """The consent interrupt is the only thing between a model and a stored lead."""
    harness = build_harness(lead_script())

    async def scenario() -> None:
        result = await harness.runtime.send(LEAD_TENANT, "s-lead-pause", "please call me")

        assert result.is_paused
        assert result.pending is not None
        assert result.pending["awaiting"] == "lead_confirmation"
        assert result.pending["contact"] == "dana@example.com"
        assert await harness.leads.for_tenant(LEAD_TENANT) == ()

    asyncio.run(scenario())


def test_a_paused_lead_survives_a_restart_and_completes_once() -> None:
    """The whole checkpoint-resume contract, on the lead path.

    The resume runs on a runtime built after the pause, from graph, nodes, and
    service objects the paused run never touched — what a redeployed process
    would actually pick up.
    """
    before = build_harness(lead_script())

    async def scenario() -> None:
        paused = await before.runtime.send(LEAD_TENANT, "s-lead-restart", "please call me")
        assert paused.is_paused

        after = before.restarted()
        assert await after.runtime.pending(LEAD_TENANT, "s-lead-restart") is not None

        resumed = await after.runtime.resume(LEAD_TENANT, "s-lead-restart", "approved")

        assert not resumed.is_paused
        assert resumed.answer == "The team will call you back."
        assert [action["action"] for action in resumed.committed] == ["create_lead"]

        captured = await after.leads.for_tenant(LEAD_TENANT)
        assert len(captured) == 1
        assert captured[0].contact.value == "dana@example.com"

    asyncio.run(scenario())


def test_replaying_the_lead_commit_node_captures_nothing_new() -> None:
    """A forced replay captures nothing, because the key is derived, not fresh.

    Driving the node directly rather than the graph is deliberate: LangGraph
    will not resume a finished thread, so the only way to prove the *node* is
    replay-safe is to hand it the state it already committed from — exactly what
    a crash between the domain write and the checkpoint write leaves behind.
    """
    harness = build_harness(lead_script())

    async def scenario() -> None:
        await harness.runtime.send(LEAD_TENANT, "s-lead-replay", "please call me")
        first = await harness.runtime.resume(LEAD_TENANT, "s-lead-replay", "approved")

        state = await harness.checkpointed_state(LEAD_TENANT, "s-lead-replay")
        replay: DispatchState = state | {
            "pending_lead": _lead_call(state),
            "lead_approved": True,
        }
        result = await DispatchNodes(harness.dependencies).commit_lead(replay)

        captured = await harness.leads.for_tenant(LEAD_TENANT)
        assert len(captured) == 1
        committed = next(iter(result["committed"]))
        assert committed["reference"] == first.committed[0]["reference"]
        assert committed["replayed"] is True
        assert first.committed[0]["replayed"] is False

    asyncio.run(scenario())


def _lead_call(state: DispatchState) -> StoredToolCall:
    for entry in reversed(state["transcript"]):
        for call in entry["tool_calls"]:
            if call["name"] == "create_lead":
                return call
    raise AssertionError("no lead call in the checkpointed transcript")


def test_a_declined_lead_confirmation_captures_nothing_and_returns_to_the_model() -> None:
    """Declining a callback request is a normal turn, not an error."""
    harness = build_harness(
        [
            lead_script()[0],
            ModelResponse(content="Understood — anything else?", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        paused = await harness.runtime.send(LEAD_TENANT, "s-lead-decline", "please call me")
        assert paused.is_paused

        result = await harness.runtime.resume(LEAD_TENANT, "s-lead-decline", "declined")

        assert result.answer == "Understood — anything else?"
        assert result.committed == ()
        assert await harness.leads.for_tenant(LEAD_TENANT) == ()

    asyncio.run(scenario())


def _booking_call(state: DispatchState) -> StoredToolCall:
    for entry in reversed(state["transcript"]):
        for call in entry["tool_calls"]:
            if call["name"] == "book_appointment":
                return call
    raise AssertionError("no booking call in the checkpointed transcript")


def test_deleting_every_checkpoint_loses_no_business_record() -> None:
    """Checkpoints hold resume points; the domain stores hold the business.

    An emptied checkpoint store is modelled as a new one, which is what a
    truncated table is from the runtime's point of view. Afterwards the booking
    is still there and new conversations still start, so the store is safe to
    clear under an incident.
    """
    harness = build_harness(booking_script())

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-wipe", "book HVAC Monday")
        await harness.runtime.resume(BOOKING_TENANT, "s-wipe", "approved")
        booked_before = await harness.bookings.for_tenant(BOOKING_TENANT)
        assert len(booked_before) == 1

        wiped = build_harness(
            [ModelResponse(content="We are open until 7pm.", model_name="scripted")],
            bookings=harness.bookings,
            leads=harness.leads,
            handoffs=harness.handoffs,
            idempotency=harness.idempotency,
        )

        assert await wiped.bookings.for_tenant(BOOKING_TENANT) == booked_before
        assert await wiped.runtime.pending(BOOKING_TENANT, "s-wipe") is None

        started = await wiped.runtime.send(BOOKING_TENANT, "s-after-wipe", "what are your hours?")
        assert started.answer == "We are open until 7pm."

    asyncio.run(scenario())


def test_a_declined_confirmation_books_nothing_and_returns_to_the_model() -> None:
    """Declining is a normal turn, not an error and not a dead end."""
    harness = build_harness(
        [
            booking_script()[0],
            ModelResponse(content="No problem — shall I look at Wednesday?", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-decline", "book HVAC Monday")
        result = await harness.runtime.resume(BOOKING_TENANT, "s-decline", "declined")

        assert result.answer == "No problem — shall I look at Wednesday?"
        assert result.committed == ()
        assert await harness.bookings.for_tenant(BOOKING_TENANT) == ()

    asyncio.run(scenario())


def test_an_unrecognized_resume_value_declines() -> None:
    """Anything that is not an explicit approval must not book.

    A resume value crosses a trust boundary. The asymmetry is the argument: a
    wrongly declined booking costs one exchange, a wrongly approved one sends a
    crew to an address nobody confirmed.
    """
    harness = build_harness(
        [booking_script()[0], ModelResponse(content="Let me know.", model_name="scripted")]
    )

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-ambiguous", "book HVAC Monday")
        await harness.runtime.resume(BOOKING_TENANT, "s-ambiguous", {"unexpected": "shape"})

        assert await harness.bookings.for_tenant(BOOKING_TENANT) == ()

    asyncio.run(scenario())


def test_an_unofferable_slot_never_reaches_the_customer_as_a_question() -> None:
    """A slot nobody offered is refused before the confirmation is raised."""
    harness = build_harness(
        [
            booking_script(slot="Some Tuesday I invented")[0],
            ModelResponse(content="That time is not available.", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-bad-slot", "book HVAC")

        assert not result.is_paused
        assert result.answer == "That time is not available."
        assert await harness.bookings.for_tenant(BOOKING_TENANT) == ()

    asyncio.run(scenario())


def test_a_second_turn_on_one_thread_answers_the_second_question() -> None:
    """History accumulates; the answer does not.

    A turn that ends without fresh prose must not republish the previous turn's
    answer, and the transcript the model sees must still contain the earlier
    exchange.
    """
    harness = build_harness(
        [
            ModelResponse(content="We are open until 7pm.", model_name="scripted"),
            ModelResponse(content="Yes, we cover 97205.", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        first = await harness.runtime.send(BOOKING_TENANT, "s-two-turns", "what are your hours?")
        second = await harness.runtime.send(BOOKING_TENANT, "s-two-turns", "do you serve 97205?")
        state = await harness.checkpointed_state(BOOKING_TENANT, "s-two-turns")

        assert first.answer == "We are open until 7pm."
        assert second.answer == "Yes, we cover 97205."
        assert state["turn_index"] == 2
        assert [entry["content"] for entry in state["transcript"] if entry["role"] == "user"] == [
            "what are your hours?",
            "do you serve 97205?",
        ]

    asyncio.run(scenario())
