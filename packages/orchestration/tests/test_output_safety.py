"""What a turn records when the model misbehaves, and what the visitor gets.

Three model failures used to leave the record thinner than the turn deserved:
a provider exception, an empty response, and an output-policy block all
reached the model and recorded nothing about the call. A fourth never reached
the validators at all: the model writing its tool call into the *text* of the
answer sailed through as `answered`, markup and echoed contact details
included. These tests pin the behavior after the fix — every call that reached
the model is attributable, and a leaked call is refused, diagnosed, and never
recorded as an answer.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import pytest

from packages.orchestration.tests.dispatch_harness import (
    ESCALATION_REPLY,
    LEAK_REFUSAL_REPLY,
    TENANT_ID,
    build_harness,
    overlong_budget_policy,
)
from tenantchat.orchestration.model import ModelResponse


def _outcome(trace: Mapping[str, object] | None) -> dict[str, object]:
    assert trace is not None
    value = trace["outcome"]
    assert isinstance(value, Mapping)
    return dict(value)


def _verdicts(trace: Mapping[str, object] | None) -> dict[str, object]:
    assert trace is not None
    value = trace["verdicts"]
    assert isinstance(value, Mapping)
    return dict(value)


def _diagnoses(trace: Mapping[str, object] | None) -> list[dict[str, object]]:
    assert trace is not None
    raw = trace.get("diagnoses")
    assert isinstance(raw, list)
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _model_section(trace: Mapping[str, object] | None) -> dict[str, object]:
    assert trace is not None
    value = trace["model"]
    assert isinstance(value, Mapping)
    return dict(value)


def _invocations(trace: Mapping[str, object] | None) -> list[dict[str, object]]:
    assert trace is not None
    raw = trace.get("model_invocations")
    assert isinstance(raw, list)
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def test_leaked_tool_call_markup_is_refused_diagnosed_and_never_answered() -> None:
    """Provider chat-template markup in the text is an invalid model response.

    Live, the model emitted `<tool_call>` syntax as its answer: no tool ran,
    no validator flagged it, the turn was recorded `answered`, and the visitor
    read their own contact details inside the markup. The record now shows a
    refused turn with a detected diagnosis instead.
    """
    harness = build_harness(
        [ModelResponse(content="We are open until 7pm.", model_name="scripted")]
    )

    async def scenario() -> None:
        leaked = (
            "<tool_call>\n<function=create_lead>\n<parameter=name>Jane Tester</parameter>\n"
            "<parameter=customer_phone_or_email>555-222-1919</parameter>\n</tool_call>"
        )
        harness.model.script = [ModelResponse(content=leaked, model_name="scripted")]
        result = await harness.runtime.send(TENANT_ID, "s-leak-markup", "What are your hours?")

        assert result.answer == LEAK_REFUSAL_REPLY
        assert result.answer != leaked
        assert _outcome(result.trace) == {"status": "refused", "rounds": 1, "failure": None}
        assert _verdicts(result.trace)["output_invalid"] == [
            {"kind": "raw_tool_call", "value": "<tool_call>"}
        ]
        diagnoses = _diagnoses(result.trace)
        assert len(diagnoses) == 1
        assert diagnoses[0]["cause"] == "model_malformed_output"
        assert diagnoses[0]["stage"] == "model"
        assert diagnoses[0]["status"] == "detected"
        assert diagnoses[0]["confidence"] == "high"
        assert diagnoses[0]["evidence"] == ["output_invalid:raw_tool_call"]

    asyncio.run(scenario())


def test_a_source_style_tool_call_leak_is_refused_the_same_way() -> None:
    """The leak has a second costume: a graph tool written as source code.

    The live turns also showed `create_lead(name="Jane Tester", phone=...)`
    published as prose. The detection is the same refusal, and the verdict
    carries the offending line so the record explains what was withheld.
    """
    harness = build_harness(
        [ModelResponse(content="We are open until 7pm.", model_name="scripted")]
    )

    async def scenario() -> None:
        leaked = 'create_lead(name="Jane Tester", phone="555-222-1919")'
        harness.model.script = [ModelResponse(content=leaked, model_name="scripted")]
        result = await harness.runtime.send(TENANT_ID, "s-leak-call", "What are your hours?")

        assert result.answer == LEAK_REFUSAL_REPLY
        assert _outcome(result.trace)["status"] == "refused"
        assert _verdicts(result.trace)["output_invalid"] == [
            {"kind": "raw_tool_call", "value": leaked}
        ]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "answer",
    [
        "We are open until 7pm.",
        # Reviewer false positives: generic markup talk and a spaced mention
        # of a tool name are prose, and refusing them costs the visitor their
        # answer for nothing.
        "Wrap the handler in a <function> element.",
        "The <parameter> element is optional, but useful.",
        "The agent may call check_service_area (ZIP 97205) first.",
    ],
    ids=["plain", "markup-word-function", "markup-word-parameter", "spaced-tool-mention"],
)
def test_an_answer_without_leaked_call_syntax_still_publishes(answer: str) -> None:
    """The leak detector is conservative: legitimate prose is never refused.

    Only chat-template tags with their `=` attributes and an adjacent-paren
    call of a graph tool count. A bare "<function>" in a sentence about
    markup, or a tool name followed by a spaced parenthetical, must publish —
    a detector that fires on those turns every honest answer into a refusal.
    """
    harness = build_harness([ModelResponse(content=answer, model_name="scripted")])

    async def scenario() -> None:
        result = await harness.runtime.send(TENANT_ID, "s-clean", "What are your hours?")

        assert result.answer == answer
        assert _outcome(result.trace)["status"] == "answered"
        assert _verdicts(result.trace)["output_invalid"] == []
        assert _diagnoses(result.trace) == []

    asyncio.run(scenario())


def test_a_provider_failure_records_the_model_call_it_attempted() -> None:
    """A turn that reached the model is attributable even when the call raised.

    The escalation that followed used to carry no prompt, no invocation, and no
    usage — a turn with zero model attribution despite one spent round. The
    record now shows the attempted call; the response fields stay empty
    because no response ever arrived.
    """
    harness = build_harness(
        [ModelResponse(content="We are open until 7pm.", model_name="scripted")],
        failure=TimeoutError("provider did not respond"),
    )

    async def scenario() -> None:
        result = await harness.runtime.send(TENANT_ID, "s-provider-down", "What are your hours?")

        assert result.answer == ESCALATION_REPLY
        assert _outcome(result.trace) == {
            "status": "escalated",
            "rounds": 1,
            "failure": "tool_failure",
        }
        assert result.trace is not None
        assert isinstance(result.trace["prompt"], Mapping)
        model_section = _model_section(result.trace)
        assert isinstance(model_section, Mapping)
        assert dict(model_section) == {
            "name": "",
            "usage": {},
            "cache_hit": False,
            "fallback_hops": [],
        }
        invocations = _invocations(result.trace)
        assert len(invocations) == 1
        assert invocations[0]["round"] == 1
        assert invocations[0]["model_name"] == ""
        assert invocations[0]["usage"] == {}
        assert invocations[0]["produced_content"] is False
        assert invocations[0]["cache_hit"] is False
        assert invocations[0]["fallback_hops"] == []
        assert isinstance(invocations[0]["prompt_assembly"], Mapping)

    asyncio.run(scenario())


def test_an_empty_model_response_records_the_call_and_its_usage() -> None:
    """An empty response still spent a round, and the record says which one.

    The escalation path used to drop the model name and the usage the ledger
    had already counted, so the turn read as if the model was never called.
    """
    harness = build_harness(
        [
            ModelResponse(
                content="",
                model_name="scripted",
                usage={"prompt_tokens": 120, "completion_tokens": 0},
            )
        ]
    )

    async def scenario() -> None:
        result = await harness.runtime.send(TENANT_ID, "s-empty", "What are your hours?")

        assert result.answer == ESCALATION_REPLY
        assert _outcome(result.trace) == {
            "status": "escalated",
            "rounds": 1,
            "failure": "unresolved",
        }
        model_section = _model_section(result.trace)
        assert isinstance(model_section, Mapping)
        assert dict(model_section) == {
            "name": "scripted",
            "usage": {"prompt_tokens": 120, "completion_tokens": 0},
            "cache_hit": False,
            "fallback_hops": [],
        }
        invocations = _invocations(result.trace)
        assert len(invocations) == 1
        assert invocations[0]["model_name"] == "scripted"
        assert invocations[0]["usage"] == {"prompt_tokens": 120, "completion_tokens": 0}
        assert invocations[0]["produced_content"] is False
        assert isinstance(invocations[0]["prompt_assembly"], Mapping)

    asyncio.run(scenario())


def test_an_output_policy_block_records_the_model_call_it_refused() -> None:
    """A blocked output is a refusal of a real call, not a missing one.

    The visitor gets the server-written reply either way, but the record kept
    neither the prompt nor the call that produced the blocked prose — the one
    `OBS-004` attribution a blocked turn most needs.
    """
    harness = build_harness(
        [ModelResponse(content="We are open until 7pm.", model_name="scripted")],
        policy=overlong_budget_policy(),
    )

    async def scenario() -> None:
        result = await harness.runtime.send(TENANT_ID, "s-blocked", "What are your hours?")

        assert result.answer == (
            "I could not finish that answer. Please ask again, or call (555) 816-4420."
        )
        assert _outcome(result.trace) == {"status": "answered", "rounds": 1, "failure": None}
        model_section = _model_section(result.trace)
        assert isinstance(model_section, Mapping)
        assert dict(model_section)["name"] == "scripted"
        invocations = _invocations(result.trace)
        assert len(invocations) == 1
        assert invocations[0]["model_name"] == "scripted"
        assert invocations[0]["produced_content"] is True
        assert isinstance(invocations[0]["prompt_assembly"], Mapping)

    asyncio.run(scenario())


def test_a_direct_handoff_ticket_does_not_describe_a_model_failure() -> None:
    """A customer-requested handoff is a designed route, not a failed turn.

    The ticket text used to read "could not complete turn 1 ... after 0 model
    calls" for a handoff the router made before any model call — accurate for
    a failure, misleading for the one path that runs by design.
    """
    harness = build_harness(
        [ModelResponse(content="We are open until 7pm.", model_name="scripted")]
    )

    async def scenario() -> None:
        await harness.runtime.send(TENANT_ID, "s-direct-handoff", "let me talk to a person")

        assert len(harness.handoffs.commands) == 1
        summary = harness.handoffs.commands[0].summary
        assert summary == (
            "Turn 1 was routed straight to a person (customer_request); " "no model call was made."
        )

    asyncio.run(scenario())


def test_a_failed_turn_ticket_still_reports_the_spent_rounds() -> None:
    """The failure wording is unchanged where it was accurate."""
    harness = build_harness(
        [ModelResponse(content="We are open until 7pm.", model_name="scripted")],
        failure=TimeoutError("provider did not respond"),
    )

    async def scenario() -> None:
        await harness.runtime.send(TENANT_ID, "s-failed-handoff", "What are your hours?")

        assert len(harness.handoffs.commands) == 1
        assert harness.handoffs.commands[0].summary == (
            "Assistant could not complete turn 1 (tool_failure) after 1 model calls."
        )

    asyncio.run(scenario())
