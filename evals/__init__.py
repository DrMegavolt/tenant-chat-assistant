"""Golden offline evaluation of retrieval, citation, and abstention behavior.

The harness (``evals.runner``) is the minimum scoreboard needed to tune the
RAG path (`RAG-009`): deterministic fixtures, three scores, pinned component
versions, and a diffable summary. ``RAG-008`` grows this runner into the
versioned release-gate suite rather than building a second one.
"""

from evals.corpus import FixtureCorpus
from evals.retriever import Retriever
from evals.scorer import EvaluationReport
from evals.versions import component_manifest

__all__ = ["EvaluationReport", "FixtureCorpus", "Retriever", "component_manifest"]
