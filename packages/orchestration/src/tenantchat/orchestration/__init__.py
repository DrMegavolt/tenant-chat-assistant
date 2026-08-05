"""LangGraph agent runtime for the dispatcher assistant.

The single agent runtime, per `ADR-0001`. LangGraph is a hard dependency here
and is banned from ``packages/core``, application-service public contracts, API
schemas, and business repository adapters — a dependency *direction*, not a
repository-wide import ban.

Nothing in this package decides whether an action is permitted or writes a
record. It decides what to attempt and in what order, and causes every effect
through a :mod:`tenantchat.core.ports` service that takes an idempotency key,
because a node runs again whenever a run resumes.
"""

from tenantchat.orchestration.checkpoints import (
    InMemorySaver,
    checkpoint_connection_string,
    postgres_checkpointer,
)
from tenantchat.orchestration.dependencies import DispatchDependencies
from tenantchat.orchestration.graph import (
    GRAPH_VERSION,
    build_dispatch_graph,
    compile_dispatch_graph,
)
from tenantchat.orchestration.model import (
    AssembledMessage,
    AssembledPrompt,
    ChatModel,
    MessageRole,
    ModelResponse,
    PromptRegion,
    PromptSegment,
    ToolCall,
    ToolSpec,
)
from tenantchat.orchestration.nodes import MAX_TOOL_ROUNDS, DispatchNode
from tenantchat.orchestration.prompts import (
    DISPATCH_SYSTEM_REF,
    DISPATCH_SYSTEM_TEMPLATE_ID,
    DISPATCH_SYSTEM_VERSION,
    AssemblyOutcome,
    ExcludedItem,
    ExcludedKind,
    ExcludedReason,
    HistoryTurn,
    PromptBindingError,
    PromptBudget,
    PromptBudgetError,
    PromptEvidence,
    TemplateDiff,
    TemplateRegistry,
    TemplateRegistryError,
    TemplateSegment,
    TemplateVersion,
    assemble_prompt,
    diff_templates,
    estimate_tokens,
)
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel
from tenantchat.orchestration.runtime import DispatchRuntime, TurnResult, thread_id
from tenantchat.orchestration.state import CommittedAction, DispatchState
from tenantchat.orchestration.tools import TOOL_SPECS, ToolName

__all__ = [
    "DISPATCH_SYSTEM_REF",
    "DISPATCH_SYSTEM_TEMPLATE_ID",
    "DISPATCH_SYSTEM_VERSION",
    "GRAPH_VERSION",
    "MAX_TOOL_ROUNDS",
    "TOOL_SPECS",
    "AssembledMessage",
    "AssembledPrompt",
    "AssemblyOutcome",
    "ChatModel",
    "CommittedAction",
    "DispatchDependencies",
    "DispatchNode",
    "DispatchRuntime",
    "DispatchState",
    "ExcludedItem",
    "ExcludedKind",
    "ExcludedReason",
    "HistoryTurn",
    "InMemorySaver",
    "MessageRole",
    "ModelResponse",
    "OpenAICompatibleChatModel",
    "PromptBindingError",
    "PromptBudget",
    "PromptBudgetError",
    "PromptEvidence",
    "PromptRegion",
    "PromptSegment",
    "TemplateDiff",
    "TemplateRegistry",
    "TemplateRegistryError",
    "TemplateSegment",
    "TemplateVersion",
    "ToolCall",
    "ToolName",
    "ToolSpec",
    "TurnResult",
    "assemble_prompt",
    "build_dispatch_graph",
    "checkpoint_connection_string",
    "compile_dispatch_graph",
    "diff_templates",
    "estimate_tokens",
    "postgres_checkpointer",
    "thread_id",
]
