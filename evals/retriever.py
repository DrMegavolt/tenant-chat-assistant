"""The retriever protocol the scoreboard evaluates, plus its v1 baseline.

``RAG-004`` plugs the production hybrid retriever into :class:`Retriever`;
until then the harness scores :class:`LexicalOverlapRetriever`, a
deterministic baseline that makes the scoreboard meaningful from day one (a
retriever that cannot beat word-overlap is not worth tuning).

All scoring is deterministic: tokenization is fixed, ties break on chunk id,
and the tenant filter plus the ``active`` flag apply exactly as the
production index enforces them.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from evals.corpus import FixtureCorpus

_WORD = re.compile(r"[a-z0-9']+")
_MIN_STEM = 4
_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "at",
        "can",
        "do",
        "does",
        "for",
        "how",
        "i",
        "in",
        "is",
        "it",
        "my",
        "of",
        "on",
        "our",
        "out",
        "that",
        "the",
        "there",
        "to",
        "we",
        "what",
        "who",
        "you",
        "your",
    }
)


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """One retrieved chunk and its deterministic relevance score."""

    chunk_id: str
    score: float


class Retriever(Protocol):
    """Something that returns the top ``k`` chunks for a tenant's query."""

    async def retrieve(self, query: str, *, tenant_id: str, k: int) -> Sequence[RetrievalResult]:
        """Rank the tenant's active chunks for one query."""
        ...


@dataclass(frozen=True, slots=True)
class RetrieverConfig:
    """The exact parameters a run used, so two runs are comparable."""

    name: str
    version: str
    k: int
    description: str


def _words(text: str) -> frozenset[str]:
    return frozenset(word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS)


def _matches(query_word: str, chunk_word: str) -> bool:
    """Whole-word match, or a shared stem of at least four characters.

    Prefix stemming models how real lexical search treats inflections
    (``cleaning``/``clean``, ``upgrades``/``upgrade``) without adding a
    dependency; the minimum length keeps unrelated short words from
    collapsing onto each other.
    """
    if query_word == chunk_word:
        return True
    return len(query_word) >= _MIN_STEM and chunk_word.startswith(query_word)


class LexicalOverlapRetriever:
    """Deterministic word-overlap baseline over the fixture corpus.

    Scores a chunk by the fraction of query words its text contains (with
    prefix stemming), filters to the tenant's active chunks, and ranks by
    ``(-score, chunk_id)``. The abstention threshold is separate: it is the
    scorer's decision boundary, not part of ranking.
    """

    def __init__(self, corpus: FixtureCorpus) -> None:
        self._corpus = corpus

    async def retrieve(self, query: str, *, tenant_id: str, k: int) -> Sequence[RetrievalResult]:
        query_words = _words(query)
        scored: list[tuple[float, str]] = []
        for chunk in self._corpus.chunks:
            if chunk.tenant_id != tenant_id or not chunk.active:
                continue
            chunk_words = _words(chunk.text)
            overlap = sum(1 for qw in query_words if any(_matches(qw, cw) for cw in chunk_words))
            score = overlap / len(query_words) if query_words else 0.0
            scored.append((score, chunk.chunk_id))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return tuple(
            RetrievalResult(chunk_id=chunk_id, score=score) for score, chunk_id in scored[:k]
        )


def baseline_config(k: int) -> RetrieverConfig:
    """The v1 retriever configuration the scoreboard pins in its report."""
    return RetrieverConfig(
        name="lexical-overlap",
        version="v1",
        k=k,
        description="word-overlap baseline over active, tenant-filtered chunks",
    )
