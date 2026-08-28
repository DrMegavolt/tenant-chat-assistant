"""What survives a run that did not finish.

A crash used to be the end of the thread: the checkpointed transcript kept the
model's tool calls with no results, and every later turn on that thread was
rejected by the provider before the model was ever asked anything. A
cancellation used to be the end of the record: the turn vanished entirely.
These tests pin the recovery — the next turn heals the transcript, and a
cancelled run still leaves a turn record behind.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

from packages.orchestration.tests.dispatch_harness import (
    TENANT_ID,
    build_harness,
    demo_slot,
)
from tenantchat.orchestration.model import AssembledPrompt, ModelResponse, ToolCall


def _outcome(trace: Mapping[str, object] | None) -> dict[str, object]:
    assert trace is not None
    value = trace["outcome"]
    assert isinstance(value, Mapping)
    return dict(value)


def _executed(trace: Mapping[str, object] | None) -> dict[str, object]:
    assert trace is not None
    value = trace["executed_graph"]
    assert isinstance(value, Mapping)
    return dict(value)


def _nodes(section: Mapping[str, object]) -> list[dict[str, object]]:
    raw = section["nodes"]
    assert isinstance(raw, list)
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _tool_payloads(prompt: AssembledPrompt) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for message in prompt.messages:
        if message.role == "tool":
            parsed = json.loads(message.content)
            assert isinstance(parsed, dict)
            payloads.append(parsed)
    return payloads


def test_a_cancelled_turn_still_records_a_turn_record() -> None:
    """A cancelled run is recorded as cancelled, not left out of the history.

    The request task is cancelled the way an HTTP server cancels a
    disconnected visitor's: mid-flight, from outside. The visitor never
    receives an answer and nothing is committed, but the turn record exists
    with an honest outcome — before the fix, the cancellation propagated past
    every recording path and the turn was simply missing.
    """
    harness = build_harness(
        [ModelResponse(content="We are open until 7pm.", model_name="scripted")],
        hang=True,
    )

    async def scenario() -> None:
        request = asyncio.create_task(
            harness.runtime.send(TENANT_ID, "s-cancelled", "What are your hours?")
        )
        while harness.model.call_count == 0:
            await asyncio.sleep(0)
        request.cancel()
        result = await request

        assert result.answer == ""
        assert result.pending is None
        assert result.committed == ()
        assert _outcome(result.trace) == {"status": "cancelled", "rounds": 0, "failure": None}
        # No failure is attributed: a cancellation is not a system fault.
        assert result.trace is not None
        assert result.trace["diagnoses"] == []
        # The executed graph stops at the node that was running, honestly open.
        section = _executed(result.trace)
        assert [node["name"] for node in _nodes(section)] == ["route", "model"]
        assert _nodes(section)[0]["status"] == "ok"
        assert _nodes(section)[1]["status"] == "error"
        assert _nodes(section)[1]["ended_at"] is None

    asyncio.run(scenario())


def test_a_crash_after_tool_calls_leaves_the_next_turn_recoverable() -> None:
    """A crash between tool-call and tool-result must not poison the thread.

    The checkpoint holds the model's tool calls with no results, and every
    provider rejects a conversation in that shape — so before the fix, the
    next turn on the thread failed the same way forever. The next send closes
    the stranded calls with a server-written interrupted result, and the model
    is asked a question the provider will actually accept.
    """
    harness = build_harness(
        [
            ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        call_id="call-availability",
                        name="get_availability",
                        arguments={"service": "HVAC"},
                    ),
                ),
                model_name="scripted",
            ),
            ModelResponse(content="We are open until 7pm.", model_name="scripted"),
        ],
        strict_history=True,
    )
    harness.availability.error = RuntimeError("calendar exploded")

    async def scenario() -> None:
        failed = await harness.runtime.send(TENANT_ID, "s-crash-loop", "when can you come?")
        assert _outcome(failed.trace)["status"] == "failed"

        recovered = await harness.runtime.send(TENANT_ID, "s-crash-loop", "What are your hours?")
        assert recovered.answer == "We are open until 7pm."
        assert _outcome(recovered.trace)["status"] == "answered"

        # The second turn's prompt carried the stranded call, closed out by a
        # payload no model could mistake for a real availability result.
        payloads = _tool_payloads(harness.model.calls[-1])
        assert {
            "error": "turn_interrupted",
            "message": "The assistant was interrupted before this action could run.",
        } in payloads

    asyncio.run(scenario())


def test_a_paused_thread_is_not_healed_by_a_new_message() -> None:
    """A thread paused at a confirmation keeps its run intact.

    The stranded-call repair exists for runs that died; a paused run is still
    alive and will produce the real tool results when it resumes, so a new
    message must not write a synthetic result over a confirmation the customer
    has not answered yet.
    """
    harness = build_harness(
        [
            ModelResponse(
                content="",
                tool_calls=(
                    ToolCall(
                        call_id="call-booking",
                        name="book_appointment",
                        arguments={
                            "service": "HVAC",
                            "slot": demo_slot().label,
                            "customer_name": "Dana Ruiz",
                            "customer_phone_or_email": "555-222-1919",
                            "address": "12 Alder Court, Portland, OR 97205",
                        },
                    ),
                ),
                model_name="scripted",
            ),
        ]
    )
    harness.availability.slots = (demo_slot(),)

    async def scenario() -> None:
        paused = await harness.runtime.send(TENANT_ID, "s-paused", "book HVAC for tomorrow")
        assert paused.is_paused

        # A new message on a paused thread runs the normal machinery — the
        # abandoned confirmation is closed out by the escalation path's own
        # record, never by the crash repair.
        await harness.runtime.send(TENANT_ID, "s-paused", "what are your hours?")

        assert not any(
            payload.get("error") == "turn_interrupted"
            for prompt in harness.model.calls
            for payload in _tool_payloads(prompt)
        )
        state = await harness.runtime.snapshot(TENANT_ID, "s-paused")
        transcript = state["transcript"]
        assert isinstance(transcript, list)
        assert all(
            "turn_interrupted" not in str(entry.get("content", ""))
            for entry in transcript
            if isinstance(entry, Mapping)
        )

    asyncio.run(scenario())
