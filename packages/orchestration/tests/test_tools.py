"""The boundary between model-written text and a domain command."""

from __future__ import annotations

import pytest

from tenantchat.core.errors import ValidationError
from tenantchat.orchestration.tools import (
    MAX_ARGUMENT_CHARACTERS,
    TOOL_SPECS,
    ToolName,
    text_argument,
)


def test_a_missing_argument_reads_as_empty_rather_than_raising() -> None:
    """ "The model did not say" is a question to ask, not a failure to report."""
    assert text_argument({}, "customer_name") == ""
    assert text_argument({"customer_name": None}, "customer_name") == ""


def test_a_non_string_argument_is_coerced() -> None:
    """Providers do emit a ZIP code as a number; the domain wants text."""
    assert text_argument({"zip": 97205}, "zip") == "97205"


def test_a_runaway_argument_is_refused_before_the_domain_sees_it() -> None:
    """A cap here means a megabyte of generation never reaches a field bound."""
    with pytest.raises(ValidationError):
        text_argument({"summary": "x" * (MAX_ARGUMENT_CHARACTERS + 1)}, "summary")


def test_an_argument_at_the_cap_is_accepted() -> None:
    assert len(text_argument({"summary": "x" * MAX_ARGUMENT_CHARACTERS}, "summary")) == (
        MAX_ARGUMENT_CHARACTERS
    )


def test_an_unknown_tool_name_resolves_to_none() -> None:
    """A model can invent a name; resolving must be a decision, not a KeyError."""
    assert ToolName.resolve("cancel_everything") is None
    assert ToolName.resolve("create_lead") is ToolName.CREATE_LEAD


def test_every_tool_the_graph_knows_is_offered_to_the_model() -> None:
    """A tool the graph can run but never offers is dead code with a handler."""
    assert {spec.name for spec in TOOL_SPECS} == {member.value for member in ToolName}


def test_every_schema_refuses_arguments_it_did_not_declare() -> None:
    """``additionalProperties: false`` is what keeps a tool's surface reviewable."""
    for spec in TOOL_SPECS:
        assert spec.parameters["additionalProperties"] is False, spec.name
        required = spec.parameters["required"]
        properties = spec.parameters["properties"]
        assert isinstance(required, list)
        assert isinstance(properties, dict)
        assert set(required) <= set(properties), spec.name
