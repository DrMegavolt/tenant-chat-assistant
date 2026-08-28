"""`OBS-006` executed-graph capture: what the listener records, content-free.

The listener is the boundary that turns LangGraph's debug stream into the trace
section. These tests pin what it keeps (node names, edges, attempts, durations,
terminal status) and what it cannot keep: no argument, message, or evidence text
survives, a node outside the closed vocabulary is dropped, and a run that
crashes stops at the node that crashed instead of inventing a completion.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from tenantchat.orchestration.executed import ExecutedGraphListener


def _task(
    task_id: str,
    name: str,
    triggers: tuple[str, ...] = (),
    *,
    ts: str = "2026-08-07T00:00:00+00:00",
) -> dict[str, object]:
    return {
        "type": "task",
        "timestamp": ts,
        "payload": {"id": task_id, "name": name, "triggers": triggers, "input": {}},
    }


def _result(
    task_id: str,
    name: str,
    *,
    ts: str = "2026-08-07T00:00:00.005+00:00",
    error: object = None,
    interrupts: tuple[object, ...] = (),
) -> dict[str, object]:
    return {
        "type": "task_result",
        "timestamp": ts,
        "payload": {
            "id": task_id,
            "name": name,
            "error": error,
            "result": {},
            "interrupts": list(interrupts),
        },
    }


def _nodes(section: Mapping[str, object]) -> list[dict[str, object]]:
    raw = section["nodes"]
    assert isinstance(raw, list)
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def _edges(section: Mapping[str, object]) -> list[dict[str, object]]:
    raw = section["edges"]
    assert isinstance(raw, list)
    return [dict(item) for item in raw if isinstance(item, Mapping)]


def test_a_first_run_records_each_node_its_edge_and_duration() -> None:
    listener = ExecutedGraphListener()
    listener.on_event(_task("1", "route", ("branch:to:route",), ts="2026-08-07T00:00:00.001+00:00"))
    listener.on_event(_result("1", "route", ts="2026-08-07T00:00:00.004+00:00"))
    listener.on_event(_task("2", "model", ("branch:to:model",), ts="2026-08-07T00:00:00.005+00:00"))
    listener.on_event(_result("2", "model", ts="2026-08-07T00:00:00.006+00:00"))

    section = listener.to_section()
    assert section is not None

    assert section is not None
    assert section["run_kind"] == "send"
    assert [node["name"] for node in _nodes(section)] == ["route", "model"]
    assert _nodes(section)[0] == {
        "name": "route",
        "attempt": 1,
        "edge": "branch:to:route",
        "status": "ok",
        "interrupted": False,
        "replayed": False,
        "started_at": "2026-08-07T00:00:00.001+00:00",
        "ended_at": "2026-08-07T00:00:00.004+00:00",
        "duration_ms": 3,
    }
    assert _edges(section) == [
        {"source": "__start__", "target": "route", "label": "branch:to:route"},
        {"source": "route", "target": "model", "label": "branch:to:model"},
    ]
    assert section["duration_ms"] == 5


def test_a_resumed_run_is_marked_and_its_replayed_node_identified() -> None:
    listener = ExecutedGraphListener(resumed=True)
    listener.on_event(_task("9", "confirm_booking", ("branch:to:confirm_booking",)))
    listener.on_event(_result("9", "confirm_booking"))
    listener.on_event(_task("10", "commit_booking", ("branch:to:commit_booking",)))
    listener.on_event(_result("10", "commit_booking"))

    section = listener.to_section()
    assert section is not None

    assert section is not None
    assert section["run_kind"] == "resume"
    # The first node of a resumed run is the node the interrupt paused and
    # re-executed; it is the visible marker that this run resumed.
    assert _nodes(section)[0]["name"] == "confirm_booking"
    assert _nodes(section)[0]["replayed"] is True
    assert _nodes(section)[1]["replayed"] is False


def test_a_real_router_edge_label_is_captured_not_synthesized() -> None:
    """The captured edge must be the trigger that actually fired.

    The fallback labels an unknown edge ``branch:to:<node>``, so a test that
    feeds exactly that string cannot tell capture from fallback. A real router
    edge (e.g. the ``route`` node's conditional branch) is a different label,
    and only capture can produce it — this pins the edge to the trigger rather
    than to a synthesized stand-in.
    """
    listener = ExecutedGraphListener()
    listener.on_event(_task("1", "route", ("route:escalate",)))
    listener.on_event(_result("1", "route"))
    listener.on_event(_task("2", "escalate", ("route:escalate",)))
    listener.on_event(_result("2", "escalate"))

    section = listener.to_section()
    assert section is not None
    assert _edges(section) == [
        {"source": "__start__", "target": "route", "label": "route:escalate"},
        {"source": "route", "target": "escalate", "label": "route:escalate"},
    ]
    assert _nodes(section)[0]["edge"] == "route:escalate"


def test_list_triggers_are_read_the_same_as_tuple_triggers() -> None:
    """LangGraph annotates ``triggers`` as a sequence; a list must capture too.

    Before the fix the guard was ``isinstance(triggers, tuple)`` — a list fell
    through to the fallback label, and because the fallback can synthesize the
    very strings the tests asserted, the broken capture passed silently.
    """
    listener = ExecutedGraphListener()
    listener.on_event(_task_list_triggers("1", "route", ["route:model"]))
    listener.on_event(_result("1", "route"))

    section = listener.to_section()
    assert section is not None
    assert _nodes(section)[0]["edge"] == "route:model"
    assert _edges(section)[0]["label"] == "route:model"


def _task_list_triggers(task_id: str, name: str, triggers: list[str]) -> dict[str, object]:
    return {
        "type": "task",
        "timestamp": "2026-08-07T00:00:00+00:00",
        "payload": {"id": task_id, "name": name, "triggers": triggers, "input": {}},
    }


def test_a_second_round_of_a_tool_loop_records_an_attempt_not_a_replay() -> None:
    """Attempt numbers count fresh executions; ``replayed`` is resume only.

    Conflating the two mislabelled every ordinary multi-round turn as a
    replayed one, and hid the signal a checkpoint resume is supposed to carry.
    A second round runs new work — it is attempt 2 and nothing else.
    """
    listener = ExecutedGraphListener()
    for task_id in ("a", "b"):
        listener.on_event(_task(task_id, "model", ("branch:to:model",)))
        listener.on_event(_result(task_id, "model"))

    section = listener.to_section()
    assert section is not None

    assert [node["attempt"] for node in _nodes(section)] == [1, 2]
    assert all(node["replayed"] is False for node in _nodes(section))


def test_an_interrupted_node_is_marked_but_ok() -> None:
    listener = ExecutedGraphListener()
    listener.on_event(_task("1", "route"))
    listener.on_event(_result("1", "route"))
    listener.on_event(_task("2", "confirm_booking", ("branch:to:confirm_booking",)))
    listener.on_event(
        _result("2", "confirm_booking", interrupts=({"awaiting": "booking_confirmation"},))
    )

    section = listener.to_section()
    assert section is not None

    assert _nodes(section)[1]["interrupted"] is True
    assert _nodes(section)[1]["status"] == "ok"
    assert _nodes(section)[1]["ended_at"] is not None


def test_a_crashed_run_stops_at_the_crashed_node_without_idealizing() -> None:
    """The crashed node entered but never exited; the section must end there."""
    listener = ExecutedGraphListener()
    listener.on_event(_task("1", "route"))
    listener.on_event(_result("1", "route"))
    listener.on_event(_task("2", "model", ("branch:to:model",)))
    listener.crash()

    section = listener.to_section()
    assert section is not None

    assert [node["name"] for node in _nodes(section)] == ["route", "model"]
    assert _nodes(section)[1]["status"] == "error"
    assert _nodes(section)[1]["ended_at"] is None
    assert _nodes(section)[1]["duration_ms"] is None
    # No node after the crash is invented — there is no finalize.
    assert "finalize" not in [node["name"] for node in _nodes(section)]


def test_no_captured_event_carries_content() -> None:
    """Task input and node output hold the full state, and the listener keeps none of it.

    The stream delivers prompt text, evidence, answers, and contact details on
    every event; the section must round-trip through JSON with none of them.
    """
    listener = ExecutedGraphListener()
    listener.on_event(
        {
            "type": "task",
            "timestamp": "2026-08-07T00:00:00+00:00",
            "payload": {
                "id": "1",
                "name": "model",
                "triggers": ("branch:to:model",),
                "input": {
                    "transcript": [
                        {"content": "Book HVAC for Dana PII-Marker Ruiz at 555-222-1919"}
                    ],
                    "prompt_assembly": {"messages": [{"content": "System: you are the assistant"}]},
                },
            },
        }
    )
    listener.on_event(
        {
            "type": "task_result",
            "timestamp": "2026-08-07T00:00:01+00:00",
            "payload": {
                "id": "1",
                "name": "model",
                "error": None,
                "result": {
                    "transcript": [
                        {"content": "It costs $99. Call 555-222-1919 [evidence:chunk-1]"}
                    ]
                },
                "interrupts": [],
            },
        }
    )

    section = listener.to_section()
    assert section is not None
    assert section is not None
    encoded = json.dumps(section)

    assert "PII-Marker" not in encoded
    assert "555-222-1919" not in encoded
    assert "$99" not in encoded
    assert "chunk-1" not in encoded
    assert "you are the assistant" not in encoded


def test_a_node_outside_the_closed_vocabulary_is_dropped() -> None:
    """A name the graph does not know must never become a captured label value."""
    listener = ExecutedGraphListener()
    listener.on_event(_task("1", "route"))
    listener.on_event(_result("1", "route"))
    listener.on_event(_task("2", "hallucinated_node"))
    listener.on_event(_result("2", "hallucinated_node"))

    section = listener.to_section()
    assert section is not None

    assert [node["name"] for node in _nodes(section)] == ["route"]


def test_framework_nodes_do_not_become_rows() -> None:
    listener = ExecutedGraphListener()
    listener.on_event(_task("__start__", "__start__"))
    listener.on_event(_result("__start__", "__start__"))
    listener.on_event(_task("1", "route", ("branch:to:route",)))
    listener.on_event(_result("1", "route"))

    section = listener.to_section()
    assert section is not None

    assert [node["name"] for node in _nodes(section)] == ["route"]
    assert _edges(section)[0]["source"] == "__start__"


def test_an_empty_listener_has_no_section() -> None:
    assert ExecutedGraphListener().to_section() is None
