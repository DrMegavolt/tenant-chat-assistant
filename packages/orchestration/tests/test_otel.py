"""Content-free, correctly attributed GenAI spans."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from tenantchat.orchestration import otel
from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    ChatModel,
    MessageRole,
    ModelResponse,
    PromptRegion,
    PromptSegment,
    ToolSpec,
)


def _prompt() -> AssembledPrompt:
    return AssembledPrompt(
        template_id="otel-test",
        template_version=1,
        bindings={},
        messages=(
            AssembledMessage(
                role=MessageRole.USER,
                segments=(PromptSegment("visitor", PromptRegion.UNTRUSTED, "private"),),
            ),
        ),
    )


class _Model:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error

    async def complete(
        self, prompt: AssembledPrompt, *, tools: Sequence[ToolSpec]
    ) -> ModelResponse:
        del prompt, tools
        if self.error is not None:
            raise self.error
        return ModelResponse(
            content="answer",
            model_name="provider-response-model",
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )


def _recorder(monkeypatch: pytest.MonkeyPatch) -> tuple[TracerProvider, InMemorySpanExporter]:
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(otel, "_tracer", lambda: provider.get_tracer("test"))
    return provider, exporter


def test_model_span_uses_configured_request_model_and_current_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider, exporter = _recorder(monkeypatch)
    tracer = provider.get_tracer("parent")
    wrapped: ChatModel = otel.SpanRecordingChatModel(
        _Model(), gen_ai_system="openai", request_model="gpt-test"
    )

    async def run() -> None:
        with tracer.start_as_current_span("http-request"):
            await wrapped.complete(_prompt(), tools=())

    asyncio.run(run())

    spans = exporter.get_finished_spans()
    model_span = next(span for span in spans if span.name == "chat otel-test@1")
    parent_span = next(span for span in spans if span.name == "http-request")
    attributes = model_span.attributes or {}
    assert model_span.parent is not None
    assert model_span.parent.span_id == parent_span.context.span_id
    assert attributes["gen_ai.system"] == "openai"
    assert attributes["gen_ai.request.model"] == "gpt-test"
    assert attributes["gen_ai.response.model"] == "provider-response-model"
    assert attributes["openinference.span.kind"] == "LLM"


def test_model_failure_records_type_without_exception_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _provider, exporter = _recorder(monkeypatch)
    secret = "visitor@example.test must never enter telemetry"
    wrapped: ChatModel = otel.SpanRecordingChatModel(
        _Model(error=RuntimeError(secret)),
        gen_ai_system="openai_compatible",
        request_model="local-model",
    )

    with pytest.raises(RuntimeError, match="visitor@example.test"):
        asyncio.run(wrapped.complete(_prompt(), tools=()))

    span = exporter.get_finished_spans()[0]
    attributes = span.attributes or {}
    assert span.status.description is None
    assert attributes["gen_ai.error.type"] == "RuntimeError"
    assert span.events == ()
    assert secret not in repr(attributes)
