"""The tools the assistant may call, and how their arguments are read.

The schemas here describe the *conversation's* vocabulary. They are not the
domain contract: every argument is model-written text that arrives without
having passed the API's request validation, so nothing in this module decides
whether an action is allowed. Each one is handed to a
:mod:`tenantchat.core.commands` command, which is where policy, completeness,
contact parsing, and service resolution are enforced for all three callers.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Final

from tenantchat.core.errors import ValidationError
from tenantchat.orchestration.model import ToolSpec

# A guard against a runaway generation, not a field rule. The per-field bounds
# live in `tenantchat.core.commands` and are tighter than this; the point of a
# cap here is that a megabyte of model output never reaches them at all.
MAX_ARGUMENT_CHARACTERS: Final = 4096


class ToolName(StrEnum):
    """Every tool the graph knows how to run.

    Closed so that an unrecognized name from the model is a routing decision the
    graph makes deliberately, rather than a ``KeyError`` in a node.
    """

    CHECK_SERVICE_AREA = "check_service_area"
    GET_AVAILABILITY = "get_availability"
    BOOK_APPOINTMENT = "book_appointment"
    CREATE_LEAD = "create_lead"
    HANDOFF_TO_HUMAN = "handoff_to_human"

    @classmethod
    def resolve(cls, raw: str) -> ToolName | None:
        try:
            return cls(raw)
        except ValueError:
            return None


def text_argument(arguments: Mapping[str, object], key: str) -> str:
    """Read one argument as text, tolerating a model that sent a number.

    Returns the empty string for a missing or null argument: "the model did not
    supply this" is an ordinary conversational state that the command reports as
    a missing field, not a failure of the tool call.

    Raises:
        ValidationError: the value exceeds :data:`MAX_ARGUMENT_CHARACTERS`.
    """
    value = arguments.get(key)
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    if len(text) > MAX_ARGUMENT_CHARACTERS:
        raise ValidationError(
            detail=f"tool argument {key!r} is {len(text)} characters, limit "
            f"{MAX_ARGUMENT_CHARACTERS}"
        )
    return text


def _string(description: str) -> dict[str, object]:
    return {"type": "string", "description": description}


TOOL_SPECS: Final[tuple[ToolSpec, ...]] = (
    ToolSpec(
        name=ToolName.CHECK_SERVICE_AREA.value,
        description="Check whether this company serves a five-digit US ZIP code.",
        parameters={
            "type": "object",
            "properties": {"zip": _string("A five digit US ZIP code, such as 97205.")},
            "required": ["zip"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name=ToolName.GET_AVAILABILITY.value,
        description="List bookable appointment slots for one service this company offers.",
        parameters={
            "type": "object",
            "properties": {"service": _string("A service category, for example HVAC.")},
            "required": ["service"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name=ToolName.BOOK_APPOINTMENT.value,
        description=(
            "Propose a booking for a slot returned by get_availability. The customer is "
            "asked to confirm before anything is booked, so call this as soon as every "
            "detail has been collected."
        ),
        parameters={
            "type": "object",
            "properties": {
                "service": _string("The service to book."),
                "slot": _string("A slot label exactly as get_availability returned it."),
                "customer_name": _string("The customer's name."),
                "customer_phone_or_email": _string("An email address or a 10-digit US number."),
                "address": _string("The service address."),
            },
            "required": [
                "service",
                "slot",
                "customer_name",
                "customer_phone_or_email",
                "address",
            ],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name=ToolName.CREATE_LEAD.value,
        description=(
            "Record a follow-up request so a person can call the customer back. Use this "
            "when the company does not book through chat, or when the customer asks to be "
            "contacted."
        ),
        parameters={
            "type": "object",
            "properties": {
                "customer_name": _string("The customer's name."),
                "customer_phone_or_email": _string("An email address or a 10-digit US number."),
                "service": _string("What the customer needs, in their own words if unlisted."),
                "summary": _string("A short summary of the request."),
                "address_or_zip": _string("A service address or ZIP code, if known."),
                "urgency": _string("One of emergency, today, this_week, flexible, or unknown."),
            },
            "required": ["customer_name", "customer_phone_or_email", "service", "summary"],
            "additionalProperties": False,
        },
    ),
    ToolSpec(
        name=ToolName.HANDOFF_TO_HUMAN.value,
        description=(
            "Ask a person to take over when the assistant is not permitted to answer or "
            "act, or when the customer asks for a human."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": _string(
                    "One of customer_request, outside_policy, tool_failure, or unresolved."
                ),
                "summary": _string("What the person taking over needs to know."),
            },
            "required": ["reason", "summary"],
            "additionalProperties": False,
        },
    ),
)
