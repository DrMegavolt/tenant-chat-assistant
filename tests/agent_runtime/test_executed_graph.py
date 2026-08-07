"""`OBS-006` end-to-end: the graph that actually ran is what the trace records.

The runtime executes through LangGraph's debug stream, so a turn's trace carries
the executed-graph section: the nodes that ran with their edges, attempts, and
durations, and — for a resumed or crashed run — the honest shape of that run. A
resumed turn is visibly a resume, a crashed turn stops at the node that crashed
instead of idealizing a completion, and a listener that raises degrades to the
derived view while the turn still answers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Mapping
from typing import cast

import pytest

from tenantchat.core.ports import WorkflowService
from tenantchat.orchestration.checkpoints import InMemorySaver
from tenantchat.orchestration.graph import compile_dispatch_graph
from tenantchat.orchestration.model import ModelResponse
from tenantchat.orchestration.runtime import DispatchRuntime
from tenantchat.orchestration.trace import TRACE_SCHEMA_VERSION
from tests.agent_runtime.conftest import (
    BOOKING_TENANT,
    RuntimeHarness,
    booking_arguments,
    build_harness,
    tool_call,
)

CRASH_REPLY = "I could not finish that because of an unexpected error. Please try again."


def _booking_script() -> list[ModelResponse]:
    return [
        ModelResponse(
            content="",
            tool_calls=(tool_call("book_appointment", **booking_arguments()),),
            model_name="scripted",
        ),
        ModelResponse(content="You are booked.", model_name="scripted"),
    ]


def _section(trace: Mapping[str, object] | None, key: str) -> dict[str, object]:
    assert trace is not None
    value = trace[key]
    assert isinstance(value, Mapping)
    return dict(value)


def _executed(trace: Mapping[str, object] | None) -> dict[str, object]:
    assert trace is not None
    value = trace.get("executed_graph")
    assert isinstance(value, Mapping)
    return dict(value)


def _nodes(section: Mapping[str, object]) -> list[dict[str, object]]:
    raw = section["nodes"]
    assert isinstance(raw, list)
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _diagnoses(trace: Mapping[str, object] | None) -> list[dict[str, object]]:
    assert trace is not None
    raw = trace.get("diagnoses")
    assert isinstance(raw, list)
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def test_a_turn_records_the_nodes_and_edges_that_actually_ran() -> None:
    """A plain answer's section names the real path: route, model, finalize."""

    async def scenario() -> None:
        harness = build_harness(
            [ModelResponse(content="We are open until 7pm.", model_name="scripted")]
        )
        result = await harness.runtime.send(BOOKING_TENANT, "s-captured", "What are your hours?")

        assert result.answer == "We are open until 7pm."
        assert result.trace is not None
        assert result.trace["schema_version"] == TRACE_SCHEMA_VERSION
        section = _executed(result.trace)
        assert [node["name"] for node in _nodes(section)] == ["route", "model", "finalize"]
        edges = section["edges"]
        assert isinstance(edges, list)
        assert [edge["source"] for edge in edges] == ["__start__", "route", "model"]
        assert [edge["target"] for edge in edges] == ["route", "model", "finalize"]
        assert [edge["label"] for edge in edges] == [
            "branch:to:route",
            "branch:to:model",
            "branch:to:finalize",
        ]
        assert section["run_kind"] == "send"
        assert section["duration_ms"] is not None
        for node in _nodes(section):
            # Captured, not derived: every node has its own entry and exit.
            assert node["attempt"] == 1
            assert node["status"] == "ok"
            assert node["started_at"] is not None
            assert node["ended_at"] is not None
            assert node["duration_ms"] is not None

    asyncio.run(scenario())


def test_a_paused_turn_ends_at_the_interrupt_and_is_recorded_paused() -> None:
    """The booking confirmation is a real stop: the paused run's section ends at
    the node that paused, and the trace records the pause."""

    async def scenario() -> None:
        harness = build_harness(_booking_script())
        result = await harness.runtime.send(BOOKING_TENANT, "s-paused", "Book HVAC")

        assert result.is_paused
        assert _section(result.trace, "outcome")["status"] == "paused"
        section = _executed(result.trace)
        assert [node["name"] for node in _nodes(section)] == [
            "route",
            "model",
            "confirm_booking",
        ]
        assert _nodes(section)[-1]["interrupted"] is True
        assert _nodes(section)[-1]["status"] == "ok"

    asyncio.run(scenario())


def test_a_resumed_turn_is_visibly_distinguishable_from_a_first_run() -> None:
    """A checkpoint resume re-runs the interrupted node; the record says so.

    The resumed run is marked ``resume``, its first node is the one the pause
    interrupted (replayed), and the section then walks the rest of the graph to
    an answer — a shape no first run could produce.
    """

    async def scenario() -> None:
        harness = build_harness(_booking_script())
        first = await harness.runtime.send(BOOKING_TENANT, "s-resume", "Book HVAC")
        resumed = await harness.runtime.resume(BOOKING_TENANT, "s-resume", True)

        assert _section(first.trace, "outcome")["status"] == "paused"
        first_section = _executed(first.trace)
        assert first_section["run_kind"] == "send"
        assert [node["name"] for node in _nodes(first_section)] == [
            "route",
            "model",
            "confirm_booking",
        ]

        assert resumed.answer == "You are booked."
        resumed_section = _executed(resumed.trace)
        assert resumed_section["run_kind"] == "resume"
        assert [node["name"] for node in _nodes(resumed_section)] == [
            "confirm_booking",
            "commit_booking",
            "model",
            "finalize",
        ]
        assert _nodes(resumed_section)[0]["replayed"] is True
        assert _nodes(resumed_section)[0]["name"] == "confirm_booking"

    asyncio.run(scenario())


class _BoomWorkflows:
    """A workflow service whose first read raises, crashing the route node.

    The crash is deliberate and unhandled by any node, which is the only way the
    graph itself fails: the model port's exceptions are caught by the model node
    and become a handoff, so a hard crash needs a genuinely unexpected failure.
    """

    def __init__(self, inner: object) -> None:
        self._inner = inner

    async def current(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("workflow backend exploded")

    async def last_routing(self, *args: object, **kwargs: object) -> None:
        return None

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def _crashing_runtime(harness: RuntimeHarness) -> DispatchRuntime:
    dependencies = dataclasses.replace(
        harness.dependencies, workflows=cast(WorkflowService, _BoomWorkflows(harness.workflows))
    )
    return DispatchRuntime(compile_dispatch_graph(dependencies, InMemorySaver()))


def test_a_crashed_turn_shows_the_nodes_that_ran_and_stops() -> None:
    """The route node crashed on entry; the section records it and nothing more.

    There is no idealized completion: no model, no finalize, no invented answer.
    The turn is recorded as failed with an application-error diagnosis so the
    crash is inspectable rather than a silent 500.
    """

    async def scenario() -> None:
        harness = build_harness([ModelResponse(content="hi", model_name="scripted")])
        runtime = _crashing_runtime(harness)
        result = await runtime.send(BOOKING_TENANT, "s-crash", "What are your hours?")

        assert result.answer == CRASH_REPLY
        assert _section(result.trace, "outcome") == {
            "status": "failed",
            "rounds": 0,
            "failure": "application_error",
        }
        section = _executed(result.trace)
        assert [node["name"] for node in _nodes(section)] == ["route"]
        assert _nodes(section)[0]["status"] == "error"
        assert _nodes(section)[0]["ended_at"] is None
        assert _nodes(section)[0]["duration_ms"] is None
        assert [diagnosis["cause"] for diagnosis in _diagnoses(result.trace)] == [
            "application_error"
        ]
        assert result.committed == ()

    asyncio.run(scenario())


def test_a_listener_failure_still_answers_and_degrades_to_the_derived_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acceptance test: a listener that raises must not fail the turn.

    The trace records no executed-graph section, the schema stays current, and
    the answer is the one the graph produced — the listener is off the critical
    section's decision path by construction.
    """

    class RaisingListener:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def on_event(self, part: object) -> None:
            raise RuntimeError("listener exploded")

        def crash(self) -> None:
            return None

        def to_section(self) -> None:
            return None

    async def scenario() -> None:
        harness = build_harness(
            [ModelResponse(content="We are open until 7pm.", model_name="scripted")]
        )
        monkeypatch.setattr(
            "tenantchat.orchestration.runtime.ExecutedGraphListener", RaisingListener
        )
        result = await harness.runtime.send(BOOKING_TENANT, "s-degrade", "What are your hours?")

        assert result.answer == "We are open until 7pm."
        assert result.trace is not None
        assert result.trace["schema_version"] == TRACE_SCHEMA_VERSION
        assert _section(result.trace, "outcome")["status"] == "answered"
        assert "executed_graph" not in result.trace

    asyncio.run(scenario())


def test_no_captured_event_reaches_the_operational_plane() -> None:
    """A hostile turn's content stays out of the executed-graph section.

    The message, the model output, and the tool arguments carry contact details
    and free text; the section's own JSON must contain none of them.
    """

    async def scenario() -> None:
        extra = {
            k: v
            for k, v in booking_arguments().items()
            if k not in ("customer_name", "customer_phone_or_email", "address")
        }
        harness = build_harness(
            [
                ModelResponse(
                    content="",
                    tool_calls=(
                        tool_call(
                            "book_appointment",
                            customer_name="Dana PII-Marker Ruiz",
                            customer_phone_or_email="555-222-1919",
                            address="12 PII-Marker Lane, Portland, OR 97205",
                            **extra,
                        ),
                    ),
                    model_name="scripted",
                ),
                ModelResponse(
                    content="Booked for dana.pii@example.com at 555-222-1919.",
                    model_name="scripted",
                ),
            ]
        )
        result = await harness.runtime.send(BOOKING_TENANT, "s-pii", "Book HVAC for PII-Marker")
        section = _executed(result.trace)
        encoded = json.dumps(section)

        assert "PII-Marker" not in encoded
        assert "555-222-1919" not in encoded
        assert "dana.pii@example.com" not in encoded
        assert "12 Alder Court" not in encoded

    asyncio.run(scenario())


class _RecordingMetrics:
    """A metrics port that keeps what was observed, so a test can read it back."""

    def __init__(self) -> None:
        self.observations: list[tuple[str, float, dict[str, str]]] = []

    def observe(
        self,
        name: object,
        value: float,
        *,
        labels: Mapping[str, str] | None = None,
    ) -> None:
        self.observations.append((str(name), value, dict(labels or {})))


def test_a_crashed_turn_is_counted_once_in_the_outcome_partition() -> None:
    """A crash records exactly one failed outcome and skips no latency.

    `OBS-002` requires the outcome distribution to sum to the turns the API
    completed; the runtime records the failed class itself because no terminal
    node exists to do it, and the label is distinct from a failed tool call.
    """

    async def scenario() -> None:
        metrics = _RecordingMetrics()
        harness = build_harness([ModelResponse(content="hi", model_name="scripted")])
        dependencies = dataclasses.replace(
            harness.dependencies,
            workflows=cast(WorkflowService, _BoomWorkflows(harness.workflows)),
            metrics=metrics,
        )
        runtime = DispatchRuntime(
            compile_dispatch_graph(dependencies, InMemorySaver()), metrics=metrics
        )
        result = await runtime.send(BOOKING_TENANT, "s-partition", "What are your hours?")

        outcomes = [
            value
            for name, value, _labels in metrics.observations
            if name == "tenantchat_turn_outcomes_total"
        ]
        assert outcomes == [1.0]
        outcome_labels = [
            labels
            for name, _value, labels in metrics.observations
            if name == "tenantchat_turn_outcomes_total"
        ]
        assert outcome_labels == [{"outcome": "turn_failed"}]
        # The crashed node never completed, so no node latency is invented for it.
        node_latencies = [
            (labels["node"], labels["status"])
            for name, _value, labels in metrics.observations
            if name == "tenantchat_node_latency_seconds"
        ]
        assert node_latencies == []
        assert _section(result.trace, "outcome")["status"] == "failed"

    asyncio.run(scenario())
