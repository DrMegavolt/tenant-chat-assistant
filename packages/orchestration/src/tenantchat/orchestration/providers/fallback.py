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
normal ``model_name`` attribution, a fallback hop is counted as a
``MODEL_FALLBACKS`` metric with the bounded failure reason, and the failed
attempts ride the response as ``fallback_hops`` (model name + reason, no
content — R-38) so the trace shows the chain, not only its final link.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import replace

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
from tenantchat.orchestration.model import (
    AssembledPrompt,
    ChatModel,
    FallbackHop,
    ModelResponse,
    ToolSpec,
)
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
        names: The configured model identifiers, aligned with ``models``. A hop
            record names the model it tried (R-38); a chain built without
            names records an empty name rather than an invented one.
    """

    def __init__(
        self,
        models: Sequence[ChatModel],
        *,
        metrics: MetricsReporter | None = None,
        classify: Callable[[Exception], FailureKind] = _classify_llm_error,
        names: Sequence[str] | None = None,
    ) -> None:
        if len(models) < 1:
            raise ValueError("a fallback chain needs at least one model")
        self._models = tuple(models)
        self._metrics = metrics
        self._classify = classify
        self._names = tuple(names) if names is not None else None

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        hops: list[FallbackHop] = []
        for index, model in enumerate(self._models):
            try:
                response = await model.complete(prompt, tools=tools)
            except asyncio.CancelledError:
                raise
            except DependencyUnavailableError:
                if index == len(self._models) - 1:
                    raise
                reason = Status.UNAVAILABLE.value
                self._observe(reason)
            except Exception as exc:
                kind = self._classify(exc)
                if not is_retryable(kind) or index == len(self._models) - 1:
                    raise
                reason = kind.value
                self._observe(reason)
            else:
                # The serving response carries the hops that bought it: the
                # graph can only record what the port returns, and this is the
                # one channel a wrapped chain has (R-38).
                return replace(response, fallback_hops=tuple(hops))
            hops.append(FallbackHop(model_name=self._name(index), reason=reason))
        raise RuntimeError("unreachable: an empty chain cannot call complete")

    def _name(self, index: int) -> str:
        if self._names is None or index >= len(self._names):
            return ""
        return self._names[index]

    def _observe(self, reason: str) -> None:
        if self._metrics is None:
            return
        self._metrics.observe(
            MetricName.MODEL_FALLBACKS,
            1,
            labels={MetricLabelName.REASON.value: reason},
        )
