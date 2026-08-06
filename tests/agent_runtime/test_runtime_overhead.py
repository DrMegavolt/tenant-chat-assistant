"""The price `ADR-0001` agreed to pay for having one runtime.

The ADR accepted that "simple one-turn chat now pays graph and checkpoint
overhead it does not need". A cost that is accepted but never measured is a cost
that grows, so this module measures it and fails when it grows past the budget.

Both numbers are written to ``artifacts/agent-runtime-overhead.json`` on every
run, so the record is what this machine measured rather than what a document
once claimed. The budgets are ceilings with room in them: the point is to catch
a step change — a node added to the hot path, a per-turn round trip introduced
into a service — not to police a few percent of drift on a shared CI runner.

Measured on the reference machine at the time of writing: 9 checkpoint writes
and roughly 1.5 ms median for a question that calls no tools and commits
nothing (`AGENT-001` added the router node to this path). The model is
scripted, so the figure is framework overhead alone.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from pathlib import Path

from tenantchat.orchestration.model import ModelResponse
from tests.agent_runtime.conftest import BOOKING_TENANT, CountingSaver, build_harness

MEASUREMENTS = Path(__file__).resolve().parents[2] / "artifacts" / "agent-runtime-overhead.json"

# Three nodes and their supersteps (route, model, finalize — `AGENT-001`
# added the router to the hot path deliberately). Nine is the measured figure;
# twelve leaves room for LangGraph's own accounting to shift by a couple, while
# still catching a fourth node appearing on this path.
MAX_SINGLE_TURN_CHECKPOINT_WRITES = 12

# In-process wall time for a turn whose model call is instant. Far above the
# observed figure on purpose — CI hardware is not the reference machine, and a
# tight bound here buys flakiness rather than information.
MAX_SINGLE_TURN_MEDIAN_MS = 25.0

SAMPLES = 25


def _answer_only() -> list[ModelResponse]:
    return [ModelResponse(content="We are open until 7pm.", model_name="scripted")]


def record(**measurements: float) -> None:
    """Merge measurements into the run's artifact.

    Merged rather than overwritten so the two tests here can be run
    independently — `pytest -k latency` should not erase the checkpoint figure
    a previous run wrote.
    """
    MEASUREMENTS.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, float] = {}
    if MEASUREMENTS.is_file():
        existing = json.loads(MEASUREMENTS.read_text(encoding="utf-8"))
    MEASUREMENTS.write_text(
        json.dumps(existing | measurements, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def test_a_single_turn_question_stays_within_its_checkpoint_budget() -> None:
    """A question that calls no tools writes no more checkpoints than its path needs."""
    saver = CountingSaver()
    harness = build_harness(_answer_only(), checkpointer=saver)

    async def scenario() -> None:
        result = await harness.runtime.send(BOOKING_TENANT, "s-budget", "what are your hours?")
        assert result.answer == "We are open until 7pm."
        assert result.committed == ()

    asyncio.run(scenario())
    record(single_turn_checkpoint_writes=saver.checkpoint_writes)

    assert saver.checkpoint_writes <= MAX_SINGLE_TURN_CHECKPOINT_WRITES


def test_a_single_turn_question_stays_within_its_latency_budget() -> None:
    """The graph's own cost per turn, with the model's cost held at zero."""

    async def scenario() -> list[float]:
        timings = []
        for sample in range(SAMPLES):
            harness = build_harness(_answer_only())
            started = time.perf_counter()
            await harness.runtime.send(BOOKING_TENANT, f"s-latency-{sample}", "hours?")
            timings.append((time.perf_counter() - started) * 1000)
        return timings

    timings = asyncio.run(scenario())
    median = statistics.median(timings)
    record(
        single_turn_median_ms=round(median, 3),
        single_turn_max_ms=round(max(timings), 3),
    )

    assert median <= MAX_SINGLE_TURN_MEDIAN_MS
