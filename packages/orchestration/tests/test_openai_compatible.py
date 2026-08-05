"""The AI-001 OpenAI-compatible provider adapter, exercised over a fake transport.

These tests pin the wire contract an adapter must speak: the request shape sent
to an OpenAI-compatible ``/chat/completions`` endpoint and the translation of
its response back into the domain's :class:`AssembledPrompt`/:class:`ToolCall`
types. Because the transport is an ``httpx`` mock, the tests run with no network,
no key, and no provider — the same way the rest of the runtime is tested.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    MessageRole,
    ModelResponse,
    PromptRegion,
    PromptSegment,
    ToolCall,
    ToolSpec,
)
from tenantchat.orchestration.providers.openai_compatible import (
    OpenAICompatibleChatModel,
)

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
    """One assembled message whose single segment carries ``content``.

    Visitor turns are marked untrusted, matching how assembly marks them; the
    adapter must translate the content and not care about the marking.
    """
    region = PromptRegion.UNTRUSTED if role is MessageRole.USER else PromptRegion.TRUSTED
    return AssembledMessage(
        role=role,
        segments=(PromptSegment("segment", region, content),),
        tool_call_id=tool_call_id,
    )


def prompt(*messages: AssembledMessage) -> AssembledPrompt:
    return AssembledPrompt(
        template_id="wire-test",
        template_version=1,
        bindings={},
        messages=tuple(messages),
    )


def _run(
    handler: httpx.MockTransport,
    messages: list[AssembledMessage],
    tools: tuple[ToolSpec, ...],
) -> ModelResponse:
    """Drive one `complete` against a fake transport and return the response."""

    async def invoke() -> ModelResponse:
        adapter = OpenAICompatibleChatModel(
            base_url="http://provider/v1", model="local-model", transport=handler
        )
        return await adapter.complete(prompt(*messages), tools=tools)

    return asyncio.run(invoke())


def test_sends_chat_completions_request_shape() -> None:
    """The adapter speaks OpenAI's wire format for a plain chat turn."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "local-model"
        assert body["messages"] == [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "hello"},
        ]
        assert request.url.path == "/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer secret"
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    async def invoke() -> ModelResponse:
        adapter = OpenAICompatibleChatModel(
            base_url="http://provider/v1",
            model="local-model",
            api_key="secret",
            transport=httpx.MockTransport(handler),
        )
        return await adapter.complete(
            prompt(
                message(MessageRole.SYSTEM, "You are helpful."),
                message(MessageRole.USER, "hello"),
            ),
            tools=(),
        )

    with_adapter = asyncio.run(invoke())
    assert with_adapter.content == "hi"
    assert with_adapter.tool_calls == ()


def test_the_api_key_reaches_the_authorization_header_and_nowhere_else() -> None:
    """The secret travels only where the provider expects it.

    The payload, the adapter's own representation, and the exceptions it raises
    must never echo the key: a client-visible error or a log line built from
    any of them would publish a credential.
    """
    secret = "super-secret-key"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == f"Bearer {secret}"
        assert secret not in json.dumps(json.loads(request.content))
        return httpx.Response(401, json={"error": secret})

    adapter = OpenAICompatibleChatModel(
        base_url="http://provider/v1",
        model="local-model",
        api_key=secret,
        transport=httpx.MockTransport(handler),
    )

    assert secret not in repr(adapter)
    assert secret not in str(adapter)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        asyncio.run(adapter.complete(prompt(message(MessageRole.USER, "hi")), tools=()))
    assert secret not in str(raised.value)


def test_omits_auth_header_when_no_key_is_configured() -> None:
    """A local, unauthenticated endpoint (e.g. llama.cpp) keeps working keyless."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "Authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    response = _run(
        httpx.MockTransport(handler),
        [message(MessageRole.USER, "hi")],
        (),
    )
    assert response.content == "ok"


def test_translates_tool_specs_and_parses_tool_calls() -> None:
    """Tools are serialized to the provider and call arguments parsed back."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tools"][0]["function"]["name"] == "check_service_area"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {
                                        "name": "check_service_area",
                                        "arguments": '{"zip": "97205"}',
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    response = _run(
        httpx.MockTransport(handler),
        [message(MessageRole.USER, "what zip?")],
        (TOOL,),
    )
    assert response.content == ""
    assert response.tool_calls == (
        ToolCall(call_id="call-1", name="check_service_area", arguments={"zip": "97205"}),
    )


def test_sends_tool_results_with_correlation_id() -> None:
    """A TOOL message carries the call it answers, not a serialized tool call."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["messages"][1] == {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": "result",
        }
        return httpx.Response(200, json={"choices": [{"message": {"content": "done"}}]})

    response = _run(
        httpx.MockTransport(handler),
        [
            message(MessageRole.USER, "run it"),
            message(MessageRole.TOOL, "result", tool_call_id="call-1"),
        ],
        (),
    )
    assert response.content == "done"


def test_records_model_name_and_usage() -> None:
    """Attribution data the provider returns is kept on the turn for OBS-004."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "model": "provider-model",
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            },
        )

    response = _run(
        httpx.MockTransport(handler),
        [message(MessageRole.USER, "hi")],
        (),
    )
    assert response.model_name == "provider-model"
    assert response.usage == {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}


def test_treats_a_malformed_response_as_a_failed_turn() -> None:
    """A provider that returns no choices must not produce an empty answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    with pytest.raises(ValueError):
        _run(httpx.MockTransport(handler), [message(MessageRole.USER, "hi")], ())


def test_raises_on_transport_failure() -> None:
    """A provider 5xx surfaces as an exception the graph turns into a handoff."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        _run(httpx.MockTransport(handler), [message(MessageRole.USER, "hi")], ())
