"""The OpenAI-compatible provider adapter (`AI-001`).

One concrete :class:`~tenantchat.orchestration.model.ChatModel` for the
dispatcher assistant, speaking OpenAI's ``/chat/completions`` HTTP contract over
``httpx``. This is the adapter all of `AI-001`'s acceptance criteria exercise:
provider-neutral in the other direction (the graph sees only the port), and
portable because the request/response translation lives in one place.

Deliberate non-goals, owned by later tasks:

- Retries, timeouts with jitter, circuit breaking, and pooling are `REL-001`.
  This adapter fails fast and lets the graph turn one model failure into a
  handoff, exactly as ``call_model`` documents.
- Streaming and usage accounting beyond the naive token counts a provider may
  include are `AI-001`/`OBS-002` scope; this adapter captures whatever the
  response carries and nothing more.

Why httpx rather than the ``openai`` SDK: the SDK is a heavy, fast-moving
dependency and drags in a transport layer the project does not otherwise need.
The request this adapter makes is small enough to speak directly, and doing so
keeps the provider surface fully visible and testable against a fake transport.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import httpx

from tenantchat.orchestration.model import (
    AssembledPrompt,
    MessageRole,
    ModelResponse,
    ToolCall,
    ToolSpec,
)

ROLE_TO_API: Mapping[MessageRole, str] = {
    MessageRole.SYSTEM: "system",
    MessageRole.USER: "user",
    MessageRole.ASSISTANT: "assistant",
    MessageRole.TOOL: "tool",
}

# The API takes tool results as a "tool" message naming the call it answered;
# assoc_id is the OpenAI wire name for that correlation.
TOOL_CALL_ID_FIELD = "tool_call_id"


def _api_tool(spec: ToolSpec) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": dict(spec.parameters),
        },
    }


def _api_messages(prompt: AssembledPrompt) -> list[dict[str, object]]:
    wire: list[dict[str, object]] = []
    for message in prompt.messages:
        entry: dict[str, object] = {"role": ROLE_TO_API[message.role], "content": message.content}
        if message.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in message.tool_calls
            ]
        elif message.tool_call_id is not None:
            entry[TOOL_CALL_ID_FIELD] = message.tool_call_id
        wire.append(entry)
    return wire


def _parse_calls(tool_calls: list[object] | None) -> tuple[ToolCall, ...]:
    """Translate the provider's tool-call objects into the domain's ToolCall.

    Provider arguments arrive serialized as a JSON string; the port contract says
    call arguments must already be parsed, so each one is decoded here. A
    malformed payload raises, which the graph treats as a failed provider turn.
    """
    parsed: list[ToolCall] = []
    for raw in tool_calls or []:
        if not isinstance(raw, Mapping):
            continue
        call_id = str(raw.get("id") or "")
        function = raw.get("function")
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        if isinstance(arguments, str) and arguments.strip():
            data = json.loads(arguments)
        elif isinstance(arguments, Mapping):
            data = dict(arguments)
        else:
            data = {}
        parsed.append(ToolCall(call_id=call_id, name=name, arguments=data))
    return tuple(parsed)


class OpenAICompatibleChatModel:
    """A ``ChatModel`` speaking OpenAI's chat-completions contract.

    Args:
        base_url: An absolute ``http(s)://`` OpenAI-compatible endpoint. The
            adapter appends ``/chat/completions``.
        model: The model ID sent in each request.
        api_key: Optional bearer token. Sent only when non-empty so a local,
            unauthenticated endpoint (e.g. llama.cpp) keeps working.
        timeout_seconds: Per-request timeout; a provider that exceeds it fails
            the call, which the graph turns into a handoff.
        transport: Optional httpx transport, which is how the contract suite
            exercises the adapter against a fake endpoint with no network.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        timeout_seconds: int = 120,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_seconds)
        self._client = httpx.AsyncClient(timeout=self._timeout, transport=transport)

    @property
    def model(self) -> str:
        return self._model

    async def close(self) -> None:
        """Release the underlying connection pool."""
        await self._client.aclose()

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        """Complete the conversation from one assembled, versioned prompt.

        The prompt is the only input the adapter accepts (`AI-003`); the wire
        messages are derived from its segments, never from caller strings.

        Raises:
            httpx.HTTPError: any transport or status failure. The graph converts
                an unhandled model failure into a handoff.
        """
        payload: dict[str, object] = {
            "model": self._model,
            "messages": _api_messages(prompt),
        }
        if tools:
            payload["tools"] = [_api_tool(spec) for spec in tools]

        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        response = await self._client.post(
            f"{self._base_url}/chat/completions",
            json=payload,
            headers=headers,
        )
        response.raise_for_status()
        body = response.json()

        choices = body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ValueError("chat-completions response carried no choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, Mapping) else None
        if not isinstance(message, Mapping):
            raise ValueError("chat-completions choice carried no message")

        content = message.get("content")
        return ModelResponse(
            content=content if isinstance(content, str) else (content or ""),
            tool_calls=_parse_calls(message.get("tool_calls")),
            model_name=str(body.get("model") or self._model),
            usage=_parse_usage(body.get("usage")),
        )


def _parse_usage(usage: object) -> Mapping[str, int]:
    """Read whatever token accounting the provider returned, as bounded ints.

    ``OBS-002`` will turn this into metrics; until then it is recorded on the
    turn for attribution without ever leaving the inference plane.
    """
    if not isinstance(usage, Mapping):
        return {}
    parsed: dict[str, int] = {}
    for key, value in usage.items():
        try:
            parsed[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return parsed
