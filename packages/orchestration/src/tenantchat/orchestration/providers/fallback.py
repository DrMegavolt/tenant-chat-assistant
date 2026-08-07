"""A ``ChatModel`` that hands an outage to the next provider (`AI-002`).

The resilience envelope (`REL-001`) retries inside one provider client until
its own budget is spent; this adapter is the step above it — when the primary
provider is out (dead, or its circuit breaker has opened), the logical call
moves to the next model in the tenant's chain instead of failing the turn.

The fallback rule mirrors the retry rule on purpose: only an outage-shaped
failure moves to the next model, because a contract failure (a malformed
response, a non-``429`` refusal) is a release bug a second provider is just as
likely to have. A breaker refusal is *always* fallback-worthy: the primary
already tried and gave up, so there is nothing to save by calling it again.

Observability: the turn records the model that actually answered through the
normal ``model_name`` attribution, and a fallback hop is counted as a
``MODEL_FALLBACKS`` metric with the bounded failure reason — the two together
make fallback auditable (the trace names the serving model) and measurable (the
series counts how often the primary was unavailable).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence

from tenantchat.core.metrics import (
    MetricLabelName,
    MetricName,
    MetricsReporter,
    Status,
)
from tenantchat.core.resilience import (
    DependencyUnavailableError,
    FailureKind,
    is_retryable,
)
from tenantchat.orchestration.model import AssembledPrompt, ChatModel, ModelResponse, ToolSpec
from tenantchat.orchestration.providers.openai_compatible import _classify_llm_error


class FallbackChatModel:
    """Tries an ordered chain of models until one answers or the chain ends.

    Args:
        models: The chain, best first. The last model's failure is the logical
            call's failure, raised unchanged so the graph's handoff logic sees
            the same error it would have seen without a chain.
        metrics: Optional reporter for fallback hops. When ``None``, fallbacks
            still happen; only the count is lost.
        classify: Maps an exception raised by a model to the bounded retry
            decision. Defaults to the OpenAI-compatible classification.
    """

    def __init__(
        self,
        models: Sequence[ChatModel],
        *,
        metrics: MetricsReporter | None = None,
        classify: Callable[[Exception], FailureKind] = _classify_llm_error,
    ) -> None:
        if len(models) < 1:
            raise ValueError("a fallback chain needs at least one model")
        self._models = tuple(models)
        self._metrics = metrics
        self._classify = classify

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        for index, model in enumerate(self._models):
            try:
                return await model.complete(prompt, tools=tools)
            except asyncio.CancelledError:
                raise
            except DependencyUnavailableError:
                if index == len(self._models) - 1:
                    raise
                self._observe(Status.UNAVAILABLE.value)
            except Exception as exc:
                kind = self._classify(exc)
                if not is_retryable(kind) or index == len(self._models) - 1:
                    raise
                self._observe(kind.value)
        raise RuntimeError("unreachable: an empty chain cannot call complete")

    def _observe(self, reason: str) -> None:
        if self._metrics is None:
            return
        self._metrics.observe(
            MetricName.MODEL_FALLBACKS,
            1,
            labels={MetricLabelName.REASON.value: reason},
        )
