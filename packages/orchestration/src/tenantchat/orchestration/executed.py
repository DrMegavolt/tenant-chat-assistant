"""Captured executed-graph events: the graph that actually ran, not a
reconstruction inferred from its final state.

`OBS-004` documented LangGraph node/edge capture as the instrumentation
follow-up, and `FEAT-015` shipped the honest interim (a derived waterfall over
stored trace fields). This module is the follow-up: :class:`ExecutedGraphListener`
consumes the debug stream of one run and records the events themselves — node
entries and exits, the edge that scheduled each node, per-node durations and
attempt numbers, and each node's terminal status.

**Content-free by construction.** The debug stream carries the full state on
every event (``task.input`` and ``task_result.result``), and this listener reads
neither: an event records the node name (a closed vocabulary from
``graph.py``/``nodes.py``), the edge label, two timestamps, and statuses. No
argument, message, or evidence text can enter an event because no such text is
ever read. The events are execution metadata and belong beside the trace, not
in the operational plane (`ADR-0010`).

**A listener failure must never fail the turn.** :mod:`tenantchat.orchestration.runtime`
feeds this listener every debug event and catches anything it raises, degrading
to the pre-`OBS-006` derived view (no executed-graph section in the trace). The
trace's own contract — a pure function over checkpointed state, no I/O, no
raise — survives because this module lives on the run path, never inside
:func:`tenantchat.orchestration.trace.build_turn_trace`.

**A run that crashes mid-graph stops.** The crashed node's entry was seen but
its exit never arrives; :meth:`ExecutedGraphListener.crash` closes it as
``error`` and no later node is invented, so the section never displays an
idealized completion. A resumed run is marked ``resumed`` and its first node
carries ``replayed`` — the node the interrupt paused and re-executed — so a
checkpoint resume is visibly distinct from a first run.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

from tenantchat.orchestration.nodes import DispatchNode

# The framework's own nodes. They are part of the execution (the START->route
# edge and the final edge to END are real events) but not part of the graph's
# closed vocabulary, so they never become rows in the executed-graph section;
# their edges are recorded on the adjacent real nodes instead.
_FRAMEWORK_NODES: Final[frozenset[str]] = frozenset({"__start__", "__end__"})


class NodeStatus(StrEnum):
    """How one node execution ended.

    A closed value so the executed-graph section stays safe as a metric
    dimension. ``INTERRUPTED`` nodes are still ``OK``: the node completed its
    body and paused for the customer, which is a normal booking-confirmation
    stop rather than a failure.
    """

    OK = "ok"
    ERROR = "error"


@dataclass(slots=True)
class _NodeRun:
    """One node execution, as the events recorded it."""

    name: str
    attempt: int
    edge: str | None
    source: str | None
    started_at: str | None
    replayed: bool = False
    ended_at: str | None = None
    status: str = NodeStatus.OK.value
    interrupted: bool = False


class ExecutedGraphListener:
    """One run's executed-graph capture, fed the debug stream event by event.

    The runtime iterates ``astream(..., stream_mode=["debug", "values"])`` and
    calls :meth:`on_event` for every debug part; :meth:`to_section` returns the
    content-free executed-graph section the trace stores, or ``None`` when the
    run produced no events. :meth:`crash` marks the run as having aborted
    mid-graph, closing any node that entered and never exited as ``error``.
    """

    def __init__(self, *, resumed: bool = False) -> None:
        self._resumed = resumed
        self._by_task: dict[str, _NodeRun] = {}
        self._order: list[str] = []
        self._attempts: dict[str, int] = {}
        self._started_at: str | None = None
        self._ended_at: str | None = None
        self._last_completed: str | None = None

    def on_event(self, part: object) -> None:
        """Record one debug stream part, content-free.

        Only ``task`` and ``task_result`` parts carry node transitions; the
        ``checkpoint`` parts are step bookkeeping and are ignored.
        """
        if not isinstance(part, dict):
            return
        kind = part.get("type")
        payload = part.get("payload")
        if not isinstance(payload, dict):
            return
        timestamp = part.get("timestamp")
        if kind == "task":
            self._start(str(timestamp) if timestamp is not None else None, payload)
        elif kind == "task_result":
            self._finish(str(timestamp) if timestamp is not None else None, payload)

    def crash(self) -> None:
        """Mark the run as aborted, closing the open node (if any) as error.

        Called by the runtime when the stream raises: the crashed node's entry
        was recorded but its exit never arrives, and the section must show it
        as the last thing that ran rather than pretend the graph completed.
        """
        for task_id in self._order:
            run = self._by_task[task_id]
            if run.ended_at is None:
                run.status = NodeStatus.ERROR.value

    def to_section(self) -> dict[str, object] | None:
        """The executed-graph section, or ``None`` when nothing was captured.

        Content-free: node names come from the closed ``DispatchNode``
        vocabulary, edge labels from the stream's trigger names, and timestamps
        are the only free values. No input, output, or write is ever kept.
        """
        if not self._order:
            return None
        nodes: list[dict[str, object]] = []
        edges: list[dict[str, object]] = []
        for task_id in self._order:
            run = self._by_task[task_id]
            nodes.append(
                {
                    "name": run.name,
                    "attempt": run.attempt,
                    "edge": run.edge,
                    "status": run.status,
                    "interrupted": run.interrupted,
                    "replayed": run.replayed,
                    "started_at": run.started_at,
                    "ended_at": run.ended_at,
                    "duration_ms": _milliseconds(run.started_at, run.ended_at),
                }
            )
            edges.append(
                {
                    "source": run.source or "__start__",
                    "target": run.name,
                    "label": run.edge or f"branch:to:{run.name}",
                }
            )
        return {
            "run_kind": "resume" if self._resumed else "send",
            "started_at": self._started_at,
            "ended_at": self._ended_at,
            "duration_ms": _milliseconds(self._started_at, self._ended_at),
            "nodes": nodes,
            "edges": edges,
        }

    def _start(self, timestamp: str | None, payload: dict[str, object]) -> None:
        name = payload.get("name")
        if not isinstance(name, str) or name in _FRAMEWORK_NODES:
            return
        try:
            DispatchNode(name)
        except ValueError:
            # A node outside the closed vocabulary is either a future graph
            # this build does not know or a framework internals name; either
            # way its label cannot be a captured value.
            return
        task_id = str(payload.get("id", ""))
        attempt = self._attempts.get(name, 0) + 1
        self._attempts[name] = attempt
        triggers = payload.get("triggers")
        edge = (
            str(next(iter(triggers), ""))
            if isinstance(triggers, Sequence) and not isinstance(triggers, str | bytes)
            else ""
        )
        run = _NodeRun(
            name=name,
            attempt=attempt,
            edge=edge or None,
            source=self._last_completed,
            started_at=timestamp,
            replayed=attempt > 1 or (self._resumed and not self._order),
        )
        self._by_task[task_id] = run
        self._order.append(task_id)
        if self._started_at is None:
            self._started_at = timestamp

    def _finish(self, timestamp: str | None, payload: dict[str, object]) -> None:
        task_id = str(payload.get("id", ""))
        run = self._by_task.get(task_id)
        if run is None:
            return
        name = payload.get("name")
        run.ended_at = timestamp
        run.interrupted = bool(payload.get("interrupts"))
        if payload.get("error") is not None:
            run.status = NodeStatus.ERROR.value
        if isinstance(name, str):
            self._last_completed = name
        self._ended_at = timestamp


def _milliseconds(started_at: str | None, ended_at: str | None) -> int | None:
    """Wall duration between two ISO-8601 event timestamps, in milliseconds.

    ``None`` when either endpoint is missing — a node that entered and never
    exited (a mid-graph crash) has no duration, and the section must not
    invent one.
    """
    if started_at is None or ended_at is None:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        end = datetime.fromisoformat(ended_at)
    except ValueError:
        return None
    return round((end - start).total_seconds() * 1000)
