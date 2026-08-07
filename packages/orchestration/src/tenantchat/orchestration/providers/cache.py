"""A ``ChatModel`` that serves cached safe responses (`AI-002`).

The last lever of the cost-control task is the cheapest one: when the exact
same prompt bytes reach the assistant again, re-answering them is spend with no
information gained. The cache is deliberately narrow so it cannot serve the
wrong thing:

- **The key is the assembled prompt's content hash plus the offered tool set.**
  The hash covers every segment `AI-003` rendered — visitor turns, evidence,
  collected fields, tool results — so two prompts share a key only when they
  are byte-identical, which is the definition of *non-personalized*: if the
  transcript or evidence differed, the hash differs and the lookup misses.
- **Only prose answers are cached.** A response that carries a tool call is
  conversational state, not an answer, and a prompt that resolves to one must
  reach the model every time so nothing is ever executed from a cache.
- **A hit reports zero fresh usage.** The cached response returns with its
  token accounting cleared, so the turn records no spend that did not happen.

The cache is per-process and bounded, exactly like the ``RateLimitStore``'s
in-memory shape: a deployment that needs a shared cache is an operator decision
documented as follow-up, not something this adapter pretends to be.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Final

from tenantchat.core.metrics import (
    CacheResult,
    MetricLabelName,
    MetricName,
    MetricsReporter,
)
from tenantchat.orchestration.model import (
    AssembledPrompt,
    ChatModel,
    ModelResponse,
    ToolSpec,
)

# Hard bound on cached entries: a dictionary whose contents grow with every
# question a visitor invents is a memory leak wearing a cache costume. Oldest
# entries are evicted first, so the hot questions survive.
_MAX_ENTRIES: Final = 1024


class CachingChatModel:
    """Serves byte-identical non-personalized prompts from a bounded cache.

    Args:
        inner: The model chain a miss falls through to.
        metrics: Optional reporter for hit/miss counts. When ``None``, the
            cache still works; only the observability is lost.
        ttl_seconds: How long an entry is trusted before it is re-asked, so an
            answer stops being cached after an operator changes the model or a
            tenant updates its knowledge.
        clock: Injectable monotonic clock for deterministic tests; defaults to
            :func:`time.monotonic`.
    """

    def __init__(
        self,
        inner: ChatModel,
        *,
        metrics: MetricsReporter | None = None,
        ttl_seconds: float = 300.0,
        max_entries: int = _MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("ttl_seconds must be non-negative")
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        self._inner = inner
        self._metrics = metrics
        self._ttl_seconds = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        # key -> (stored_at, response); insertion order doubles as eviction order.
        self._entries: dict[str, tuple[float, ModelResponse]] = {}

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        key = self._key(prompt, tools)
        cached = self._entries.get(key)
        if cached is not None and self._clock() - cached[0] < self._ttl_seconds:
            self._observe(CacheResult.HIT)
            # A hit spent no fresh tokens, so the response must not claim any:
            # attribution is honest spend, and the metric already counted the
            # original call's tokens when it happened.
            return replace(cached[1], usage={})
        if cached is not None:
            del self._entries[key]
        self._observe(CacheResult.MISS)
        response = await self._inner.complete(prompt, tools=tools)
        if self._cacheable(response):
            self._store(key, response)
        return response

    @staticmethod
    def _key(prompt: AssembledPrompt, tools: Sequence[ToolSpec]) -> str:
        offered = ",".join(sorted(spec.name for spec in tools))
        material = f"{prompt.content_hash}|{offered}".encode()
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _cacheable(response: ModelResponse) -> bool:
        return not response.tool_calls and bool(response.content.strip())

    def _store(self, key: str, response: ModelResponse) -> None:
        if len(self._entries) >= self._max_entries:
            self._entries.pop(next(iter(self._entries)))
        self._entries[key] = (self._clock(), response)

    def _observe(self, result: CacheResult) -> None:
        if self._metrics is None:
            return
        self._metrics.observe(
            MetricName.RESPONSE_CACHE,
            1,
            labels={MetricLabelName.RESULT.value: result.value},
        )
