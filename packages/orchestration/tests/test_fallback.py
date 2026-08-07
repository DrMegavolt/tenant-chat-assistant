"""AI-002: model fallback selection over an ordered chain of providers.

The fallback rule is pinned here: an outage-shaped failure on a non-last model
moves to the next, a contract failure does not, and the last model's failure is
the logical call's failure. Every hop is recorded as a bounded metric, so
fallback is measurable and the serving model is the turn's own attribution.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

import httpx
import pytest

from tenantchat.core.metrics import MetricName
from tenantchat.core.resilience import Dependency, DependencyUnavailableError, ResiliencePolicy
from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    MessageRole,
    ModelResponse,
    PromptRegion,
    PromptSegment,
    ToolSpec,
)
from tenantchat.orchestration.providers.fallback import FallbackChatModel
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel


def _prompt() -> AssembledPrompt:
    return AssembledPrompt(
        template_id="fallback-test",
        template_version=1,
        bindings={},
        messages=(
            AssembledMessage(
                role=MessageRole.USER,
                segments=(PromptSegment("segment", PromptRegion.UNTRUSTED, "hello"),),
            ),
        ),
    )


class _RecordingMetrics:
    def __init__(self) -> None:
        self.observations: list[tuple[str, float, dict[str, str]]] = []

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.observations.append((name.value, value, dict(labels or {})))


class _StubModel:
    """One model in the chain: fixed response, or one fixed failure per call."""

    def __init__(
        self,
        response: ModelResponse,
        *,
        failure: BaseException | None = None,
        name: str = "stub",
    ) -> None:
        self._response = response
        self._failure = failure
        self._name = name
        self.calls = 0

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        del prompt, tools
        self.calls += 1
        if self._failure is not None:
            raise self._failure
        return self._response

    @property
    def name(self) -> str:
        return self._name


def _chain(*models: _StubModel, metrics: _RecordingMetrics | None = None) -> FallbackChatModel:
    return FallbackChatModel(models, metrics=metrics)


def _run(chain: FallbackChatModel) -> ModelResponse:
    return asyncio.run(chain.complete(_prompt(), tools=()))


class TestFallbackSelection:
    def test_an_outage_on_the_primary_is_answered_by_the_fallback(self) -> None:
        primary = _StubModel(ModelResponse(content="primary"), failure=httpx.ConnectError("down"))
        fallback = _StubModel(ModelResponse(content="fallback answer"))
        chain = _chain(primary, fallback)

        response = _run(chain)

        assert response.content == "fallback answer"
        assert primary.calls == 1
        assert fallback.calls == 1

    def test_a_healthy_primary_never_touches_the_fallback(self) -> None:
        primary = _StubModel(ModelResponse(content="primary answer"))
        fallback = _StubModel(ModelResponse(content="fallback"))

        response = _run(_chain(primary, fallback))

        assert response.content == "primary answer"
        assert primary.calls == 1
        assert fallback.calls == 0

    def test_a_contract_failure_does_not_fall_back(self) -> None:
        """A malformed response or a 4xx is a release bug, not an outage."""
        request = httpx.Request("POST", "http://primary/v1/chat/completions")
        refusal = httpx.HTTPStatusError("bad key", request=request, response=httpx.Response(401))
        primary = _StubModel(ModelResponse(content=""), failure=refusal)
        fallback = _StubModel(ModelResponse(content="fallback"))

        with pytest.raises(httpx.HTTPStatusError):
            _run(_chain(primary, fallback))
        assert primary.calls == 1
        assert fallback.calls == 0

    def test_a_breaker_refusal_falls_back(self) -> None:
        """The primary's breaker already gave up; the fallback is the next try."""
        primary = _StubModel(
            ModelResponse(content=""), failure=DependencyUnavailableError(dependency=Dependency.LLM)
        )
        fallback = _StubModel(ModelResponse(content="rescued"))

        response = _run(_chain(primary, fallback))

        assert response.content == "rescued"
        assert primary.calls == 1
        assert fallback.calls == 1

    def test_the_last_model_s_failure_is_the_logical_call_s_failure(self) -> None:
        primary = _StubModel(ModelResponse(content=""), failure=httpx.ReadTimeout("slow"))
        fallback = _StubModel(ModelResponse(content=""), failure=httpx.ReadTimeout("also slow"))

        with pytest.raises(httpx.TimeoutException):
            _run(_chain(primary, fallback))
        assert primary.calls == 1
        assert fallback.calls == 1

    def test_a_cancelled_request_passes_straight_through(self) -> None:
        async def scenario() -> None:
            primary = _StubModel(ModelResponse(content=""), failure=asyncio.CancelledError())
            fallback = _StubModel(ModelResponse(content="fallback"))
            task = asyncio.create_task(_chain(primary, fallback).complete(_prompt(), tools=()))
            with pytest.raises(asyncio.CancelledError):
                await task
            assert fallback.calls == 0

        asyncio.run(scenario())


class TestFallbackObservability:
    def test_each_fallback_hop_records_a_bounded_metric(self) -> None:
        metrics = _RecordingMetrics()
        primary = _StubModel(ModelResponse(content=""), failure=httpx.ReadTimeout("slow"))
        fallback = _StubModel(ModelResponse(content="ok"))
        chain = _chain(primary, fallback, metrics=metrics)

        _run(chain)

        falls = [obs for obs in metrics.observations if obs[0] == MetricName.MODEL_FALLBACKS.value]
        assert falls == [
            ("tenantchat_model_fallbacks_total", 1.0, {"reason": "timeout"}),
        ]

    def test_a_breaker_refusal_records_the_unavailable_reason(self) -> None:
        metrics = _RecordingMetrics()
        primary = _StubModel(
            ModelResponse(content=""), failure=DependencyUnavailableError(dependency=Dependency.LLM)
        )
        fallback = _StubModel(ModelResponse(content="ok"))
        chain = _chain(primary, fallback, metrics=metrics)

        _run(chain)

        falls = [obs for obs in metrics.observations if obs[0] == MetricName.MODEL_FALLBACKS.value]
        assert falls == [("tenantchat_model_fallbacks_total", 1.0, {"reason": "unavailable"})]


class TestFallbackWithTheRealAdapter:
    def test_a_dead_primary_client_falls_back_to_a_live_secondary(self) -> None:
        """The full path: two OpenAI-compatible clients over fake transports."""

        def dead_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(503)

        def live_handler(request: httpx.Request) -> httpx.Response:
            del request
            return httpx.Response(200, json={"choices": [{"message": {"content": "rescued"}}]})

        async def scenario() -> None:
            primary = OpenAICompatibleChatModel(
                base_url="http://primary/v1",
                model="primary-model",
                policy=ResiliencePolicy(),
                transport=httpx.MockTransport(dead_handler),
            )
            secondary = OpenAICompatibleChatModel(
                base_url="http://secondary/v1",
                model="secondary-model",
                policy=ResiliencePolicy(),
                transport=httpx.MockTransport(live_handler),
            )
            chain = FallbackChatModel((primary, secondary))
            try:
                response = await chain.complete(_prompt(), tools=())
                assert response.content == "rescued"
                assert response.model_name == "secondary-model"
            finally:
                await primary.close()
                await secondary.close()

        asyncio.run(scenario())
