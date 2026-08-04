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
    ChatModel,
    MessageRole,
    ModelMessage,
    ModelResponse,
    ToolCall,
    ToolSpec,
)
from tenantchat.orchestration.nodes import MAX_TOOL_ROUNDS, DispatchNode
from tenantchat.orchestration.prompts import SYSTEM_PROMPT_VERSION, build_system_prompt
from tenantchat.orchestration.providers.openai_compatible import OpenAICompatibleChatModel
from tenantchat.orchestration.runtime import DispatchRuntime, TurnResult, thread_id
from tenantchat.orchestration.state import CommittedAction, DispatchState
from tenantchat.orchestration.tools import TOOL_SPECS, ToolName

__all__ = [
    "GRAPH_VERSION",
    "MAX_TOOL_ROUNDS",
    "SYSTEM_PROMPT_VERSION",
    "TOOL_SPECS",
    "ChatModel",
    "CommittedAction",
    "DispatchDependencies",
    "DispatchNode",
    "DispatchRuntime",
    "DispatchState",
    "InMemorySaver",
    "MessageRole",
    "ModelMessage",
    "ModelResponse",
    "OpenAICompatibleChatModel",
    "ToolCall",
    "ToolName",
    "ToolSpec",
    "TurnResult",
    "build_dispatch_graph",
    "build_system_prompt",
    "checkpoint_connection_string",
    "compile_dispatch_graph",
    "postgres_checkpointer",
    "thread_id",
]
