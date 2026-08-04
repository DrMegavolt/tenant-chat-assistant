"""The model call, reduced to what a graph node needs.

Deliberately smaller than any provider's API. A node needs to send a
conversation and a tool list and get back either prose or tool calls; anything
else — streaming, usage accounting, retries, provider selection — belongs to
`AI-001`, which implements this port. Keeping the port this narrow is what lets
the graph be tested against a scripted model with no network, no key, and no
recorded fixtures.

Tool-call arguments arrive **already parsed**. Providers disagree about whether
they emit an object or a JSON string, and a graph that has to know is a graph
that has a provider baked into it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class MessageRole(StrEnum):
    """Who produced a message in the model-facing transcript.

    Distinct from :class:`tenantchat.api.store.MessageRole`, which is the
    persisted business record. This one exists to satisfy a model's chat format
    and has a ``TOOL`` entry that a transcript shown to a human does not need.
    """

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool offered to the model, with a JSON Schema for its arguments."""

    name: str
    description: str
    parameters: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A model's request to run one tool.

    ``call_id`` is the provider's correlation ID. The graph reuses it as the
    distinguishing part of the action's idempotency key, so it must identify this
    call within the conversation and must not be regenerated on replay.
    """

    call_id: str
    name: str
    arguments: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class ModelMessage:
    """One entry in the conversation sent to the model."""

    role: MessageRole
    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    # Set on TOOL messages only: which call this is the result of.
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What one model call produced.

    ``content`` and ``tool_calls`` are not exclusive — a provider may return both
    — but a response carrying neither is a failed turn, not an empty answer, and
    the graph treats it as one.
    """

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    # Recorded against the turn so a behavior change is attributable (`OBS-004`).
    model_name: str = "unknown"
    usage: Mapping[str, int] = field(default_factory=dict)


class ChatModel(Protocol):
    """A chat-completion endpoint that can call tools."""

    async def complete(
        self,
        messages: Sequence[ModelMessage],
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        """Continue the conversation, optionally by calling tools.

        Raises:
            Exception: any provider failure. The graph converts an unhandled
                failure into a handoff rather than retrying, because `REL-001`
                owns retry and backoff for dependency clients.
        """
        ...
