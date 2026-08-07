"""REL-001 failure injection for the OpenAI-compatible LLM client.

Every failure class the acceptance criteria name — timeout, connection reset,
``429``, ``5xx``, malformed response, and recovery — is exercised here against
a fake transport, plus the two structural guarantees: a cancelled request is
never retried, and an open circuit fails fast without touching the network. The
retry budget is bounded by the policy, and the only request a retry can reissue
is the chat-completions POST — the model call is side-effect-free, so a retry
after a timeout cannot duplicate a business action (those carry idempotency
keys in the domain services that own them).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx
import pytest

from tenantchat.core.metrics import MetricName
from tenantchat.core.resilience import (
    CircuitPolicy,
    DependencyUnavailableError,
    ResiliencePolicy,
    RetryPolicy,
)
from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    MessageRole,
    ModelResponse,
    PromptRegion,
    PromptSegment,
    ToolSpec,
)
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel
from tenantchat.orchestration.providers.recording import MetricRecordingChatModel

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


def message(role: MessageRole, content: str) -> AssembledMessage:
    region = PromptRegion.UNTRUSTED if role is MessageRole.USER else PromptRegion.TRUSTED
    return AssembledMessage(
        role=role,
        segments=(PromptSegment("segment", region, content),),
    )


def prompt() -> AssembledPrompt:
    return AssembledPrompt(
        template_id="resilience-test",
        template_version=1,
        bindings={},
        messages=(message(MessageRole.USER, "hello"),),
    )


def policy(
    *,
    max_attempts: int = 3,
    threshold: int = 5,
    cooldown: float = 30.0,
    total_deadline: float = 180.0,
) -> ResiliencePolicy:
    return ResiliencePolicy(
        retries=RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=0.0,
            max_delay_seconds=0.01,
            jitter_seconds=0.0,
        ),
        circuit=CircuitPolicy(failure_threshold=threshold, cooldown_seconds=cooldown),
        total_deadline_seconds=total_deadline,
    )


class _RecordingMetrics:
    def __init__(self) -> None:
        self.observations: list[tuple[str, float, Mapping[str, str]]] = []

    def observe(
        self,
        name: MetricName,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.observations.append((name.value, value, dict(labels or {})))


def _run(handler: httpx.MockTransport, *, applied_policy: ResiliencePolicy) -> ModelResponse:
    async def invoke() -> ModelResponse:
        adapter = OpenAICompatibleChatModel(
            base_url="http://provider/v1",
            model="local-model",
            policy=applied_policy,
            transport=handler,
        )
        try:
            return await adapter.complete(prompt(), tools=())
        finally:
            await adapter.close()

    return asyncio.run(invoke())


def _raise(handler: httpx.MockTransport, *, applied_policy: ResiliencePolicy) -> Exception:
    async def invoke() -> None:
        adapter = OpenAICompatibleChatModel(
            base_url="http://provider/v1",
            model="local-model",
            policy=applied_policy,
            transport=handler,
        )
        try:
            await adapter.complete(prompt(), tools=())
        finally:
            await adapter.close()

    try:
        asyncio.run(invoke())
    except Exception as exc:
        return exc
    raise AssertionError("expected the model call to raise")


class TestOutageShapedFailures:
    def test_a_read_timeout_is_retried_then_raises_timeout(self) -> None:
        attempts = 0
        seen_paths: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            seen_paths.append(request.url.path)
            raise httpx.ReadTimeout("provider did not answer", request=request)

        exc = _raise(httpx.MockTransport(handler), applied_policy=policy())
        assert isinstance(exc, httpx.TimeoutException)
        assert attempts == 3
        assert seen_paths == ["/v1/chat/completions"] * 3

    def test_a_connection_reset_is_retried_then_raises(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            raise httpx.RemoteProtocolError("connection closed", request=request)

        exc = _raise(httpx.MockTransport(handler), applied_policy=policy())
        assert isinstance(exc, httpx.TransportError)
        assert attempts == 3

    def test_a_rate_limit_is_retried_and_recovers(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(429)
            return httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}}]})

        response = _run(httpx.MockTransport(handler), applied_policy=policy())
        assert response.content == "recovered"
        assert attempts == 3

    def test_a_5xx_is_retried_then_raises_http_status_error(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        exc = _raise(httpx.MockTransport(handler), applied_policy=policy())
        assert isinstance(exc, httpx.HTTPStatusError)
        assert attempts == 3

    def test_an_outage_that_recovers_mid_budget_succeeds(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        response = _run(httpx.MockTransport(handler), applied_policy=policy())
        assert response.content == "ok"
        assert attempts == 3


class TestContractFailuresAreNotRetried:
    def test_a_malformed_response_is_not_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(200, json={"choices": []})

        exc = _raise(httpx.MockTransport(handler), applied_policy=policy())
        assert isinstance(exc, ValueError)
        assert attempts == 1

    def test_a_non_rate_limit_4xx_is_not_retried(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(401, json={"error": "bad key"})

        exc = _raise(httpx.MockTransport(handler), applied_policy=policy())
        assert isinstance(exc, httpx.HTTPStatusError)
        assert attempts == 1


class TestCancellationAndCircuitBreaking:
    def test_the_total_deadline_cancels_a_hanging_attempt(self) -> None:
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(10)
            return httpx.Response(200, json={"choices": [{"message": {"content": "late"}}]})

        async def scenario() -> None:
            adapter = OpenAICompatibleChatModel(
                base_url="http://provider/v1",
                model="local-model",
                policy=policy(total_deadline=0.05),
                transport=httpx.MockTransport(handler),
            )
            with pytest.raises(TimeoutError):
                await adapter.complete(prompt(), tools=())
            await adapter.close()

        asyncio.run(scenario())
        assert attempts == 1

    def test_cancellation_propagates_without_a_retry(self) -> None:
        attempts = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            await asyncio.sleep(10)
            return httpx.Response(200, json={"choices": [{"message": {"content": "late"}}]})

        async def scenario() -> None:
            adapter = OpenAICompatibleChatModel(
                base_url="http://provider/v1",
                model="local-model",
                policy=policy(),
                transport=httpx.MockTransport(handler),
            )
            task = asyncio.create_task(adapter.complete(prompt(), tools=()))
            await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await adapter.close()

        asyncio.run(scenario())
        assert attempts == 1

    def test_an_open_breaker_fails_fast_without_touching_the_network(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(503)

        adapter = OpenAICompatibleChatModel(
            base_url="http://provider/v1",
            model="local-model",
            policy=policy(max_attempts=1, threshold=2),
            transport=httpx.MockTransport(handler),
        )

        async def scenario() -> None:
            for _ in range(2):
                with pytest.raises(httpx.HTTPStatusError):
                    await adapter.complete(prompt(), tools=())
            with pytest.raises(DependencyUnavailableError):
                await adapter.complete(prompt(), tools=())
            await adapter.close()

        asyncio.run(scenario())
        assert attempts == 2

    def test_a_half_open_probe_recovers_the_breaker(self) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                return httpx.Response(503)
            return httpx.Response(200, json={"choices": [{"message": {"content": "back"}}]})

        async def scenario() -> None:
            adapter = OpenAICompatibleChatModel(
                base_url="http://provider/v1",
                model="local-model",
                policy=policy(max_attempts=1, threshold=2, cooldown=0.05),
                transport=httpx.MockTransport(handler),
            )
            for _ in range(2):
                with pytest.raises(httpx.HTTPStatusError):
                    await adapter.complete(prompt(), tools=())
            with pytest.raises(DependencyUnavailableError):
                await adapter.complete(prompt(), tools=())
            assert adapter._resilience.breaker.state.value == "open"
            await asyncio.sleep(0.06)
            response = await adapter.complete(prompt(), tools=())
            assert response.content == "back"
            assert adapter._resilience.breaker.state.value == "closed"
            await adapter.close()

        asyncio.run(scenario())
        assert attempts == 3


class TestObservability:
    def test_retries_and_circuit_state_reach_the_metrics_port(self) -> None:
        metrics = _RecordingMetrics()
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise httpx.ReadTimeout("slow", request=request)
            return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

        async def scenario() -> None:
            adapter = OpenAICompatibleChatModel(
                base_url="http://provider/v1",
                model="local-model",
                policy=policy(),
                transport=httpx.MockTransport(handler),
                metrics=metrics,
            )
            response = await adapter.complete(prompt(), tools=())
            assert response.content == "ok"
            await adapter.close()

        asyncio.run(scenario())

        retries = [
            observation
            for observation in metrics.observations
            if observation[0] == MetricName.DEPENDENCY_RETRIES.value
        ]
        assert len(retries) == 2
        assert all(labels == {"dependency": "llm", "reason": "timeout"} for _, _, labels in retries)

    def test_a_breaker_refusal_records_the_unavailable_status(self) -> None:
        metrics = _RecordingMetrics()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        async def scenario() -> None:
            raw = OpenAICompatibleChatModel(
                base_url="http://provider/v1",
                model="local-model",
                policy=policy(max_attempts=1, threshold=2),
                transport=httpx.MockTransport(handler),
            )
            wrapped = MetricRecordingChatModel(raw, metrics)
            for _ in range(2):
                with pytest.raises(httpx.HTTPStatusError):
                    await wrapped.complete(prompt(), tools=())
            with pytest.raises(DependencyUnavailableError):
                await wrapped.complete(prompt(), tools=())
            await raw.close()

        asyncio.run(scenario())

        calls = [
            labels for name, _, labels in metrics.observations if name == MetricName.LLM_CALLS.value
        ]
        assert {"status": "unavailable", "template": "resilience-test@1"} in calls
