"""Content-free OTel GenAI spans for the chat path (L8-OTEL, ADR-0010).

Only the allowlisted attributes from the collector's redaction processor survive
export. This module sets nothing else — no prompt, no completion, no evidence, no
message content. The collector drops anything not listed regardless, but emitting
it at all would mean a grep of our own code finds content-bearing attribute names,
which is a reviewable event.

The ``EMITTED_ATTRIBUTES`` tuple is enumerated so a test can assert completeness
against the collector manifest and prove no content attribute ever names a key
this module sets. See ``tests/security/test_trace_plane.py``.
"""

from __future__ import annotations

import contextvars
from collections.abc import Sequence
from typing import Final

from opentelemetry.trace import SpanKind, StatusCode, Tracer, get_tracer

from tenantchat.orchestration.model import (
    AssembledPrompt,
    ChatModel,
    ModelResponse,
    ToolSpec,
)

INSTRUMENTATION_NAME: Final = "tenantchat.orchestration"
INSTRUMENTATION_VERSION: Final = "1.0.0"

_TENANT_ID = "tenant.id"
_SESSION_ID = "session.id"

_GEN_AI_SYSTEM = "gen_ai.system"
_GEN_AI_OPERATION = "gen_ai.operation.name"
_GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
_GEN_AI_RESPONSE_MODEL = "gen_ai.response.model"
_GEN_AI_RESPONSE_FINISH_REASON = "gen_ai.response.finish_reason"
_GEN_AI_USAGE_INPUT = "gen_ai.usage.input_tokens"
_GEN_AI_USAGE_OUTPUT = "gen_ai.usage.output_tokens"
_GEN_AI_USAGE_TOTAL = "gen_ai.usage.total_tokens"
_GEN_AI_ERROR_TYPE = "gen_ai.error.type"

# Every attribute this module ever sets on a span. A test asserts that none of
# these match content-bearing keys like ``gen_ai.prompt`` and that every one is
# on the collector's allowlist.
EMITTED_ATTRIBUTES: Final[tuple[str, ...]] = (
    _GEN_AI_SYSTEM,
    _GEN_AI_OPERATION,
    _GEN_AI_REQUEST_MODEL,
    _GEN_AI_RESPONSE_MODEL,
    _GEN_AI_RESPONSE_FINISH_REASON,
    _GEN_AI_USAGE_INPUT,
    _GEN_AI_USAGE_OUTPUT,
    _GEN_AI_USAGE_TOTAL,
    _GEN_AI_ERROR_TYPE,
    _TENANT_ID,
    _SESSION_ID,
)

_tenant_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("otel_tenant_id", default="")
_session_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("otel_session_id", default="")


def set_tenant_identity(tenant_id: str, session_id: str) -> None:
    _tenant_id_var.set(tenant_id)
    _session_id_var.set(session_id)


def _tracer() -> Tracer:
    return get_tracer(INSTRUMENTATION_NAME, INSTRUMENTATION_VERSION)


class SpanRecordingChatModel:
    """Wraps ``ChatModel.complete`` in a content-free GenAI-convention span.

    Follows the exact same wrapping pattern as
    :class:`~tenantchat.orchestration.providers.recording.MetricRecordingChatModel`:
    delegates every call to the inner model and records latency, the outcome
    (error or OK), and the allowlisted operational attributes around it.

    Tenant and session are read from context variables set by the API layer
    before the graph runs, so a test harness that forgets to set them gets
    empty strings — never a crash and never a spurious value.
    """

    def __init__(self, inner: ChatModel, *, gen_ai_system: str = "") -> None:
        self._inner = inner
        self._system = gen_ai_system

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        tracer = _tracer()
        with tracer.start_as_current_span(
            "chat",
            kind=SpanKind.CLIENT,
            attributes={
                _GEN_AI_SYSTEM: self._system,
                _GEN_AI_OPERATION: "chat",
                _GEN_AI_REQUEST_MODEL: (prompt.bindings.get("model") or self._system),
            },
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            tenant_id = _tenant_id_var.get()
            session_id = _session_id_var.get()
            if tenant_id:
                span.set_attribute(_TENANT_ID, tenant_id)
            if session_id:
                span.set_attribute(_SESSION_ID, session_id)
            try:
                response = await self._inner.complete(prompt, tools=tools)
            except Exception as exc:
                span.set_attribute(_GEN_AI_ERROR_TYPE, type(exc).__name__)
                span.set_status(StatusCode.ERROR)
                raise
            span.set_attribute(_GEN_AI_RESPONSE_MODEL, response.model_name)
            if response.content.strip() and not response.tool_calls:
                span.set_attribute(_GEN_AI_RESPONSE_FINISH_REASON, "stop")
            elif response.tool_calls:
                span.set_attribute(_GEN_AI_RESPONSE_FINISH_REASON, "tool_calls")
            for kind_key, attr in (
                ("prompt_tokens", _GEN_AI_USAGE_INPUT),
                ("completion_tokens", _GEN_AI_USAGE_OUTPUT),
                ("total_tokens", _GEN_AI_USAGE_TOTAL),
            ):
                tokens = response.usage.get(kind_key)
                if tokens is not None:
                    span.set_attribute(attr, int(tokens))
            span.set_status(StatusCode.OK)
            return response
