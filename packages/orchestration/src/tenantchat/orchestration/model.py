"""The model call, reduced to what a graph node needs.

Deliberately smaller than any provider's API. A node sends an assembled prompt
and a tool list and gets back prose or tool calls, plus any usage counters the
provider returned. Retries and provider selection wrap this port. Response
streaming is not implemented and remains in `FEAT-010`. Keeping the port this
narrow lets the graph run against a scripted model with no network, key, or
recorded fixtures.

The port's input is the **assembled prompt** (`AI-003`): a closed, versioned
value carrying its template ID and version, its resolved bindings, and its
content hash, with every message decomposed into trust-marked segments. A raw
string conversation cannot reach a provider, because nothing a provider accepts
is typed as one.

Tool-call arguments arrive **already parsed**. Providers disagree about whether
they emit an object or a JSON string, and a graph that has to know is a graph
that has a provider baked into it.
"""

from __future__ import annotations

import hashlib
import json
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


class PromptRegion(StrEnum):
    """Whether a segment's content is server-authored or visitor/external.

    `RAG-007` enforces a single boundary between the two, so the marking must
    live on the assembled type rather than in a convention: everything the
    visitor wrote and everything retrieved from tenant knowledge is
    ``UNTRUSTED`` by construction, and everything the server wrote is
    ``TRUSTED``. Assembly never promotes untrusted content into a trusted
    segment.
    """

    TRUSTED = "trusted"
    UNTRUSTED = "untrusted"


@dataclass(frozen=True, slots=True)
class PromptSegment:
    """One region of one message, marked trusted or untrusted.

    ``segment_id`` is stable within a template version so `FEAT-015` can track
    a segment across versions, and ``text`` is the fully resolved content —
    template text, a slot value, an evidence passage, or a visitor turn.
    """

    segment_id: str
    region: PromptRegion
    text: str


@dataclass(frozen=True, slots=True)
class AssembledMessage:
    """One entry in the assembled prompt, decomposed into marked segments.

    ``content`` is derived from the segments, so no path can change the text a
    provider receives without changing the segments `RAG-007` inspects.
    """

    role: MessageRole
    segments: tuple[PromptSegment, ...]
    tool_calls: tuple[ToolCall, ...] = ()
    # Set on TOOL messages only: which call this is the result of.
    tool_call_id: str | None = None

    @property
    def content(self) -> str:
        return "".join(segment.text for segment in self.segments)


@dataclass(frozen=True, slots=True)
class AssembledPrompt:
    """The complete, versioned input to one model call.

    Carries the template ID and version the call is attributable to, the
    resolved slot bindings, and a content hash over the resolved content, so
    `OBS-004` can pin a model call to the exact artifact that produced it and
    the exact bytes the provider received.
    """

    template_id: str
    template_version: int
    bindings: Mapping[str, str]
    messages: tuple[AssembledMessage, ...]

    @property
    def template_ref(self) -> str:
        return f"{self.template_id}@{self.template_version}"

    @property
    def segments(self) -> tuple[PromptSegment, ...]:
        """Every segment of every message, in message order."""
        return tuple(segment for message in self.messages for segment in message.segments)

    @property
    def content_hash(self) -> str:
        """SHA-256 over the template ref and every segment, byte for byte.

        Deterministic — equal inputs produce equal hashes — and covers the
        tool-call shape as well as the text, because both are what the provider
        receives. Bindings are covered through the segment text: every declared
        slot is rendered into it.
        """
        canonical = {
            "template": self.template_ref,
            "messages": [
                {
                    "role": message.role.value,
                    "tool_call_id": message.tool_call_id,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "name": call.name,
                            "arguments": dict(call.arguments),
                        }
                        for call in message.tool_calls
                    ],
                    "segments": [
                        [segment.segment_id, segment.region.value, segment.text]
                        for segment in message.segments
                    ],
                }
                for message in self.messages
            ],
        }
        encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class FallbackHop:
    """One failed attempt the fallback chain made before an answer (R-38).

    ``model_name`` is the configured identifier of the model that was tried
    and ``reason`` the bounded outage classification — the same vocabulary the
    ``MODEL_FALLBACKS`` metric uses. Never carries failure text: an exception
    message is provider output and stays out of the record.
    """

    model_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What one model call produced.

    ``content`` and ``tool_calls`` are not exclusive — a provider may return both
    — but a response carrying neither is a failed turn, not an empty answer, and
    the graph treats it as one.

    The two provenance flags keep attribution honest (`OBS-004`): a
    cache-served response must not read as a fresh completion, and a response
    that arrived only after earlier models in the chain failed must show the
    hops. Both default to "fresh, first try" so a plain provider cannot
    misattribute itself.
    """

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    # Recorded against the turn so a behavior change is attributable (`OBS-004`).
    model_name: str = "unknown"
    usage: Mapping[str, int] = field(default_factory=dict)
    cache_hit: bool = False
    fallback_hops: tuple[FallbackHop, ...] = ()


class ChatModel(Protocol):
    """A chat-completion endpoint that can call tools."""

    async def complete(
        self,
        prompt: AssembledPrompt,
        *,
        tools: Sequence[ToolSpec],
    ) -> ModelResponse:
        """Complete the conversation from one assembled, versioned prompt.

        The assembled prompt is the only input: every call is attributable to
        the template ID and version it carries, and no code path can reach a
        provider with a prompt the registry did not assemble.

        Raises:
            Exception: any provider failure. The graph converts an unhandled
                failure into a handoff rather than retrying, because `REL-001`
                owns retry and backoff for dependency clients.
        """
        ...
