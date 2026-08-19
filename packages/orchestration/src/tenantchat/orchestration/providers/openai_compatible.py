"""The OpenAI-compatible provider adapter (`AI-001`).

One concrete :class:`~tenantchat.orchestration.model.ChatModel` for the
dispatcher assistant, speaking OpenAI's ``/chat/completions`` HTTP contract over
``httpx``. This is the adapter all of `AI-001`'s acceptance criteria exercise:
provider-neutral in the other direction (the graph sees only the port), and
portable because the request/response translation lives in one place.

The resilience envelope is `REL-001`: the adapter applies the connect/read/
write/pool/total timeouts of its :class:`ResiliencePolicy` to every request,
retries outage-shaped failures (timeouts, resets, ``429``, ``5xx``) with
exponential backoff and jitter, and trips a circuit breaker so a dead provider
fails fast instead of holding a worker through every timeout. Chat completions
are idempotent in effect — the call commits no business action, which is why a
retry after a timeout cannot duplicate one — and a model call is the only
operation this adapter retries. The graph still turns the *final* failure into
a handoff, exactly as ``call_model`` documents.

Deliberate non-goals:

- Response streaming belongs to `FEAT-010`. Usage accounting records the
  counters returned by the provider but does not attempt provider-specific
  billing.

Why httpx rather than the ``openai`` SDK: the SDK is a heavy, fast-moving
dependency and drags in a transport layer the project does not otherwise need.
The request this adapter makes is small enough to speak directly, and doing so
keeps the provider surface fully visible and testable against a fake transport.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import httpx

from tenantchat.core.metrics import MetricsReporter
from tenantchat.core.resilience import (
    AsyncResilientCaller,
    Dependency,
    FailureKind,
    ResiliencePolicy,
)
from tenantchat.orchestration.model import (
    AssembledPrompt,
    MessageRole,
    ModelResponse,
    ToolCall,
    ToolSpec,
)

# The connection pool one adapter instance reuses across every completion. The
# pool is the point: a per-request client (the prototype's shape) never reuses a
# connection, so TLS setup and TCP handshakes happen on every call.
_HTTP_LIMITS = httpx.Limits(max_connections=20, max_keepalive_connections=10)

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


def _classify_llm_error(exc: Exception) -> FailureKind:
    """Map a provider exception to the bounded retry decision (REL-001).

    Timeouts, connection failures, resets, ``429``, and ``5xx`` are
    outage-shaped and retryable. A non-``429`` ``4xx`` and a malformed response
    are contract or policy failures that a retry cannot fix, so they are never
    retried and never trip the breaker.
    """
    if isinstance(exc, httpx.TimeoutException):
        return FailureKind.TIMEOUT
    if isinstance(exc, httpx.ConnectError):
        return FailureKind.CONNECT
    if isinstance(exc, httpx.TransportError):
        return FailureKind.RESET
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 429:
            return FailureKind.RATE_LIMITED
        if 500 <= exc.response.status_code < 600:
            return FailureKind.SERVER_ERROR
        return FailureKind.REFUSED
    if isinstance(exc, ValueError):
        return FailureKind.MALFORMED
    return FailureKind.REFUSED


class OpenAICompatibleChatModel:
    """A ``ChatModel`` speaking OpenAI's chat-completions contract.

    Args:
        base_url: An absolute ``http(s)://`` OpenAI-compatible endpoint. The
            adapter appends ``/chat/completions``.
        model: The model ID sent in each request.
        api_key: Optional bearer token. Sent only when non-empty so a local,
            unauthenticated endpoint (e.g. llama.cpp) keeps working.
        policy: The `REL-001` resilience envelope. Defaults to the bounded
            policy in :class:`ResiliencePolicy`; the composition root tunes it
            from settings.
        metrics: Optional reporter for retry counts and circuit state. Failure
            and latency per completion are recorded by the outer
            ``MetricRecordingChatModel``, not here.
        transport: Optional httpx transport, which is how the contract suite
            exercises the adapter against a fake endpoint with no network.
    """

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str = "",
        policy: ResiliencePolicy | None = None,
        metrics: MetricsReporter | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._api_key = api_key
        resolved = policy or ResiliencePolicy()
        # httpx 0.28 has connect/read/write/pool phases and no total; the total
        # deadline is enforced by the resilient caller across the logical call.
        self._timeout = httpx.Timeout(
            connect=resolved.connect_timeout_seconds,
            read=resolved.read_timeout_seconds,
            write=resolved.write_timeout_seconds,
            pool=resolved.pool_timeout_seconds,
        )
        self._client = httpx.AsyncClient(
            timeout=self._timeout,
            transport=transport,
            limits=_HTTP_LIMITS,
        )
        self._resilience = AsyncResilientCaller(
            dependency=Dependency.LLM,
            policy=resolved,
            classify=_classify_llm_error,
            metrics=metrics,
        )

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
        Outage-shaped failures are retried inside the client (``REL-001``);
        the final failure raises as before.

        Raises:
            httpx.HTTPError: any transport or status failure that exhausted
                the retry budget. The graph converts an unhandled model failure
                into a handoff.
        """
        return await self._resilience.run(
            lambda: self._attempt(prompt, tools),
        )

    async def _attempt(
        self,
        prompt: AssembledPrompt,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
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
