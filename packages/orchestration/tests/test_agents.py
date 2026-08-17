"""The specialized agents: one per intent, with closed schemas and allowlists.

The `AGENT-001` acceptance criterion this module pins: an agent's tools are a
deterministic allowlist enforced by the registry, and no registration may
reference a tool the runtime does not know how to run. The registry is code,
so these tests double as the review of the allowlist decisions.
"""

from __future__ import annotations

from typing import cast

import pytest

from tenantchat.core.routing import IntentName
from tenantchat.orchestration.agents import (
    DEFAULT_AGENT_REGISTRY,
    AgentName,
    AgentRegistry,
    AgentRegistryError,
    AgentSpec,
    AnswerBasis,
)
from tenantchat.orchestration.tools import ToolName


def test_every_intent_has_exactly_one_agent() -> None:
    agents = {spec.intent: spec.name for spec in DEFAULT_AGENT_REGISTRY.all()}

    assert set(agents) == set(IntentName)
    assert len(agents) == len(DEFAULT_AGENT_REGISTRY.all())


def test_every_allowlisted_tool_is_a_tool_the_graph_can_run() -> None:
    """A registry entry naming an unknown tool would make the allowlist a lie."""
    for spec in DEFAULT_AGENT_REGISTRY.all():
        for tool in spec.tools:
            assert ToolName.resolve(tool.value) is tool, spec.name


def test_the_specialized_agents_carry_only_their_own_tools() -> None:
    """The acceptance criterion, as a table: no cross-agent reach."""
    allowlists = {spec.name: frozenset(spec.tool_names) for spec in DEFAULT_AGENT_REGISTRY.all()}

    assert allowlists[AgentName.SERVICE_AREA] == frozenset({"check_service_area"})
    assert allowlists[AgentName.AVAILABILITY] == frozenset({"get_availability"})
    assert allowlists[AgentName.BOOKING] == frozenset({"get_availability", "book_appointment"})
    assert allowlists[AgentName.LEAD] == frozenset({"create_lead"})
    assert allowlists[AgentName.HANDOFF] == frozenset({"handoff_to_human"})
    assert allowlists[AgentName.GENERAL] == frozenset()
    assert allowlists[AgentName.CANCEL] == frozenset()


def test_only_the_general_agent_answers_from_tenant_knowledge() -> None:
    """The evidence gate is declared per agent, and it gates only one of them.

    Every other agent is carried by its tools and its workflow record, so a
    thin retrieval pool must not stop it: an appointment does not become
    unbookable because the knowledge index is empty.
    """
    bases = {spec.intent: spec.answers_from for spec in DEFAULT_AGENT_REGISTRY.all()}
    expected = {intent: AnswerBasis.TOOL_RESULTS for intent in IntentName}
    expected[IntentName.GENERAL] = AnswerBasis.TENANT_KNOWLEDGE

    assert bases == expected


def test_only_the_collection_agents_hold_multi_turn_workflows() -> None:
    workflow_agents = {spec.intent for spec in DEFAULT_AGENT_REGISTRY.all() if spec.workflow}

    assert workflow_agents == {IntentName.BOOKING, IntentName.LEAD}


def test_the_workflow_agents_declare_input_schemas_with_distinct_fields() -> None:
    for spec in DEFAULT_AGENT_REGISTRY.all():
        if not spec.workflow:
            continue
        names = [field.name for field in spec.input_fields]
        assert len(names) == len(set(names)), spec.name
        assert names, spec.name


def test_a_registration_duplicating_an_intent_is_refused() -> None:
    registry = AgentRegistry()
    base = AgentSpec(
        name=AgentName.GENERAL,
        intent=IntentName.GENERAL,
        description="general",
        input_fields=(),
        output="prose",
        tools=(),
        answers_from=AnswerBasis.TENANT_KNOWLEDGE,
        workflow=False,
    )
    registry.register(base)
    duplicate = AgentSpec(
        name=AgentName.CANCEL,
        intent=IntentName.GENERAL,
        description="duplicate",
        input_fields=(),
        output="prose",
        tools=(),
        answers_from=AnswerBasis.TOOL_RESULTS,
        workflow=False,
    )

    with pytest.raises(AgentRegistryError, match="duplicates intent"):
        registry.register(duplicate)


def test_a_registration_naming_an_unknown_tool_is_refused() -> None:
    """An allowlist must be closed over the tools that actually exist.

    The registry accepts the spec as a ``tuple[ToolName, ...]``; this test
    casts a name the enum does not contain, which is exactly what a registry
    entry would look like if the tool enum and the allowlists drifted.
    """
    registry = AgentRegistry()
    with pytest.raises(AgentRegistryError, match="unknown tool"):
        registry.register(
            AgentSpec(
                name=AgentName.SERVICE_AREA,
                intent=IntentName.SERVICE_AREA,
                description="area",
                input_fields=(),
                output="verdict",
                tools=(
                    ToolName.CHECK_SERVICE_AREA,
                    cast(ToolName, "cancel_everything"),
                ),
                answers_from=AnswerBasis.TOOL_RESULTS,
                workflow=False,
            )
        )
