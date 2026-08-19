"""The specialized agents, registered with their schemas and tool allowlists.

`AGENT-001` replaces "one dispatcher that may call everything" with a set of
specialized agents, one per intent. Each registration is code, reviewed like
any other security policy: the agent's input fields, its output, and — the part
the model cannot grant itself — the closed set of tools it may call. The tools
node enforces the allowlist deterministically, and the model is only ever
*offered* the allowed tools in the first place.

Like the prompt registry, agents are versioned and append-only: the version
string is recorded against every routing decision and workflow, so `OBS-004`
can pin which agent definition produced a turn.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tenantchat.core.routing import IntentName
from tenantchat.orchestration.tools import ToolName


class AgentName(StrEnum):
    """One registered agent per intent; the enum is closed with the intents."""

    GENERAL = "general"
    SERVICE_AREA = "service_area"
    AVAILABILITY = "availability"
    BOOKING = "booking"
    LEAD = "lead"
    HANDOFF = "handoff"
    CANCEL = "cancel"


class AnswerBasis(StrEnum):
    """What an agent's answers rest on, and so what thin retrieval costs it.

    ``TENANT_KNOWLEDGE`` is the tenant's own material, which has two sources:
    the server-owned business facts bound into the prompt — hours, phone,
    address, approved prices — and the approved documents retrieval supplies.
    An agent declaring it answers from those two and nothing else, so a
    question that neither covers is one it must refuse rather than improvise
    (`RAG-005`).

    ``TOOL_RESULTS`` is an agent carried by its tools and its durable workflow
    record. The knowledge base is beside the point for it: an appointment does
    not become unbookable because the index is empty.
    """

    TENANT_KNOWLEDGE = "tenant_knowledge"
    TOOL_RESULTS = "tool_results"


@dataclass(frozen=True, slots=True)
class WorkflowField:
    """One field a workflow agent collects across turns.

    The names are the input schema of the agent: the tools node persists model
    arguments under exactly these names and nothing else, so a field the
    registry does not declare can never reach the durable workflow record.
    """

    name: str
    description: str


@dataclass(frozen=True, slots=True)
class AgentSpec:
    """One specialized agent: its intent, schemas, and tool allowlist."""

    name: AgentName
    intent: IntentName
    description: str
    input_fields: tuple[WorkflowField, ...]
    output: str
    tools: tuple[ToolName, ...]
    answers_from: AnswerBasis
    # Whether the agent holds multi-turn state: only collection workflows do.
    workflow: bool

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.value for tool in self.tools)


class AgentRegistryError(ValueError):
    """The registry refused a registration."""


class AgentRegistry:
    """The closed set of specialized agents, one per intent.

    Registration is code and runs once at import: a registration conflict — two
    agents for one intent, an unknown tool in an allowlist, a duplicate field —
    is a loud import-time failure rather than a routing-time surprise.
    """

    def __init__(self) -> None:
        self._by_name: dict[AgentName, AgentSpec] = {}
        self._by_intent: dict[IntentName, AgentSpec] = {}

    def register(self, spec: AgentSpec) -> None:
        if spec.name in self._by_name:
            raise AgentRegistryError(f"agent {spec.name.value} is already registered")
        existing = self._by_intent.get(spec.intent)
        if existing is not None:
            raise AgentRegistryError(
                f"agent {spec.name.value} duplicates intent {spec.intent.value} "
                f"of {existing.name.value}"
            )
        for tool in spec.tools:
            if not isinstance(tool, ToolName):
                raise AgentRegistryError(
                    f"agent {spec.name.value} allowlist names unknown tool {tool!r}"
                )
        field_names = {field.name for field in spec.input_fields}
        if len(field_names) != len(spec.input_fields):
            raise AgentRegistryError(f"agent {spec.name.value} has duplicate input fields")
        self._by_name[spec.name] = spec
        self._by_intent[spec.intent] = spec

    def current(self, name: AgentName) -> AgentSpec:
        """The registered agent with that name.

        Raises:
            AgentRegistryError: no such agent.
        """
        spec = self._by_name.get(name)
        if spec is None:
            raise AgentRegistryError(f"no agent registered as {name.value!r}")
        return spec

    def for_intent(self, intent: IntentName) -> AgentSpec | None:
        """The agent that handles an intent, or ``None`` if no agent does."""
        return self._by_intent.get(intent)

    def all(self) -> tuple[AgentSpec, ...]:
        return tuple(spec for spec in self._by_name.values())


AGENTS_VERSION: Final = "agents@1"

BOOKING_FIELDS = (
    WorkflowField("service", "The service category to book."),
    WorkflowField("slot", "The slot label exactly as get_availability returned it."),
    WorkflowField("customer_name", "The customer's name."),
    WorkflowField("customer_phone_or_email", "An email address or 10-digit US number."),
    WorkflowField("address", "The service address."),
)
LEAD_FIELDS = (
    WorkflowField("customer_name", "The customer's name."),
    WorkflowField("customer_phone_or_email", "An email address or 10-digit US number."),
    WorkflowField("service", "What the customer needs."),
    WorkflowField("summary", "A short summary of the request."),
    WorkflowField("address_or_zip", "A service address or ZIP code, if known."),
    WorkflowField("urgency", "One of emergency, today, this_week, flexible, or unknown."),
)

DEFAULT_AGENT_REGISTRY = AgentRegistry()
DEFAULT_AGENT_REGISTRY.register(
    AgentSpec(
        name=AgentName.GENERAL,
        intent=IntentName.GENERAL,
        description=(
            "General chat: business facts, hours, pricing from the approved list, and "
            "anything that needs no tool."
        ),
        input_fields=(),
        output="A direct answer from tenant policy, with no tool effect.",
        tools=(),
        answers_from=AnswerBasis.TENANT_KNOWLEDGE,
        workflow=False,
    )
)
DEFAULT_AGENT_REGISTRY.register(
    AgentSpec(
        name=AgentName.SERVICE_AREA,
        intent=IntentName.SERVICE_AREA,
        description="Service-area questions: whether the company serves a ZIP code.",
        input_fields=(),
        output="A served or not-served verdict for one ZIP code.",
        tools=(ToolName.CHECK_SERVICE_AREA,),
        answers_from=AnswerBasis.TOOL_RESULTS,
        workflow=False,
    )
)
DEFAULT_AGENT_REGISTRY.register(
    AgentSpec(
        name=AgentName.AVAILABILITY,
        intent=IntentName.AVAILABILITY,
        description="Appointment availability: which slots exist for one service.",
        input_fields=(),
        output="The offered slots for one service, or a policy refusal.",
        tools=(ToolName.GET_AVAILABILITY,),
        answers_from=AnswerBasis.TOOL_RESULTS,
        workflow=False,
    )
)
DEFAULT_AGENT_REGISTRY.register(
    AgentSpec(
        name=AgentName.BOOKING,
        intent=IntentName.BOOKING,
        description=(
            "Booking an appointment: collects the service, slot, name, contact, and "
            "address, then proposes the booking for the customer's confirmation."
        ),
        input_fields=BOOKING_FIELDS,
        output="A confirmed booking, or a declined booking reported back.",
        tools=(ToolName.GET_AVAILABILITY, ToolName.BOOK_APPOINTMENT),
        answers_from=AnswerBasis.TOOL_RESULTS,
        workflow=True,
    )
)
DEFAULT_AGENT_REGISTRY.register(
    AgentSpec(
        name=AgentName.LEAD,
        intent=IntentName.LEAD,
        description=(
            "Follow-up requests: collects name, contact, service, and summary so a "
            "person can call the customer back."
        ),
        input_fields=LEAD_FIELDS,
        output="A captured lead with a reference, or a missing-fields question.",
        tools=(ToolName.CREATE_LEAD,),
        answers_from=AnswerBasis.TOOL_RESULTS,
        workflow=True,
    )
)
DEFAULT_AGENT_REGISTRY.register(
    AgentSpec(
        name=AgentName.HANDOFF,
        intent=IntentName.HANDOFF,
        description="Human handoff: the customer asked for a person.",
        input_fields=(),
        output="A handoff ticket and the phone number to call meanwhile.",
        tools=(ToolName.HANDOFF_TO_HUMAN,),
        answers_from=AnswerBasis.TOOL_RESULTS,
        workflow=False,
    )
)
DEFAULT_AGENT_REGISTRY.register(
    AgentSpec(
        name=AgentName.CANCEL,
        intent=IntentName.CANCEL,
        description="Cancellation: abandons the active workflow, then answers in prose.",
        input_fields=(),
        output="A cancelled workflow, or nothing when no workflow was active.",
        tools=(),
        answers_from=AnswerBasis.TOOL_RESULTS,
        workflow=False,
    )
)
