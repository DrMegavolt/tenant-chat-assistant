"""The shared lexical vocabulary: canonical tokenization for overlap scoring.

Lives in the domain because three consumers must agree on what a "word" is —
lexical retrieval (`RAG-004`), abstention calibration, and the claim validator
(`RAG-007`) that must prove an answer's words against evidence. One definition
in the domain is what keeps a sentence grounded in retrieval and a sentence
grounded in a copy of the same code from disagreeing.
"""

from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9']+")
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


def tokenize(text: str) -> list[str]:
    """Lowercased non-stopword tokens in order: the shared lexical vocabulary."""
    return [word for word in _WORD.findall(text.lower()) if word not in _STOPWORDS]


def query_words(text: str) -> frozenset[str]:
    """The unordered vocabulary of a text, for overlap scoring."""
    return frozenset(tokenize(text))
