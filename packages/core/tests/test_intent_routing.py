"""The structured intent router: determinism, diagnosis, and thresholds.

These are the acceptance-criterion tests for `AGENT-001` routing: the same
message routes identically under a versioned policy, and a misroute is
diagnosable from the decision record alone. The record assertions matter as
much as the route assertions — the whole decision (every candidate, the scores,
the chosen intent, the thresholds) is what `OBS-004` later classifies a turn
against.
"""

from __future__ import annotations

from tenantchat.core.routing import (
    ROUTING_POLICY,
    ROUTING_POLICY_VERSION,
    IntentName,
    RoutingDecision,
    RoutingOutcome,
    RoutingRule,
    clarify_question,
)


def route(message: str, **kwargs: object) -> RoutingDecision:
    return ROUTING_POLICY.route(message, **kwargs)  # type: ignore[arg-type]


def _candidate(decision: RoutingDecision, intent: IntentName) -> float:
    for candidate in decision.candidates:
        if candidate.intent is intent:
            return candidate.score
    raise AssertionError(f"{intent.value} is not a candidate: {decision.candidates}")


def test_the_same_message_routes_identically_under_the_same_policy_version() -> None:
    """Determinism is the property that makes a misroute diagnosable.

    If the same message could route differently under the same versioned
    policy, a record of the decision would still leave "why did this turn go to
    the wrong agent?" unanswerable. Two passes must be byte-identical.
    """
    first = route("book HVAC on Monday")
    second = route("book HVAC on Monday")

    assert first == second
    assert first.policy_version == ROUTING_POLICY_VERSION


def test_a_booking_message_routes_to_booking_with_its_full_record() -> None:
    decision = route("book HVAC on Monday")

    assert decision.outcome is RoutingOutcome.DIRECT
    assert decision.rule is RoutingRule.MATCHED
    assert decision.chosen is IntentName.BOOKING
    assert decision.confidence == _candidate(decision, IntentName.BOOKING)
    assert decision.direct_threshold == 4.0
    assert decision.clarify_threshold == 2.5
    assert decision.conflict_gap == 2.0
    assert decision.candidates[0].intent is IntentName.BOOKING


def test_every_intent_is_a_scored_candidate_not_just_the_winner() -> None:
    """The record carries the losers, which is what diagnoses a misroute."""
    decision = route("do you serve 97205?")

    assert {candidate.intent for candidate in decision.candidates} == set(IntentName)
    assert all(candidate.score >= 0 for candidate in decision.candidates)
    assert decision.chosen is IntentName.SERVICE_AREA
    assert _candidate(decision, IntentName.SERVICE_AREA) > _candidate(decision, IntentName.BOOKING)


def test_a_general_question_routes_to_general_chat() -> None:
    for message in (
        "what are your hours?",
        "how much is an hvac repair?",
        "where are you located?",
        "do you offer window cleaning?",
    ):
        decision = route(message)
        assert decision.chosen is IntentName.GENERAL, message
        assert decision.outcome is RoutingOutcome.DIRECT, message


def test_an_availability_question_routes_to_availability() -> None:
    decision = route("when can you come out?")

    assert decision.chosen is IntentName.AVAILABILITY
    assert decision.outcome is RoutingOutcome.DIRECT


def test_a_callback_request_routes_to_a_lead() -> None:
    decision = route("have someone call me back")

    assert decision.chosen is IntentName.LEAD
    assert decision.outcome is RoutingOutcome.DIRECT


def test_a_request_for_a_person_routes_to_a_handoff() -> None:
    decision = route("let me talk to a person")

    assert decision.chosen is IntentName.HANDOFF
    assert decision.outcome is RoutingOutcome.DIRECT


def test_a_greeting_with_no_other_evidence_falls_back_to_general() -> None:
    """A greeting is not a handoff: the assistant answers it in prose."""
    decision = route("hello")

    assert decision.chosen is IntentName.GENERAL
    assert decision.rule is RoutingRule.FALLBACK
    assert decision.outcome is RoutingOutcome.DIRECT


def test_gibberish_falls_back_to_general_with_a_zero_confidence_record() -> None:
    """Unrecognizable text is answered, never guessed at by a tool agent."""
    decision = route("asdfghjkl")

    assert decision.chosen is IntentName.GENERAL
    assert decision.rule is RoutingRule.FALLBACK
    assert decision.confidence == 0.0


def test_a_conflicting_message_asks_a_clarification() -> None:
    """Booking and availability evidence close enough to tie -> ask, don't act."""
    decision = route("when can I schedule")

    assert decision.outcome is RoutingOutcome.CLARIFY
    assert decision.rule is RoutingRule.CLARIFY
    assert decision.chosen is None
    assert decision.confidence == _candidate(decision, IntentName.BOOKING)
    assert "book" in clarify_question(decision)


def test_a_second_consecutive_ambiguity_hands_off_instead_of_asking_again() -> None:
    """One clarification is generous; two is a loop the customer pays for."""
    decision = route("when can I schedule", clarification_pending=True)

    assert decision.outcome is RoutingOutcome.HANDOFF
    assert decision.rule is RoutingRule.BOUNDED_CLARIFY
    assert decision.chosen is None


def test_a_resolved_clarification_routes_directly() -> None:
    """An answer that names an intent after a clarification proceeds to it."""
    decision = route("book it", clarification_pending=True)

    assert decision.outcome is RoutingOutcome.DIRECT
    assert decision.rule is RoutingRule.MATCHED
    assert decision.chosen is IntentName.BOOKING


def test_a_weak_followup_continues_the_active_workflow() -> None:
    """Mid-booking, "about Tuesday?" must not abandon the booking."""
    decision = route("what about Tuesday?", previous_intent=IntentName.BOOKING)

    assert decision.outcome is RoutingOutcome.DIRECT
    assert decision.rule is RoutingRule.CONTINUATION
    assert decision.chosen is IntentName.BOOKING


def test_a_greeting_mid_workflow_continues_it() -> None:
    """A greeting must not suspend a booking the customer is mid-way through."""
    decision = route("thanks!", previous_intent=IntentName.BOOKING)

    assert decision.rule is RoutingRule.CONTINUATION
    assert decision.chosen is IntentName.BOOKING


def test_a_clear_new_topic_mid_workflow_is_a_topic_switch() -> None:
    """Strong evidence for another intent beats the active workflow."""
    decision = route("what are your hours?", previous_intent=IntentName.BOOKING)

    assert decision.rule is RoutingRule.MATCHED
    assert decision.chosen is IntentName.GENERAL


def test_a_service_area_question_mid_workflow_switches_topic() -> None:
    decision = route("do you serve 97205?", previous_intent=IntentName.BOOKING)

    assert decision.chosen is IntentName.SERVICE_AREA
    assert decision.outcome is RoutingOutcome.DIRECT


def test_cancel_is_always_recognized_even_mid_workflow() -> None:
    decision = route("cancel the booking", previous_intent=IntentName.BOOKING)

    assert decision.chosen is IntentName.CANCEL
    assert decision.outcome is RoutingOutcome.DIRECT
    assert decision.rule is RoutingRule.MATCHED


def test_the_candidate_scores_explain_a_lost_intent() -> None:
    """The diagnosis "scored and lost" must be readable off the record.

    The correct intent is present in the candidates with a real score; the
    record shows it was beaten, and by how much.
    """
    decision = route("book HVAC on Monday")

    assert decision.chosen is IntentName.BOOKING
    booking_score = _candidate(decision, IntentName.BOOKING)
    assert booking_score > 0
    assert decision.confidence == booking_score
    assert decision.confidence > decision.clarify_threshold
