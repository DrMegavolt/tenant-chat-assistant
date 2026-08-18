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

Each run is executed through the debug stream so `OBS-006` can capture the graph
that actually ran: :class:`~tenantchat.orchestration.executed.ExecutedGraphListener`
records node entries and exits, the edges taken, per-node durations and attempts,
and node terminal status, all content-free. The listener is deliberately off the
critical section's decision path — its capture is fed after the fact, and any
listener failure is caught here so the run degrades to the derived trace view
instead of failing the turn. A graph that raises mid-run is turned into a
recorded failed turn (outcome ``failed``, diagnosis ``application_error``) whose
executed-graph section ends at the node that crashed, so the failure is
inspectable rather than a silent 500 — and never an idealized completion.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, cast

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from tenantchat.core.citations import Citation
from tenantchat.core.metrics import MetricName, MetricsReporter
from tenantchat.core.metrics import TurnOutcome as MetricOutcome
from tenantchat.orchestration.executed import ExecutedGraphListener
from tenantchat.orchestration.graph import GRAPH_VERSION, CompiledDispatchGraph
from tenantchat.orchestration.nodes import BookingDecision
from tenantchat.orchestration.prompts import DISPATCH_SYSTEM_REF
from tenantchat.orchestration.state import (
    CommittedAction,
    DispatchState,
    initial_state,
    next_turn,
)
from tenantchat.orchestration.state import (
    TurnOutcome as TurnStatus,
)
from tenantchat.orchestration.trace import build_turn_trace

logger = logging.getLogger(__name__)

# SEC-002 resolves this session ID from a signed visitor credential before the
# runtime receives it. Bounding it again here keeps an injected test adapter or
# future caller from creating an unbounded checkpoint key; the tenant prefix
# means even a collision cannot address another tenant's thread.
_SESSION_ID = re.compile(r"\A[A-Za-z0-9._:-]{1,128}\Z")

_INTERRUPT_KEY: Final = "__interrupt__"

# The server-written reply for a turn whose graph crashed mid-run. Written here
# rather than by any node, because every node is exactly what may have crashed;
# it is content-free and commits nothing, so it cannot fabricate an action or
# repeat one.
_CRASH_REPLY: Final = "I could not finish that because of an unexpected error. Please try again."

# The bounded failure code a crashed turn is attributed with (`OBS-006`). It is
# the ``application_error`` diagnosis cause, so a crashed turn reaches the
# review queue like any other detected technical failure.
_CRASH_FAILURE: Final = "application_error"


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
    ``trace`` is the `OBS-004` inference trace of the turn as JSON-safe data,
    derived from the checkpoint state after the run.
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
    trace: Mapping[str, object] | None = None

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

    def __init__(
        self, graph: CompiledDispatchGraph, *, metrics: MetricsReporter | None = None
    ) -> None:
        self._graph = graph
        self._metrics = metrics

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
        return await self._invoke(config, update, resumed=False, tenant_id=tenant_id)

    async def resume(self, tenant_id: str, session_id: str, decision: object) -> TurnResult:
        """Answer the question the graph paused on and run to completion.

        ``decision`` is read by :meth:`BookingDecision.of`, which approves only
        on an explicit approval and declines on anything else.

        Raises:
            ValueError: the identifiers are not usable as a thread key.
        """
        config = self._config(tenant_id, session_id)
        resumed = BookingDecision.of(decision) is BookingDecision.APPROVED
        return await self._invoke(
            config, Command(resume=resumed), resumed=True, tenant_id=tenant_id
        )

    async def _invoke(
        self,
        config: RunnableConfig,
        invocation: DispatchState | Command[Any] | None,
        *,
        resumed: bool,
        tenant_id: str,
    ) -> TurnResult:
        """Run one graph invocation, capturing its execution and ending it honestly.

        A run that finishes normally returns its result; a run whose graph
        raises mid-way returns a *failed* turn whose trace names the nodes that
        ran and stops — the crash is a recorded, reviewable outcome, never an
        idealized answer. A capture failure degrades to the derived trace view
        and the run's outcome is unaffected.
        """
        listener = ExecutedGraphListener(resumed=resumed)
        final_state: Mapping[str, object] = {}
        crashed = False
        failure_type = ""
        try:
            async for mode, part in self._graph.astream(
                invocation, config, stream_mode=["debug", "values"]
            ):
                if mode == "values":
                    final_state = cast("Mapping[str, object]", part)
                else:
                    try:
                        listener.on_event(part)
                    except Exception:
                        # A listener bug must not fail the turn: drop capture
                        # and keep running. The trace records no executed-graph
                        # section, and readers show the derived view — the
                        # exact pre-`OBS-006` behavior.
                        listener = ExecutedGraphListener(resumed=resumed)
        except Exception as error:
            crashed = True
            failure_type = type(error).__name__
            listener.crash()
            logger.warning(
                "dispatcher graph failed mid-turn",
                extra={"tenant_id": tenant_id, "failure": failure_type},
            )
        section = listener.to_section()
        if self._metrics is not None and section is not None:
            self._observe_executed(self._metrics, section)
        if crashed and self._metrics is not None:
            self._metrics.observe(
                MetricName.TURN_OUTCOMES,
                1,
                labels={"outcome": MetricOutcome.FAILED.value},
            )
        return self._result(final_state, section, crashed=crashed)

    @staticmethod
    def _observe_executed(metrics: MetricsReporter, section: Mapping[str, object]) -> None:
        """Per-node latency, recorded once after the run, off the critical path.

        The node-name label is the closed ``DispatchNode`` vocabulary and the
        status is the closed ``ok``/``error`` pair, so the series is bounded;
        the cardinality test in ``services/api/tests/test_metrics.py`` proves
        it. A node with no duration (it entered and never exited) is skipped.
        """
        nodes = section.get("nodes")
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            name = node.get("name")
            status = node.get("status")
            duration_ms = node.get("duration_ms")
            if (
                not isinstance(name, str)
                or not isinstance(status, str)
                or not isinstance(duration_ms, int)
            ):
                continue
            metrics.observe(
                MetricName.NODE_LATENCY,
                duration_ms / 1000.0,
                labels={"node": name, "status": status},
            )

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
    def _result(
        raw: Mapping[str, Any],
        section: Mapping[str, object] | None,
        *,
        crashed: bool,
    ) -> TurnResult:
        state = dict(raw)
        state["executed_graph"] = section
        if crashed:
            # A crashed turn's terminal state is what the last completed
            # superstep checkpointed: the graph never recorded how it ended, so
            # the runtime records it — outcome ``failed``, attributed as an
            # application error, answered with a server-written reply.
            state["turn_outcome"] = TurnStatus.FAILED.value
            state["failure"] = _CRASH_FAILURE
        interrupts = state.get(_INTERRUPT_KEY) or ()
        pending = next(
            (entry.value for entry in interrupts if isinstance(entry.value, Mapping)), None
        )
        citations = state.get("citations", ())
        retrieval = state.get("evidence_meta") or None
        claims_invalid = tuple(
            (str(item["kind"]), str(item["value"]))
            for item in state.get("claims_invalid", ())
            if isinstance(item, Mapping)
        )
        return TurnResult(
            answer=_CRASH_REPLY if crashed else str(state.get("answer", "")),
            committed=tuple(state.get("committed", ())),
            pending=dict(pending) if pending is not None else None,
            model_name=str(state.get("model_name", "")),
            citations=tuple(_citation(item) for item in citations),
            citation_invalid=tuple(str(item) for item in state.get("citation_invalid", ())),
            refused_tools=tuple(str(item) for item in state.get("refused_tools", ())),
            claims_invalid=claims_invalid,
            retrieval=dict(retrieval) if retrieval is not None else None,
            trace=build_turn_trace(state, pending=dict(pending) if pending is not None else None),
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
