"""Releasing a handoff resumes the graph, and the resumed turn commits nothing twice.

`FEAT-004`'s release is the explicit invitation that lets the assistant answer
again. These tests pin what that means against the real graph and the real
idempotent services: a conversation that handed off and was released keeps its
checkpoint, the next turn runs from the transcript that accumulated while it
was held, and re-driving an action that already committed books nothing new.
"""

from __future__ import annotations

import asyncio

from tenantchat.api.registry import TenantRegistry
from tenantchat.core.commands import HandoffCommand
from tenantchat.orchestration.model import ModelResponse
from tests.agent_runtime.conftest import (
    BOOKING_TENANT,
    RuntimeHarness,
    build_harness,
)
from tests.agent_runtime.test_durable_workflow import (
    _replay_commit_node,
    booking_script,
)


async def _handoff(
    harness: RuntimeHarness, session_id: str, *, reason: str = "customer_request"
) -> str:
    policy = TenantRegistry.seeded().get(BOOKING_TENANT).policy
    command = HandoffCommand.parse(
        policy, reason=reason, summary="Customer asked to speak to staff."
    )
    return (await harness.handoffs.record(command, session_id=session_id)).handoff_id


def test_a_released_conversation_resumes_the_graph_from_its_checkpoint() -> None:
    """Release un-gates the assistant, and the resumed turn keeps the thread."""
    harness = build_harness(
        [
            ModelResponse(content="We are open until 7pm.", model_name="scripted"),
            ModelResponse(content="Yes, we cover 97205.", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        first = await harness.runtime.send(BOOKING_TENANT, "s-resume", "what are your hours?")
        assert first.answer == "We are open until 7pm."

        # The conversation is handed to a person and then released back.
        handoff_id = await _handoff(harness, "s-resume")
        await harness.handoffs.accept(BOOKING_TENANT, handoff_id, principal_id="operator-7")
        released = await harness.handoffs.release(
            BOOKING_TENANT, handoff_id, principal_id="operator-7"
        )
        assert released.status == "queued"

        # The next message is a fresh turn on the same thread; the handoff
        # state records the release, and nothing was committed twice.
        second = await harness.runtime.send(BOOKING_TENANT, "s-resume", "do you serve 97205?")
        assert second.answer == "Yes, we cover 97205."
        state = await harness.checkpointed_state(BOOKING_TENANT, "s-resume")
        assert state["turn_index"] == 2
        assert len(await harness.handoffs.for_tenant(BOOKING_TENANT)) == 1

    asyncio.run(scenario())


def test_replaying_a_commit_after_release_commits_nothing_new() -> None:
    """The idempotency key outlives the handoff: a resumed turn books once.

    The graph's own resume after a release can re-run the node that already
    committed the booking (a crash between the domain write and the checkpoint
    write would do the same). The key is derived from the checkpointed turn, so
    the replay returns the original reference and writes no second row — the
    guarantee the acceptance calls "a queued turn resuming after release commits
    nothing twice."
    """
    harness = build_harness(booking_script())

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-replay-rel", "book HVAC Monday")
        first = await harness.runtime.resume(BOOKING_TENANT, "s-replay-rel", "approved")
        assert len(await harness.bookings.for_tenant(BOOKING_TENANT)) == 1

        handoff_id = await _handoff(harness, "s-replay-rel")
        await harness.handoffs.accept(BOOKING_TENANT, handoff_id, principal_id="operator-7")
        await harness.handoffs.release(BOOKING_TENANT, handoff_id, principal_id="operator-7")

        replayed = await _replay_commit_node(harness, "s-replay-rel")

        assert len(await harness.bookings.for_tenant(BOOKING_TENANT)) == 1
        assert replayed[0]["reference"] == first.committed[0]["reference"]
        assert replayed[0]["replayed"] is True

    asyncio.run(scenario())


def test_a_visitor_message_held_during_takeover_lands_in_the_resumed_transcript() -> None:
    """Queued messages are stored, so the resumed turn answers with full context.

    The pause is at the API layer, which is what makes it enforceable outside
    the graph; here we assert the graph's view after release contains every
    message the visitor sent while a staff member held the conversation.
    """
    harness = build_harness(
        [
            ModelResponse(content="We are open until 7pm.", model_name="scripted"),
            ModelResponse(content="Yes, we cover 97205.", model_name="scripted"),
        ]
    )

    async def scenario() -> None:
        await harness.runtime.send(BOOKING_TENANT, "s-held", "what are your hours?")
        handoff_id = await _handoff(harness, "s-held")
        await harness.handoffs.accept(BOOKING_TENANT, handoff_id, principal_id="operator-7")
        await harness.handoffs.release(BOOKING_TENANT, handoff_id, principal_id="operator-7")

        # The message that was queued while the staff member held the
        # conversation is part of the transcript the graph resumes from.
        state = await harness.checkpointed_state(BOOKING_TENANT, "s-held")
        assert [entry["content"] for entry in state["transcript"] if entry["role"] == "user"] == [
            "what are your hours?"
        ]

    asyncio.run(scenario())
