"""Golden offline evaluation of retrieval, citation, abstention, and grounding.

The harness (``evals.runner``) is the minimum scoreboard needed to tune the
RAG path (`RAG-009`): deterministic fixtures, pinned component versions, and
a diffable summary. ``RAG-008`` grows this runner into the versioned
release-gate suite (``evals.gate``) rather than building a second one.
"""

from evals.corpus import FixtureCorpus
from evals.retriever import Retriever
from evals.scorer import EvaluationReport

__all__ = ["EvaluationReport", "FixtureCorpus", "Retriever"]
