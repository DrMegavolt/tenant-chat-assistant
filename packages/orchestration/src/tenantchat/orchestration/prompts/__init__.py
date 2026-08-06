"""Versioned prompt templates and the assembly that renders them (`AI-003`).

Templates are immutable, repository-owned artifacts registered in
:data:`DEFAULT_REGISTRY`; assembly validates tenant input against each
template's declared slot schema, enforces the prompt budget with an explicit
exclusion record, and returns the one :class:`AssembledPrompt` the model
adapter accepts.
"""

from tenantchat.orchestration.prompts.assembly import (
    AssemblyOutcome,
    ExcludedItem,
    ExcludedKind,
    ExcludedReason,
    HistoryTurn,
    PromptBudget,
    PromptBudgetError,
    PromptEvidence,
    assemble_prompt,
    estimate_tokens,
)
from tenantchat.orchestration.prompts.diff import (
    SegmentChange,
    SegmentChangeKind,
    SlotChange,
    SlotChangeKind,
    TemplateDiff,
    diff_templates,
)
from tenantchat.orchestration.prompts.dispatch import (
    DISPATCH_SYSTEM_REF,
    DISPATCH_SYSTEM_TEMPLATE_ID,
    DISPATCH_SYSTEM_V1,
    DISPATCH_SYSTEM_V2,
    DISPATCH_SYSTEM_V2_VERSION,
    DISPATCH_SYSTEM_V3,
    DISPATCH_SYSTEM_V3_VERSION,
    DISPATCH_SYSTEM_VERSION,
)
from tenantchat.orchestration.prompts.registry import (
    TemplateRegistry,
    TemplateRegistryError,
    TemplateSegment,
    TemplateVersion,
)
from tenantchat.orchestration.prompts.schema import (
    BindingSchema,
    PromptBindingError,
    SlotKind,
    SlotSpec,
)

# The registry the deployed runtime assembles from. A template change lands
# here as an additional version, never as an edit to a registered one.
DEFAULT_REGISTRY = TemplateRegistry()
DEFAULT_REGISTRY.register(DISPATCH_SYSTEM_V1)
DEFAULT_REGISTRY.register(DISPATCH_SYSTEM_V2)
DEFAULT_REGISTRY.register(DISPATCH_SYSTEM_V3)

# The assembly budget the runtime enforces today; `AI-002` tunes the numbers.
DEFAULT_BUDGET = PromptBudget()

__all__ = [
    "DEFAULT_BUDGET",
    "DEFAULT_REGISTRY",
    "DISPATCH_SYSTEM_REF",
    "DISPATCH_SYSTEM_TEMPLATE_ID",
    "DISPATCH_SYSTEM_V1",
    "DISPATCH_SYSTEM_V2",
    "DISPATCH_SYSTEM_V2_VERSION",
    "DISPATCH_SYSTEM_V3",
    "DISPATCH_SYSTEM_V3_VERSION",
    "DISPATCH_SYSTEM_VERSION",
    "AssemblyOutcome",
    "BindingSchema",
    "ExcludedItem",
    "ExcludedKind",
    "ExcludedReason",
    "HistoryTurn",
    "PromptBindingError",
    "PromptBudget",
    "PromptBudgetError",
    "PromptEvidence",
    "SegmentChange",
    "SegmentChangeKind",
    "SlotChange",
    "SlotChangeKind",
    "SlotKind",
    "SlotSpec",
    "TemplateDiff",
    "TemplateRegistry",
    "TemplateRegistryError",
    "TemplateSegment",
    "TemplateVersion",
    "assemble_prompt",
    "diff_templates",
    "estimate_tokens",
]
