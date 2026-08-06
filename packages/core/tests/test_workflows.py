"""The workflow state machine: permitted moves, and the ones that are refused.

A workflow is the durable record of one intent-driven conversation segment.
The graph drives it through transitions; this module is the specification of
which moves exist. Invalid transitions are the `AGENT-001` acceptance
criterion — the machine must refuse what the graph must not do.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tenantchat.core.errors import WorkflowTransitionError
from tenantchat.core.routing import IntentName
from tenantchat.core.workflows import (
    WorkflowState,
    WorkflowStatus,
    WorkflowTransition,
    transition_workflow,
)

NOW = datetime(2026, 8, 5, 12, 0, tzinfo=UTC)


def state(status: WorkflowStatus) -> WorkflowState:
    return WorkflowState(
        workflow_id="wf-1",
        tenant_id="clearview",
        session_id="session-1",
        intent=IntentName.BOOKING,
        agent_version="agents@1",
        status=status,
        collected_fields={"service": "HVAC"},
        pending_confirmation=None,
        tool_results=(),
        next_allowed_actions=("get_availability", "book_appointment"),
        turn_index=1,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )


@pytest.mark.parametrize(
    ("current", "transition", "result"),
    [
        (WorkflowStatus.ACTIVE, WorkflowTransition.PAUSE, WorkflowStatus.PAUSED),
        (WorkflowStatus.ACTIVE, WorkflowTransition.COMPLETE, WorkflowStatus.COMPLETED),
        (WorkflowStatus.ACTIVE, WorkflowTransition.CANCEL, WorkflowStatus.CANCELLED),
        (WorkflowStatus.ACTIVE, WorkflowTransition.SUSPEND, WorkflowStatus.SUSPENDED),
        (WorkflowStatus.ACTIVE, WorkflowTransition.FAIL, WorkflowStatus.FAILED),
        (WorkflowStatus.ACTIVE, WorkflowTransition.HAND_OFF, WorkflowStatus.HANDED_OFF),
        (WorkflowStatus.PAUSED, WorkflowTransition.RESUME, WorkflowStatus.ACTIVE),
        (WorkflowStatus.PAUSED, WorkflowTransition.CANCEL, WorkflowStatus.CANCELLED),
        (WorkflowStatus.PAUSED, WorkflowTransition.SUSPEND, WorkflowStatus.SUSPENDED),
        (WorkflowStatus.PAUSED, WorkflowTransition.FAIL, WorkflowStatus.FAILED),
        (WorkflowStatus.PAUSED, WorkflowTransition.HAND_OFF, WorkflowStatus.HANDED_OFF),
        (WorkflowStatus.SUSPENDED, WorkflowTransition.CANCEL, WorkflowStatus.CANCELLED),
        (WorkflowStatus.SUSPENDED, WorkflowTransition.HAND_OFF, WorkflowStatus.HANDED_OFF),
        (WorkflowStatus.FAILED, WorkflowTransition.HAND_OFF, WorkflowStatus.HANDED_OFF),
    ],
)
def test_the_permitted_transitions_land_on_their_statuses(
    current: WorkflowStatus, transition: WorkflowTransition, result: WorkflowStatus
) -> None:
    moved = transition_workflow(state(current), transition, now=NOW)

    assert moved.status is result


def test_a_pause_carries_the_pending_confirmation_into_the_workflow() -> None:
    """The question the customer is deciding must survive the checkpoint."""
    pending = {"awaiting": "booking_confirmation", "service": "HVAC"}
    moved = transition_workflow(
        state(WorkflowStatus.ACTIVE), WorkflowTransition.PAUSE, payload=pending, now=NOW
    )

    assert moved.pending_confirmation == pending


def test_every_transition_except_pause_clears_the_pending_confirmation() -> None:
    paused = transition_workflow(
        state(WorkflowStatus.ACTIVE),
        WorkflowTransition.PAUSE,
        payload={"awaiting": "booking_confirmation"},
        now=NOW,
    )

    # COMPLETE is deliberately absent: a workflow that is still waiting on the
    # customer must resume first — the decision is the resume, the commit is
    # the completion.
    for transition in (
        WorkflowTransition.RESUME,
        WorkflowTransition.CANCEL,
        WorkflowTransition.SUSPEND,
        WorkflowTransition.FAIL,
        WorkflowTransition.HAND_OFF,
    ):
        moved = transition_workflow(paused, transition, now=NOW)
        assert moved.pending_confirmation is None, transition


@pytest.mark.parametrize(
    ("current", "transition"),
    [
        (WorkflowStatus.COMPLETED, WorkflowTransition.RESUME),
        (WorkflowStatus.COMPLETED, WorkflowTransition.COMPLETE),
        (WorkflowStatus.CANCELLED, WorkflowTransition.RESUME),
        (WorkflowStatus.CANCELLED, WorkflowTransition.SUSPEND),
        (WorkflowStatus.HANDED_OFF, WorkflowTransition.RESUME),
        (WorkflowStatus.HANDED_OFF, WorkflowTransition.CANCEL),
        (WorkflowStatus.SUSPENDED, WorkflowTransition.RESUME),
        (WorkflowStatus.SUSPENDED, WorkflowTransition.COMPLETE),
        (WorkflowStatus.FAILED, WorkflowTransition.COMPLETE),
        (WorkflowStatus.FAILED, WorkflowTransition.RESUME),
        (WorkflowStatus.PAUSED, WorkflowTransition.PAUSE),
        (WorkflowStatus.PAUSED, WorkflowTransition.COMPLETE),
        (WorkflowStatus.ACTIVE, WorkflowTransition.RESUME),
    ],
)
def test_invalid_transitions_are_refused_with_the_machine_s_state(
    current: WorkflowStatus, transition: WorkflowTransition
) -> None:
    """Terminal states accept nothing; suspended and failed only narrow.

    The error carries the current status and what *would* have been accepted,
    so a caller that loaded a stale view can reload instead of guessing.
    """
    with pytest.raises(WorkflowTransitionError) as raised:
        transition_workflow(state(current), transition, now=NOW)

    assert raised.value.current is current
    assert raised.value.transition is transition
    assert transition not in raised.value.permitted


def test_a_resume_after_cancel_is_refused_because_the_workflow_is_gone() -> None:
    """The machine refuses what the graph must not do: reviving a cancellation."""
    cancelled = transition_workflow(
        state(WorkflowStatus.PAUSED), WorkflowTransition.CANCEL, now=NOW
    )

    with pytest.raises(WorkflowTransitionError):
        transition_workflow(cancelled, WorkflowTransition.RESUME, now=NOW)


def test_terminal_transitions_stamp_the_completion_time() -> None:
    completed = transition_workflow(
        state(WorkflowStatus.ACTIVE), WorkflowTransition.COMPLETE, now=NOW
    )

    assert completed.completed_at == NOW
    assert completed.status is WorkflowStatus.COMPLETED


def test_a_non_terminal_transition_leaves_no_completion_time() -> None:
    paused = transition_workflow(state(WorkflowStatus.ACTIVE), WorkflowTransition.PAUSE, now=NOW)

    assert paused.completed_at is None
