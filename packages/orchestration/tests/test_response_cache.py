"""AI-002: the safe-response cache.

The cache's contract is that it can never serve the wrong thing: only
byte-identical prompts share a key, only prose answers are stored, a hit
reports no fresh spend, and the entry is bounded and expiring. These tests pin
each of those properties and the hit/miss observability.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence

from tenantchat.core.metrics import MetricName
from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    FallbackHop,
    MessageRole,
    ModelResponse,
    PromptRegion,
    PromptSegment,
    ToolCall,
    ToolSpec,
)
from tenantchat.orchestration.providers.cache import CachingChatModel

TOOL = ToolSpec(
    name="check_service_area",
    description="Check a ZIP code.",
    parameters={"type": "object", "properties": {"zip": {"type": "string"}}},
)


def _prompt(*, content: str = "What are your hours?") -> AssembledPrompt:
    return AssembledPrompt(
        template_id="cache-test",
        template_version=1,
        bindings={},
        messages=(
            AssembledMessage(
                role=MessageRole.USER,
                segments=(PromptSegment("segment", PromptRegion.UNTRUSTED, content),),
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


class _CountingModel:
    def __init__(self, response: ModelResponse) -> None:
        self._response = response
        self.calls = 0

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        del prompt, tools
        self.calls += 1
        return self._response


def _run(
    cache: CachingChatModel, prompt: AssembledPrompt, tools: Sequence[ToolSpec]
) -> ModelResponse:
    return asyncio.run(cache.complete(prompt, tools=tools))


class TestCacheHits:
    def test_a_byte_identical_prompt_is_served_without_calling_the_model(self) -> None:
        inner = _CountingModel(ModelResponse(content="Open until 7pm.", model_name="qwen"))
        cache = CachingChatModel(inner)

        first = _run(cache, _prompt(), ())
        second = _run(cache, _prompt(), ())

        assert first.content == second.content == "Open until 7pm."
        assert inner.calls == 1

    def test_a_different_message_misses(self) -> None:
        inner = _CountingModel(ModelResponse(content="Open until 7pm."))
        cache = CachingChatModel(inner)

        _run(cache, _prompt(), ())
        _run(cache, _prompt(content="What are your prices?"), ())

        assert inner.calls == 2

    def test_a_different_tool_set_misses(self) -> None:
        """The offered tools are part of the key: same words, different ability."""
        inner = _CountingModel(ModelResponse(content="Done."))
        cache = CachingChatModel(inner)

        _run(cache, _prompt(), (TOOL,))
        _run(cache, _prompt(), ())

        assert inner.calls == 2

    def test_a_tool_call_response_is_never_cached(self) -> None:
        """Conversational state must reach the model every time."""
        inner = _CountingModel(
            ModelResponse(
                content="",
                tool_calls=(ToolCall(call_id="call-1", name="check_service_area", arguments={}),),
            )
        )
        cache = CachingChatModel(inner)

        _run(cache, _prompt(), (TOOL,))
        _run(cache, _prompt(), (TOOL,))

        assert inner.calls == 2

    def test_a_hit_reports_zero_fresh_usage(self) -> None:
        """Attribution is honest spend: the second turn spent no tokens."""
        inner = _CountingModel(
            ModelResponse(
                content="Open until 7pm.",
                usage={"prompt_tokens": 30, "completion_tokens": 4, "total_tokens": 34},
            )
        )
        cache = CachingChatModel(inner)

        first = _run(cache, _prompt(), ())
        second = _run(cache, _prompt(), ())

        assert first.usage["total_tokens"] == 34
        assert second.usage == {}

    def test_a_hit_is_marked_and_a_miss_is_not(self) -> None:
        """The record must tell a served-from-cache answer from a fresh one.

        Without the flag, a cache-served turn reads as a fresh completion that
        cost zero tokens — spend attribution and cache-hit auditing both lie.
        """
        inner = _CountingModel(ModelResponse(content="Open until 7pm."))
        cache = CachingChatModel(inner)

        first = _run(cache, _prompt(), ())
        second = _run(cache, _prompt(), ())

        assert first.cache_hit is False
        assert second.cache_hit is True

    def test_a_hit_carries_no_stale_fallback_chain(self) -> None:
        """The stored response's original hops describe the turn that computed
        it. The serving turn made no provider attempts, so its record must not
        borrow the earlier turn's outage story."""
        inner = _CountingModel(
            ModelResponse(
                content="Open until 7pm.",
                fallback_hops=(FallbackHop(model_name="primary", reason="timeout"),),
            )
        )
        cache = CachingChatModel(inner)

        _run(cache, _prompt(), ())
        second = _run(cache, _prompt(), ())

        assert second.cache_hit is True
        assert second.fallback_hops == ()


class TestCacheBounds:
    def test_a_cached_entry_expires_after_the_ttl(self) -> None:
        inner = _CountingModel(ModelResponse(content="Open until 7pm."))
        # store(first) = 0.0; lookup(second) = 301.0, past the 300s ttl.
        clock = iter([0.0, 301.0, 301.0])
        cache = CachingChatModel(inner, ttl_seconds=300.0, clock=lambda: next(clock))

        _run(cache, _prompt(), ())
        _run(cache, _prompt(), ())

        assert inner.calls == 2

    def test_oldest_entries_are_evicted_when_the_cache_is_full(self) -> None:
        inner = _CountingModel(ModelResponse(content="x"))
        cache = CachingChatModel(inner, max_entries=1)

        first = _prompt(content="first question")
        second = _prompt(content="second question")
        _run(cache, first, ())
        _run(cache, second, ())
        _run(cache, first, ())

        # The first entry was evicted when the second was stored, so re-asking
        # the first question calls the model again.
        assert inner.calls == 3


class TestCacheObservability:
    def test_hits_and_misses_are_recorded_with_bounded_labels(self) -> None:
        metrics = _RecordingMetrics()
        inner = _CountingModel(ModelResponse(content="Open until 7pm."))
        cache = CachingChatModel(inner, metrics=metrics)

        _run(cache, _prompt(), ())
        _run(cache, _prompt(), ())

        results = [
            (value, labels["result"])
            for name, value, labels in metrics.observations
            if name == MetricName.RESPONSE_CACHE.value
        ]
        assert sorted(results) == [(1.0, "hit"), (1.0, "miss")]
