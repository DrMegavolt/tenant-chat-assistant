"""Where the graph goes next, decided without running a node.

Routing is pure, so it can be specified directly instead of inferred from an
end-to-end run. That matters for the branches that are otherwise awkward to
reach — a model that returns nothing, a round budget already spent — which are
exactly the branches a customer meets on a bad day.
"""

from __future__ import annotations

import pytest

from tenantchat.orchestration.nodes import (
    MAX_TOOL_ROUNDS,
    BookingDecision,
    DispatchNode,
    route_after_confirmation,
    route_after_model,
    route_after_routing,
    route_after_tools,
)
from tenantchat.orchestration.state import (
    DispatchState,
    StoredToolCall,
    assistant_entry,
    initial_state,
)


def stored(name: str, call_id: str = "call-1") -> StoredToolCall:
    return {"call_id": call_id, "name": name, "arguments_json": "{}"}


def state_after(*calls: StoredToolCall, **overrides: object) -> DispatchState:
    """The state the model node would have produced from these tool calls.

    ``pending_booking`` is filled in the way ``call_model`` fills it, so a
    routing test is answering the question the graph actually asks rather than
    one about a state the graph never reaches.
    """
    base = initial_state("clearview", "session-1", "hello")
    base["transcript"] = [*base["transcript"], assistant_entry("", list(calls))]
    base["rounds"] = 1
    base["pending_booking"] = next(
        (call for call in calls if call["name"] == "book_appointment"), None
    )
    return base | overrides  # type: ignore[return-value]


def test_a_plain_answer_finalizes() -> None:
    assert route_after_model(state_after()) is DispatchNode.FINALIZE


def test_a_read_only_tool_call_runs_the_tools_node() -> None:
    assert route_after_model(state_after(stored("get_availability"))) is DispatchNode.TOOLS


def test_a_booking_alone_goes_straight_to_the_confirmation() -> None:
    """Nothing to run first, so the customer is asked immediately."""
    assert (
        route_after_model(state_after(stored("book_appointment"))) is DispatchNode.CONFIRM_BOOKING
    )


def test_a_booking_alongside_another_tool_runs_that_tool_first() -> None:
    """Every tool call needs a result before the model may speak again.

    Confirming first would leave the other call unanswered across an interrupt
    that a customer may take minutes to resolve.
    """
    route = route_after_model(
        state_after(stored("get_availability"), stored("book_appointment", "call-2"))
    )

    assert route is DispatchNode.TOOLS


def test_a_second_booking_counts_as_a_tool_to_run() -> None:
    """Only one booking is awaiting confirmation; the other still needs a result.

    Routing compares call IDs rather than tool names for exactly this case — by
    name, both calls would look like the pending booking and the second would
    never be answered.
    """
    route = route_after_model(
        state_after(stored("book_appointment"), stored("book_appointment", "call-2"))
    )

    assert route is DispatchNode.TOOLS


def test_a_model_failure_escalates_without_consulting_its_output() -> None:
    assert route_after_model(state_after(failure="tool_failure")) is DispatchNode.ESCALATE


def test_a_spent_round_budget_escalates_rather_than_calling_again() -> None:
    """The guard is what turns a looping model into one handoff."""
    exhausted = state_after(stored("get_availability"), rounds=MAX_TOOL_ROUNDS)

    assert route_after_model(exhausted) is DispatchNode.ESCALATE


def test_tools_hand_over_to_the_confirmation_when_a_booking_is_pending() -> None:
    pending = state_after(stored("book_appointment"), pending_booking=stored("book_appointment"))

    assert route_after_tools(pending) is DispatchNode.CONFIRM_BOOKING


def test_tools_return_to_the_model_when_nothing_is_pending() -> None:
    assert route_after_tools(state_after(stored("get_availability"))) is DispatchNode.MODEL


def test_a_confirmation_that_cleared_the_booking_returns_to_the_model() -> None:
    """Validation refused before the customer was asked, so there is nothing to commit."""
    assert route_after_confirmation(state_after(pending_booking=None)) is DispatchNode.MODEL


def test_a_surviving_pending_booking_reaches_the_commit_node() -> None:
    pending = state_after(pending_booking=stored("book_appointment"))

    assert route_after_confirmation(pending) is DispatchNode.COMMIT_BOOKING


@pytest.mark.parametrize(
    "resumed",
    [True, "approved", "  Approved  ", {"decision": "approved"}, {"decision": True}],
    ids=["bool", "word", "padded", "mapping", "nested-bool"],
)
def test_an_explicit_approval_is_recognized(resumed: object) -> None:
    assert BookingDecision.of(resumed) is BookingDecision.APPROVED


@pytest.mark.parametrize(
    "resumed",
    [False, None, "", "yes", "ok", 1, {"unexpected": "shape"}, ["approved"]],
    ids=["false", "none", "empty", "yes", "ok", "one", "wrong-key", "list"],
)
def test_everything_else_declines(resumed: object) -> None:
    """A resume value crosses a trust boundary, and the failures are asymmetric.

    ``"yes"`` and ``1`` look like approvals and are refused on purpose: the
    caller's contract is one word, and widening it here would make the set of
    values that dispatch a crew depend on what someone happened to send.
    """
    assert BookingDecision.of(resumed) is BookingDecision.DECLINED


# --- AGENT-001: where a routed turn goes ------------------------------------


def routed(**overrides: object) -> DispatchState:
    base = initial_state("clearview", "session-1", "hello")
    return base | overrides  # type: ignore[return-value]


def test_a_direct_route_sends_the_turn_to_the_model() -> None:
    state = routed(routing_outcome="direct", routed_intent="booking")

    assert route_after_routing(state) is DispatchNode.MODEL


def test_a_clarification_ends_as_a_question_without_calling_the_model() -> None:
    state = routed(routing_outcome="clarify")

    assert route_after_routing(state) is DispatchNode.FINALIZE


def test_an_unresolved_route_escalates_without_calling_the_model() -> None:
    state = routed(routing_outcome="handoff", route_rule="bounded_clarify")

    assert route_after_routing(state) is DispatchNode.ESCALATE


def test_a_routed_handoff_request_escalates_directly() -> None:
    """The customer asked for a person; the router does not spend a model call."""
    state = routed(routing_outcome="direct", routed_intent="handoff")

    assert route_after_routing(state) is DispatchNode.ESCALATE
