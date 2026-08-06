"""The workflow state machine: which states a workflow may move between.

A workflow is the durable record of one intent-driven conversation segment: the
intent, the fields collected so far, the pending confirmation, the tool results,
and the actions the agent may still take. The graph *drives* the workflow
through its effects, but what a transition means is decided here, in the
framework-free domain — the same rule a worker or a batch job would apply.

The transition table is deliberately conservative. ``SUSPENDED`` is not
resumable: a suspended workflow is one the customer walked away from to ask
about something else, and when they come back the conversation starts a fresh
workflow rather than reviving abandoned state that the model cannot see.
``FAILED`` may only be handed off, because a workflow that failed mid-flight
must end in a person's hands, not in a silent retry. ``COMPLETED``,
``CANCELLED``, ``SUSPENDED``, ``FAILED``, and ``HANDED_OFF`` are the stopping
states, stamped with when they stopped.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from tenantchat.core.errors import WorkflowTransitionError
from tenantchat.core.routing import IntentName


class WorkflowStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    SUSPENDED = "suspended"
    FAILED = "failed"
    HANDED_OFF = "handed_off"


class WorkflowTransition(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    COMPLETE = "complete"
    CANCEL = "cancel"
    SUSPEND = "suspend"
    FAIL = "fail"
    HAND_OFF = "hand_off"


@dataclass(frozen=True, slots=True)
class ToolResult:
    """One executed tool call's outcome, as the workflow record holds it.

    ``result`` is the JSON payload the model was shown — the same content the
    transcript carries — so the workflow record is self-contained enough for a
    later `OBS-004` reconstruction of "what the agent knew at this point".
    """

    call_id: str
    name: str
    result: str


@dataclass(frozen=True, slots=True)
class WorkflowState:
    """One durable workflow, as the domain and its services see it."""

    workflow_id: str
    tenant_id: str
    session_id: str
    intent: IntentName
    agent_version: str
    status: WorkflowStatus
    collected_fields: Mapping[str, str]
    pending_confirmation: Mapping[str, object] | None
    tool_results: tuple[ToolResult, ...]
    next_allowed_actions: tuple[str, ...]
    turn_index: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


# Statuses where the workflow stops progressing, stamped with ``completed_at``.
# ``SUSPENDED`` is included on purpose: a workflow the customer walked away
# from is not going to move again, and the durable record should say when it
# stopped. ``FAILED`` still narrows to ``HANDED_OFF``; the timestamp survives.
_TERMINAL: frozenset[WorkflowStatus] = frozenset(
    {
        WorkflowStatus.COMPLETED,
        WorkflowStatus.CANCELLED,
        WorkflowStatus.SUSPENDED,
        WorkflowStatus.FAILED,
        WorkflowStatus.HANDED_OFF,
    }
)

# The status each transition lands on. Kept next to the permission table so the
# two halves of the state machine read as one decision.
_RESULTING_STATUS: dict[WorkflowTransition, WorkflowStatus] = {
    WorkflowTransition.PAUSE: WorkflowStatus.PAUSED,
    WorkflowTransition.RESUME: WorkflowStatus.ACTIVE,
    WorkflowTransition.COMPLETE: WorkflowStatus.COMPLETED,
    WorkflowTransition.CANCEL: WorkflowStatus.CANCELLED,
    WorkflowTransition.SUSPEND: WorkflowStatus.SUSPENDED,
    WorkflowTransition.FAIL: WorkflowStatus.FAILED,
    WorkflowTransition.HAND_OFF: WorkflowStatus.HANDED_OFF,
}

_PERMITTED: dict[WorkflowStatus, frozenset[WorkflowTransition]] = {
    WorkflowStatus.ACTIVE: frozenset(
        {
            WorkflowTransition.PAUSE,
            WorkflowTransition.COMPLETE,
            WorkflowTransition.CANCEL,
            WorkflowTransition.SUSPEND,
            WorkflowTransition.FAIL,
            WorkflowTransition.HAND_OFF,
        }
    ),
    WorkflowStatus.PAUSED: frozenset(
        {
            WorkflowTransition.RESUME,
            WorkflowTransition.CANCEL,
            WorkflowTransition.SUSPEND,
            WorkflowTransition.FAIL,
            WorkflowTransition.HAND_OFF,
        }
    ),
    WorkflowStatus.SUSPENDED: frozenset({WorkflowTransition.CANCEL, WorkflowTransition.HAND_OFF}),
    WorkflowStatus.FAILED: frozenset({WorkflowTransition.HAND_OFF}),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
    WorkflowStatus.HANDED_OFF: frozenset(),
}


def transition_workflow(
    state: WorkflowState,
    transition: WorkflowTransition,
    *,
    payload: Mapping[str, object] | None = None,
    now: datetime | None = None,
) -> WorkflowState:
    """Apply one transition, or refuse it.

    ``PAUSE`` carries the pending confirmation into the workflow, and every
    other transition clears it: a workflow is waiting on exactly one question
    at most, and the question lives here rather than in the checkpoint.

    Raises:
        WorkflowTransitionError: the transition is not permitted from the
            workflow's current status.
    """
    permitted = _PERMITTED[state.status]
    if transition not in permitted:
        raise WorkflowTransitionError(
            current=state.status,
            transition=transition,
            permitted=permitted,
            detail=(
                f"workflow {state.workflow_id} in {state.status.value} cannot "
                f"{transition.value}"
            ),
        )

    status = _RESULTING_STATUS[transition]
    timestamp = now if now is not None else state.updated_at
    if transition is WorkflowTransition.PAUSE:
        pending: Mapping[str, object] | None = dict(payload) if payload is not None else None
    else:
        pending = None
    completed_at = timestamp if status in _TERMINAL else None
    return WorkflowState(
        workflow_id=state.workflow_id,
        tenant_id=state.tenant_id,
        session_id=state.session_id,
        intent=state.intent,
        agent_version=state.agent_version,
        status=status,
        collected_fields=dict(state.collected_fields),
        pending_confirmation=pending,
        tool_results=state.tool_results,
        next_allowed_actions=state.next_allowed_actions,
        turn_index=state.turn_index,
        created_at=state.created_at,
        updated_at=timestamp,
        completed_at=completed_at if completed_at is not None else state.completed_at,
    )
