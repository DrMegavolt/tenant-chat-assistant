"""The runtime the rest of the system talks to.

Three operations — send a message, resume a paused one, read what is pending —
over a compiled graph and a checkpointer. Callers get :class:`TurnResult`, which
is a plain value object: no LangGraph type crosses this boundary, so the HTTP
layer that `API-001` builds on top can be written and tested without importing
the framework.

This is not an abstraction over agent frameworks. `ADR-0001` rejected that
explicitly, and swapping LangGraph out would mean rewriting this module rather
than implementing a second adapter behind it. What it is, is the place where a
graph run turns into an answer, a list of things that were committed, and the
component versions `OBS-004` needs in order to attribute either.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from tenantchat.core.citations import Citation
from tenantchat.orchestration.graph import GRAPH_VERSION, CompiledDispatchGraph
from tenantchat.orchestration.nodes import BookingDecision
from tenantchat.orchestration.prompts import DISPATCH_SYSTEM_REF
from tenantchat.orchestration.state import CommittedAction, DispatchState, initial_state, next_turn

# The visitor controls the session ID today (`SEC-002` replaces it with a
# server-issued credential). Bounding it here means a hostile value cannot become
# an unbounded checkpoint key, and the tenant prefix means it cannot address
# another tenant's thread even if it collides.
_SESSION_ID = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

_INTERRUPT_KEY: Final = "__interrupt__"


@dataclass(frozen=True, slots=True)
class TurnResult:
    """What one turn produced.

    ``pending`` is set when the run stopped at an interrupt: it is the payload
    the graph wants answered, and its presence means ``answer`` is not final.
    ``committed`` lists the domain actions this thread has caused so far, so a
    caller can report a booking reference without querying for it.

    ``citations`` are the verified sources the answer was grounded in, already
    curated for the public client; ``citation_invalid`` names the markers the
    model wrote that were *not* in its context, for the inference plane only.
    ``retrieval`` is the retrieval that ran for this turn (verdict and
    versions), or ``None`` when this deployment composed no retrieval.
    """

    answer: str
    committed: tuple[CommittedAction, ...]
    pending: Mapping[str, object] | None
    model_name: str
    graph_version: str = GRAPH_VERSION
    # The template the model calls in this turn were assembled from; the
    # registry guarantees a stored reference keeps naming the same artifact.
    prompt_version: str = DISPATCH_SYSTEM_REF
    citations: tuple[Citation, ...] = ()
    citation_invalid: tuple[str, ...] = ()
    # `RAG-007` enforcement record: the refusal codes of tool calls the
    # permission guard stopped, and the sensitive claims (kind and value) that
    # failed deterministic validation and got the answer refused. For the
    # inference plane, not the public client.
    refused_tools: tuple[str, ...] = ()
    claims_invalid: tuple[tuple[str, str], ...] = ()
    retrieval: Mapping[str, object] | None = None

    @property
    def is_paused(self) -> bool:
        return self.pending is not None


def thread_id(tenant_id: str, session_id: str) -> str:
    """The checkpoint key for one conversation.

    Tenant-qualified, and that is a security property rather than a naming
    convention: without the prefix, a session ID guessed or replayed from one
    tenant would resume another tenant's conversation, complete with the
    customer details still in its state.

    Raises:
        ValueError: either identifier is empty or contains characters outside
            ``[A-Za-z0-9._:-]``.
    """
    for label, value in (("tenant", tenant_id), ("session", session_id)):
        if not _SESSION_ID.fullmatch(value):
            raise ValueError(f"{label} identifier is not 1-128 safe characters")
    return f"{tenant_id}:{session_id}"


class DispatchRuntime:
    """Runs dispatcher conversations against a checkpointed graph."""

    def __init__(self, graph: CompiledDispatchGraph) -> None:
        self._graph = graph

    async def send(self, tenant_id: str, session_id: str, message: str) -> TurnResult:
        """Deliver a visitor message and run until an answer or an interrupt.

        Starts a new thread or continues an existing one; the checkpointer is
        what decides which, so a caller never has to track whether a
        conversation has been seen before.

        Raises:
            ValueError: the identifiers are not usable as a thread key.
        """
        config = self._config(tenant_id, session_id)
        existing = await self._graph.aget_state(config)
        # A partial update is how LangGraph adds to a thread that already
        # exists; its input type names the whole state because a *new* thread
        # has to supply all of it.
        update = (
            cast("DispatchState", next_turn(message))
            if existing.created_at is not None
            else initial_state(tenant_id, session_id, message)
        )
        return self._result(await self._graph.ainvoke(update, config))

    async def resume(self, tenant_id: str, session_id: str, decision: object) -> TurnResult:
        """Answer the question the graph paused on and run to completion.

        ``decision`` is read by :meth:`BookingDecision.of`, which approves only
        on an explicit approval and declines on anything else.

        Raises:
            ValueError: the identifiers are not usable as a thread key.
        """
        config = self._config(tenant_id, session_id)
        resumed = BookingDecision.of(decision) is BookingDecision.APPROVED
        return self._result(await self._graph.ainvoke(Command(resume=resumed), config))

    async def pending(self, tenant_id: str, session_id: str) -> Mapping[str, object] | None:
        """The interrupt this conversation is waiting on, if any.

        Lets a returning visitor be shown the confirmation they abandoned,
        rather than a conversation that appears to have stopped mid-sentence.
        """
        state = await self._graph.aget_state(self._config(tenant_id, session_id))
        for task in state.tasks:
            for entry in task.interrupts:
                if isinstance(entry.value, Mapping):
                    return dict(entry.value)
        return None

    async def snapshot(self, tenant_id: str, session_id: str) -> Mapping[str, object]:
        """The checkpointed execution state for one conversation, as plain data.

        For inspection — an operator asking what a stuck conversation is holding,
        or a test asserting what a resumed process would pick up. It is a copy,
        and writing to it changes nothing; the way to advance a conversation is
        :meth:`send` or :meth:`resume`.
        """
        state = await self._graph.aget_state(self._config(tenant_id, session_id))
        return dict(state.values)

    @staticmethod
    def _config(tenant_id: str, session_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id(tenant_id, session_id)}}

    @staticmethod
    def _result(raw: Mapping[str, Any]) -> TurnResult:
        interrupts = raw.get(_INTERRUPT_KEY) or ()
        pending = next(
            (entry.value for entry in interrupts if isinstance(entry.value, Mapping)), None
        )
        citations = raw.get("citations", ())
        retrieval = raw.get("evidence_meta") or None
        claims_invalid = tuple(
            (str(item["kind"]), str(item["value"]))
            for item in raw.get("claims_invalid", ())
            if isinstance(item, Mapping)
        )
        return TurnResult(
            answer=str(raw.get("answer", "")),
            committed=tuple(raw.get("committed", ())),
            pending=dict(pending) if pending is not None else None,
            model_name=str(raw.get("model_name", "")),
            citations=tuple(_citation(item) for item in citations),
            citation_invalid=tuple(str(item) for item in raw.get("citation_invalid", ())),
            refused_tools=tuple(str(item) for item in raw.get("refused_tools", ())),
            claims_invalid=claims_invalid,
            retrieval=dict(retrieval) if retrieval is not None else None,
        )


def _citation(item: Any) -> Citation:
    """One verified citation from the checkpoint's JSON-safe form.

    ``effective_at`` round-trips through ISO-8601, the only datetime form the
    checkpoint stores; anything else would fail loudly here rather than publish
    a citation with a wrong version window.
    """
    return Citation(
        source_id=str(item["source_id"]),
        title=str(item["title"]),
        source_name=str(item["source_name"]),
        location=str(item["location"]),
        revision=int(item["revision"]),
        effective_at=datetime.fromisoformat(str(item["effective_at"])),
    )
