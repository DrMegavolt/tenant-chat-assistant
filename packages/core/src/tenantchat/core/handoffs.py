"""The `FEAT-004` staff-handoff lifecycle rules.

Everything here is a pure function over typed values — never a store write and
never a network call. The ownership transaction itself (who wins a race to
accept) lives in the store behind an atomic conditional update, because the
rule "exactly one staff owner" is only true if the database enforces it; this
module defines the state machine and the visitor-facing copy that machine is
allowed to publish.

A handoff moves through the five ``handoff_status`` values the schema has
carried since `BASE-001`:

- ``requested`` — freshly opened by the assistant; no staff owner yet.
- ``assigned`` — one staff member currently owns the conversation.
- ``queued`` — a previous owner released it; a staff member can still take it,
  and the assistant may answer again in the meantime.
- ``resolved`` — the conversation was closed by staff; terminal.
- ``cancelled`` — terminal, reserved for future operator tooling.

The automated agent is paused only while the conversation is under an active
handoff nobody has stepped back from — ``requested`` or ``assigned``. Releasing
is the explicit invitation that lets the graph resume, which is the guarantee
`FEAT-004` documents as "a queued turn resuming after release commits nothing
twice": the pause gate is lifted by the release, and the graph's idempotent
services keep the resumed turn from committing a second time.

The visitor-facing notices are the one piece of this module that becomes user
content, so they are bounded copy with no staff identity and no queue position:
a visitor can tell whether they are waiting, are with someone, or are done —
never who, and never where in line.
"""

from __future__ import annotations

from enum import StrEnum


class HandoffStatus(StrEnum):
    """The closed vocabulary of a handoff's lifecycle."""

    REQUESTED = "requested"
    QUEUED = "queued"
    ASSIGNED = "assigned"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"


# Statuses an accept may leave: an unowned handoff is takeable.
ACCEPTABLE_STATUSES: frozenset[HandoffStatus] = frozenset(
    {HandoffStatus.REQUESTED, HandoffStatus.QUEUED}
)

# Statuses a resolve may leave: any open handoff can be closed.
RESOLVABLE_STATUSES: frozenset[HandoffStatus] = frozenset(
    {HandoffStatus.REQUESTED, HandoffStatus.QUEUED, HandoffStatus.ASSIGNED}
)


def is_agent_paused(status: HandoffStatus) -> bool:
    """Whether the automated agent must stay quiet for this handoff.

    ``requested`` and ``assigned`` are the states where the conversation is in
    a person's hands or awaiting one; ``queued`` is the state a release leaves,
    and it is the one place the assistant is explicitly invited back.
    """
    return status in (HandoffStatus.REQUESTED, HandoffStatus.ASSIGNED)


def visitor_state_notice(status: HandoffStatus) -> str | None:
    """The identity-free notice a visitor sees for a paused or closed handoff.

    Returns ``None`` when the handoff does not hold the conversation: a
    ``queued`` handoff has been released and the assistant answers again, and a
    cancelled handoff is an operator artifact the visitor should not learn
    about. Never names a staff member and never a queue position.
    """
    if status is HandoffStatus.REQUESTED:
        return (
            "You're in the queue for a member of the team to join this "
            "conversation. You can keep sending messages and they'll be read."
        )
    if status is HandoffStatus.ASSIGNED:
        return "A member of the team is now with you in this conversation."
    if status is HandoffStatus.RESOLVED:
        return "This conversation is now closed. If you need anything else, " "start a new chat."
    return None


def state_notice(status: HandoffStatus) -> str | None:
    """The server-authored transcript notice a staff transition writes.

    Written with the ``system`` role next to the state change itself, so the
    transcript a staff member and a returning visitor read carries the same
    context the turn responses carry. Like :func:`visitor_state_notice`, this
    never names a staff member.
    """
    if status is HandoffStatus.ASSIGNED:
        return "A member of the team has joined this conversation."
    if status is HandoffStatus.QUEUED:
        return (
            "The conversation has been released back to the assistant. A "
            "member of the team can rejoin at any time."
        )
    if status is HandoffStatus.RESOLVED:
        return "This conversation has been closed. Thank you for your patience."
    return None
