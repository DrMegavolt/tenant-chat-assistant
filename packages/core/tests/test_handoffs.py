"""The FEAT-004 handoff lifecycle rules: when the agent pauses, and what a
visitor may learn from the state."""

from __future__ import annotations

from tenantchat.core.handoffs import (
    HandoffStatus,
    is_agent_paused,
    state_notice,
    visitor_state_notice,
)


def test_the_agent_pauses_only_while_the_conversation_is_being_handled() -> None:
    """Requested and assigned hold the conversation; released invites the agent
    back; resolved is a closed conversation the assistant must not reopen."""
    assert is_agent_paused(HandoffStatus.REQUESTED) is True
    assert is_agent_paused(HandoffStatus.ASSIGNED) is True
    # A release is the explicit invitation that resumes the graph.
    assert is_agent_paused(HandoffStatus.QUEUED) is False
    # A closed conversation stays closed: the gate must not silently start
    # answering it again if the domain predicate is wired into the chat path.
    assert is_agent_paused(HandoffStatus.RESOLVED) is True
    assert is_agent_paused(HandoffStatus.CANCELLED) is False


def test_the_pause_predicate_and_the_visitor_notice_agree() -> None:
    """A visitor sees a notice exactly when the agent is paused.

    The chat gate derives the notice from the pause predicate, so the two must
    never disagree; this pins the agreement so a drift fails the build.
    """
    for status in HandoffStatus:
        assert (visitor_state_notice(status) is not None) is is_agent_paused(status)


def test_the_queue_takeover_and_resolution_each_have_a_visitor_notice() -> None:
    assert "queue" in (visitor_state_notice(HandoffStatus.REQUESTED) or "")
    assert "with you" in (visitor_state_notice(HandoffStatus.ASSIGNED) or "")
    assert "closed" in (visitor_state_notice(HandoffStatus.RESOLVED) or "")


def test_no_notice_carries_a_staff_identity_or_a_queue_position() -> None:
    for status in HandoffStatus:
        notice = visitor_state_notice(status)
        if notice is None:
            continue
        lowered = notice.casefold()
        assert "operator" not in lowered
        assert "staff member" not in lowered or "member of the team" in notice
        assert "position" not in lowered
        assert "number" not in lowered


def test_a_released_or_cancelled_handoff_publishes_no_visitor_notice() -> None:
    assert visitor_state_notice(HandoffStatus.QUEUED) is None
    assert visitor_state_notice(HandoffStatus.CANCELLED) is None


def test_the_transcript_notices_track_staff_transitions_without_names() -> None:
    assert "joined" in (state_notice(HandoffStatus.ASSIGNED) or "")
    assert "released" in (state_notice(HandoffStatus.QUEUED) or "")
    assert "closed" in (state_notice(HandoffStatus.RESOLVED) or "")
    assert state_notice(HandoffStatus.REQUESTED) is None
    assert state_notice(HandoffStatus.CANCELLED) is None
