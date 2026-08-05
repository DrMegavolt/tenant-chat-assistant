"""The AI-001 agent contract, run identically against every provider adapter.

The acceptance criterion is that the *same* scenarios pass against every
configured adapter or fake, so this module owns the scenarios and a driver is
only the knowledge of how to serve one of them. The two drivers today are the
OpenAI-compatible adapter over a fake HTTP transport and the scripted double
the runtime tests use; a third provider joins by adding a driver, not by
editing a scenario.

The scenarios assert behavior, not wire shape: parsing arguments out of a
provider's JSON, correlation IDs on tool results, usage attribution, and what
a failure or an empty response must look like at the port boundary. Wire-shape
assertions for the OpenAI adapter live in ``test_openai_compatible.py``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Protocol

import httpx
import pytest

from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    ChatModel,
    MessageRole,
    ModelResponse,
    PromptRegion,
    PromptSegment,
    ToolCall,
    ToolSpec,
)
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel

TOOL = ToolSpec(
    name="check_service_area",
    description="Check a ZIP code.",
    parameters={
        "type": "object",
        "properties": {"zip": {"type": "string"}},
        "required": ["zip"],
        "additionalProperties": False,
    },
)


def message(
    role: MessageRole, content: str, *, tool_call_id: str | None = None
) -> AssembledMessage:
    """One assembled message whose single segment carries ``content``."""
    region = PromptRegion.UNTRUSTED if role is MessageRole.USER else PromptRegion.TRUSTED
    return AssembledMessage(
        role=role,
        segments=(PromptSegment("segment", region, content),),
        tool_call_id=tool_call_id,
    )


def prompt(*messages: AssembledMessage) -> AssembledPrompt:
    return AssembledPrompt(
        template_id="contract",
        template_version=1,
        bindings={},
        messages=tuple(messages),
    )


USER_TURN = prompt(message(MessageRole.USER, "what zip?"))


class AdapterDriver(Protocol):
    """How one adapter serves a script of :class:`ModelResponse` items."""

    name: str
    # The exception class a provider failure surfaces as. Part of the contract:
    # the graph treats any exception as a failed turn, and each adapter keeps
    # that promise with the failure type its provider produces.
    failure_type: type[Exception]

    def build(
        self,
        script: Sequence[ModelResponse],
        *,
        failure: Exception | None = None,
    ) -> tuple[ChatModel, Callable[[], int]]:
        """An adapter that replays ``script``, repeating the last item, plus a
        counter of how many calls the conversation cost.
        """


def _wire_body(response: ModelResponse) -> dict[str, object]:
    """Serialize a scripted response into the OpenAI chat-completions wire shape.

    The inverse of the adapter's own parsing, which is the point: the contract
    suite's expectations are the domain types, and each driver translates.
    """
    message: dict[str, object] = {"content": response.content}
    if response.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": json.dumps(dict(call.arguments)),
                },
            }
            for call in response.tool_calls
        ]
    body: dict[str, object] = {"choices": [{"message": message}]}
    if response.model_name != "unknown":
        body["model"] = response.model_name
    if response.usage:
        body["usage"] = dict(response.usage)
    return body


class OpenAIWireDriver:
    """Serves the script over the wire contract a real endpoint would."""

    name = "openai-compatible (fake transport)"
    failure_type: type[Exception] = httpx.HTTPStatusError

    def build(
        self,
        script: Sequence[ModelResponse],
        *,
        failure: Exception | None = None,
    ) -> tuple[ChatModel, Callable[[], int]]:
        bodies = [_wire_body(response) for response in script]
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            if failure is not None:
                return httpx.Response(503)
            index = min(calls, len(bodies) - 1)
            calls += 1
            return httpx.Response(200, json=bodies[index])

        adapter = OpenAICompatibleChatModel(
            base_url="http://provider/v1",
            model="deployment-model",
            transport=httpx.MockTransport(handler),
        )
        return adapter, lambda: calls


class ScriptedDoubleDriver:
    """Serves the script directly, the way the runtime tests script a model."""

    name = "scripted double"
    failure_type: type[Exception] = RuntimeError

    def build(
        self,
        script: Sequence[ModelResponse],
        *,
        failure: Exception | None = None,
    ) -> tuple[ChatModel, Callable[[], int]]:
        model = _ScriptedDouble(tuple(script), failure=failure)
        return model, lambda: model.call_count


class _ScriptedDouble:
    """Replays a fixed list of responses, then repeats the last one."""

    def __init__(
        self,
        script: tuple[ModelResponse, ...],
        *,
        failure: Exception | None = None,
    ) -> None:
        self._script = script
        self._failure = failure
        self._calls = 0

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        del prompt, tools
        if self._failure is not None:
            raise self._failure
        index = min(self._calls, len(self._script) - 1)
        self._calls += 1
        return self._script[index]

    @property
    def call_count(self) -> int:
        return self._calls


def _complete(
    model: ChatModel,
    prompt: AssembledPrompt = USER_TURN,
    *,
    tools: Sequence[ToolSpec] = (),
) -> ModelResponse:
    return asyncio.run(model.complete(prompt, tools=tools))


DRIVERS = [OpenAIWireDriver(), ScriptedDoubleDriver()]


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda driver: driver.name)
def test_a_plain_turn_returns_content_and_no_tool_calls(driver: AdapterDriver) -> None:
    """The common case: prose comes back with nothing to run."""
    model, _ = driver.build([ModelResponse(content="We are open until 7pm.")])

    response = _complete(model)

    assert response.content == "We are open until 7pm."
    assert response.tool_calls == ()


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda driver: driver.name)
def test_tool_call_arguments_arrive_parsed(driver: AdapterDriver) -> None:
    """A tool call crosses the adapter boundary with arguments as a mapping.

    Providers disagree about whether they emit an object or a JSON string; the
    port promises the graph never has to know, and every adapter must keep
    that promise.
    """
    expected = ToolCall(call_id="call-1", name="check_service_area", arguments={"zip": "97205"})
    model, _ = driver.build([ModelResponse(content="", tool_calls=(expected,))])

    response = _complete(model, tools=(TOOL,))

    assert response.tool_calls == (expected,)


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda driver: driver.name)
def test_tool_results_flow_back_with_their_call_id(driver: AdapterDriver) -> None:
    """A TOOL message names the call it answers, so the transcript stays valid."""
    model, _ = driver.build([ModelResponse(content="done")])

    response = _complete(
        model,
        prompt(
            message(MessageRole.USER, "run it"),
            message(MessageRole.TOOL, "result", tool_call_id="call-1"),
        ),
    )

    assert response.content == "done"


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda driver: driver.name)
def test_usage_and_model_name_are_attributed_to_the_turn(driver: AdapterDriver) -> None:
    """Attribution data survives the adapter so the turn can be accounted.

    ``OBS-002`` turns usage into metrics and ``OBS-004`` pins an answer to its
    model; neither works if an adapter drops the provider's accounting.
    """
    model, _ = driver.build(
        [
            ModelResponse(
                content="ok",
                model_name="provider-model",
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            )
        ]
    )

    response = _complete(model)

    assert response.model_name == "provider-model"
    assert response.usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda driver: driver.name)
def test_a_provider_failure_raises_a_failed_turn(driver: AdapterDriver) -> None:
    """Any provider failure surfaces as an exception, not as an answer.

    The graph converts the exception into a handoff rather than retrying
    (`REL-001` owns retry and backoff), and it can only do that if the adapter
    never substitutes text for a failure.
    """
    model, _ = driver.build(
        [ModelResponse(content="irrelevant")], failure=RuntimeError("provider refused")
    )

    with pytest.raises(driver.failure_type):
        _complete(model)


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda driver: driver.name)
def test_an_empty_response_is_returned_for_the_graph_to_judge(driver: AdapterDriver) -> None:
    """A response with neither content nor a tool call is not an answer.

    The port returns it unchanged and the graph escalates: an adapter that
    substituted its own text would make "the model returned nothing" look like
    a deliberate reply.
    """
    model, _ = driver.build([ModelResponse(content="")])

    response = _complete(model)

    assert response.content == ""
    assert response.tool_calls == ()


@pytest.mark.parametrize("driver", DRIVERS, ids=lambda driver: driver.name)
def test_calls_are_made_until_the_script_is_spent(driver: AdapterDriver) -> None:
    """A multi-call conversation is served from the script, call for call."""
    tool_call = ToolCall(call_id="c1", name="check_service_area", arguments={"zip": "97205"})
    model, count = driver.build(
        [
            ModelResponse(content="", tool_calls=(tool_call,)),
            ModelResponse(content="served"),
        ]
    )

    _complete(model, tools=(TOOL,))
    answered = _complete(model)

    assert count() == 2
    assert answered.content == "served"
