"""A ``ChatModel`` wrapper that turns each completion into OBS-002 metrics.

The graph is deliberately provider-agnostic, so the observable part of a model
call — latency, tokens, outcome — is recorded around the port rather than
inside a provider. Wrapping at the composition root means every deployment that
composes a model gets the metrics and no node has to know it is being observed.

The template label comes from the assembled prompt's registry reference
(``dispatch-system@3``), which is a bounded versioned artifact name, never free
text. ``response.usage`` is provider accounting; only the three documented
kinds are projected onto labels, so an unknown usage key cannot introduce a
dimension.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Final

import httpx

from tenantchat.core.metrics import MetricName, MetricsReporter, Status, TokenKind
from tenantchat.orchestration.model import AssembledPrompt, ChatModel, ModelResponse, ToolSpec

# The usage keys the OpenAI-compatible contract documents, projected onto the
# three bounded token kinds. Anything else a provider returns is not recorded:
# an unknown key would be a new label value by accident.
_USAGE_KINDS: Final = (
    (TokenKind.PROMPT, "prompt_tokens"),
    (TokenKind.COMPLETION, "completion_tokens"),
    (TokenKind.TOTAL, "total_tokens"),
)


class MetricRecordingChatModel:
    """Records every completion on the wrapped model against the metrics port."""

    def __init__(self, inner: ChatModel, metrics: MetricsReporter) -> None:
        self._inner = inner
        self._metrics = metrics

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        started = time.monotonic()
        try:
            response = await self._inner.complete(prompt, tools=tools)
        except httpx.TimeoutException:
            self._record(Status.TIMEOUT, prompt, started)
            raise
        except Exception:
            # Deliberately broad: any provider failure is one status on the
            # error series, whatever its type. The graph converts the failure
            # into a handoff; the metric records that it happened.
            self._record(Status.ERROR, prompt, started)
            raise
        self._record(Status.OK, prompt, started)
        for kind, key in _USAGE_KINDS:
            tokens = response.usage.get(key)
            if tokens is not None:
                self._metrics.observe(
                    MetricName.LLM_TOKENS,
                    float(tokens),
                    labels={"kind": kind.value, "template": prompt.template_ref},
                )
        return response

    def _record(self, status: Status, prompt: AssembledPrompt, started: float) -> None:
        labels = {"status": status.value, "template": prompt.template_ref}
        self._metrics.observe(MetricName.LLM_CALLS, 1.0, labels=labels)
        self._metrics.observe(
            MetricName.LLM_LATENCY,
            time.monotonic() - started,
            labels=labels,
        )
