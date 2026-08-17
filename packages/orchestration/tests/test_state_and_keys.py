"""Checkpointed state, thread keys, and the connection string the saver wants."""

from __future__ import annotations

import json

import pytest

from tenantchat.orchestration.checkpoints import checkpoint_connection_string
from tenantchat.orchestration.runtime import thread_id
from tenantchat.orchestration.state import (
    DispatchState,
    initial_state,
    next_turn,
    reduce_model_invocations,
)


def test_the_whole_state_round_trips_through_json() -> None:
    """A checkpoint is serialized, and a resume happens after a release.

    Anything in here that is not plain data becomes a migration problem the
    first time a running conversation outlives the process that started it.
    """
    state = initial_state("clearview", "session-1", "hello")

    assert json.loads(json.dumps(state)) == state


def test_a_new_turn_clears_the_previous_turn_without_erasing_history() -> None:
    """The round budget and any pending confirmation are per-turn; the record is not."""
    update = next_turn("and one more thing")

    assert update["rounds"] == 0
    assert update["pending_booking"] is None
    assert update["booking_approved"] is False
    assert update["pending_lead"] is None
    assert update["lead_approved"] is False
    assert update["failure"] == ""
    assert "committed" not in update


def test_a_new_turn_advances_the_turn_index_by_one() -> None:
    """``turn_index`` accumulates, so a caller need not read the thread first.

    The value is a *delta*. A node returning an absolute number here would add
    it to the count instead of setting it, which is why no node returns it.
    """
    assert next_turn("hello")["turn_index"] == 1


def test_a_new_turn_clears_prior_model_invocations_but_rounds_accumulate() -> None:
    previous = [{"round": 1, "model_name": "primary"}]
    assert reduce_model_invocations(previous, []) == []
    assert reduce_model_invocations(previous, [{"round": 2}]) == [
        *previous,
        {"round": 2},
    ]


def test_a_transcript_entry_always_carries_both_optional_fields() -> None:
    """An absent key is a KeyError on the first resume across a release that added it."""
    state: DispatchState = initial_state("clearview", "session-1", "hello")

    assert state["transcript"][0]["tool_calls"] == []
    assert state["transcript"][0]["tool_call_id"] == ""


def test_a_thread_key_is_tenant_qualified() -> None:
    """A guessed session ID must not resume another tenant's conversation."""
    assert thread_id("clearview", "s-1") != thread_id("apex", "s-1")


@pytest.mark.parametrize(
    "session_id",
    ["", "has spaces", "../escape", "x" * 129],
    ids=["empty", "spaces", "traversal", "too-long"],
)
def test_an_unusable_session_id_is_refused(session_id: str) -> None:
    """The visitor controls this value until `SEC-002` issues a real credential."""
    with pytest.raises(ValueError, match="safe characters"):
        thread_id("clearview", session_id)


def test_a_sqlalchemy_url_is_rewritten_for_the_checkpointer() -> None:
    """The saver opens its own psycopg pool and rejects a driver-qualified scheme."""
    rewritten = checkpoint_connection_string(
        "postgresql+psycopg://app:secret@db.internal:5432/tenantchat"
    )

    assert rewritten == "postgresql://app:secret@db.internal:5432/tenantchat"


def test_a_plain_postgres_url_is_left_alone() -> None:
    assert (
        checkpoint_connection_string("postgresql://app@db.internal/tenantchat")
        == "postgresql://app@db.internal/tenantchat"
    )


def test_a_non_postgres_url_is_refused() -> None:
    """The checkpointer is PostgreSQL-only; a silent fallback would lose resumability."""
    with pytest.raises(ValueError, match="must be PostgreSQL"):
        checkpoint_connection_string("sqlite:///checkpoints.db")
