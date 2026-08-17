"""The governed corpus the demo seeds must be routable to the knowledge agent.

The live cluster indexed its financing documents correctly and still answered
every financing question with a clarification: the router had no financing
vocabulary, so "what financing options are available" scored highest as
`availability` matching the word "available". The corpus was perfect and
unreachable, and nothing failed — retrieval is only exercised once a turn
reaches the agent that performs it.

This holds the two halves together: whatever `scripts/seed_knowledge.py` loads,
a question about it must reach the general agent, which is the only one that
retrieves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scripts.seed_knowledge import _TENANTS
from tenantchat.core.routing import ROUTING_POLICY, IntentName, RoutingOutcome

_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("tenant_id", "filepath", "title"), _TENANTS)
def test_the_seeded_document_exists(tenant_id: str, filepath: str, title: str) -> None:
    """A seed pointing at a missing file fails only at deploy time otherwise."""
    assert (_ROOT / filepath).is_file(), f"{tenant_id} seeds {filepath}, which is not in the repo"


@pytest.mark.parametrize(("tenant_id", "filepath", "title"), _TENANTS)
def test_a_question_about_the_seeded_document_reaches_the_knowledge_agent(
    tenant_id: str, filepath: str, title: str
) -> None:
    """The subject the demo seeds must route somewhere that retrieves.

    The document title stands in for the visitor's phrasing: it is the seed's
    own description of the corpus, so if the router cannot reach the general
    agent from those words, no realistic question about the corpus will either.
    """
    decision = ROUTING_POLICY.route(title)

    assert decision.chosen is IntentName.GENERAL, (
        f"{tenant_id} seeds {title!r}, which routes to {decision.chosen} rather than the "
        "general agent. Only the general agent retrieves, so this corpus is unreachable."
    )
    assert decision.outcome is RoutingOutcome.DIRECT, (
        f"{tenant_id} seeds {title!r}, which the router is unsure about "
        f"({decision.outcome}). An unsure route clarifies instead of retrieving."
    )
