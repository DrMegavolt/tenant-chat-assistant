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


def test_a_disabled_intent_is_not_a_candidate_and_cannot_be_chosen() -> None:
    """A tenant without a capability is not offered it, on any message.

    Booking routing for a booking-disabled tenant would hand the
    assistant the booking agent's context — "you may schedule once you collect
    these fields" — contradicting the tenant's policy. Removing the intent from
    the candidate set means the recorded decision honestly shows it never
    competed, and a continuation cannot revive it.
    """
    disabled = (IntentName.BOOKING, IntentName.AVAILABILITY)

    decision = route("book HVAC on Monday", disabled_intents=disabled)

    assert decision.chosen is IntentName.GENERAL
    assert all(candidate.intent not in disabled for candidate in decision.candidates)
    assert decision.outcome is RoutingOutcome.DIRECT
    assert decision.rule is RoutingRule.FALLBACK


def test_a_disabled_intent_cannot_be_continued_from_a_prior_workflow() -> None:
    """Even a suspended booking workflow cannot pull the turn back to booking."""
    disabled = (IntentName.BOOKING, IntentName.AVAILABILITY)

    decision = route(
        "what about Tuesday?", previous_intent=IntentName.BOOKING, disabled_intents=disabled
    )

    assert decision.chosen is not IntentName.BOOKING
    assert all(candidate.intent not in disabled for candidate in decision.candidates)


class TestDegenerateCandidateSets:
    """R-12: caller-supplied filtering could leave fewer than two scoreable
    intents, and the router once indexed `scored[1]` unguarded."""

    def test_disabling_every_intent_clarifies_instead_of_raising(self) -> None:
        decision = route("book HVAC on Monday", disabled_intents=set(IntentName))

        assert decision.candidates == ()
        assert decision.chosen is None
        assert decision.outcome is RoutingOutcome.CLARIFY
        assert decision.rule is RoutingRule.CLARIFY
        assert "right thing" in clarify_question(decision)

    def test_a_pending_clarification_over_an_empty_set_hands_off(self) -> None:
        """The bounded-clarify rule must survive a degenerate candidate set."""
        decision = route(
            "book HVAC on Monday", disabled_intents=set(IntentName), clarification_pending=True
        )

        assert decision.outcome is RoutingOutcome.HANDOFF
        assert decision.rule is RoutingRule.BOUNDED_CLARIFY

    def test_a_lone_strong_candidate_routes_directly(self) -> None:
        """One candidate has no competitor, so no conflict gap is required."""
        decision = route(
            "book HVAC on Monday",
            disabled_intents=set(IntentName) - {IntentName.BOOKING},
        )

        assert decision.chosen is IntentName.BOOKING
        assert decision.outcome is RoutingOutcome.DIRECT
        assert decision.rule is RoutingRule.MATCHED

    def test_a_lone_weak_candidate_falls_back_rather_than_raising(self) -> None:
        decision = route(
            "asdfghjkl",
            disabled_intents=set(IntentName) - {IntentName.BOOKING},
        )

        assert decision.chosen is IntentName.GENERAL
        assert decision.rule is RoutingRule.FALLBACK

    def test_a_lone_moderate_candidate_clarifies_without_a_second(self) -> None:
        """Between the clarify and direct thresholds with no competitor."""
        decision = route(
            "i need",
            disabled_intents=set(IntentName) - {IntentName.BOOKING},
        )

        assert decision.outcome is RoutingOutcome.CLARIFY
        assert decision.chosen is None
        assert "right thing" in clarify_question(decision)


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


def test_a_financing_question_reaches_the_knowledge_agent() -> None:
    """The seeded demo knowledge is financing policy; nothing else can serve it.

    Without a financing signal the strongest reading of "what financing options
    are available" is `availability` picking up "available", which clarifies
    instead of retrieving — leaving the only governed documents in the demo
    unreachable and every grounded answer citation-free.
    """
    decision = route("What financing options are available for a major HVAC replacement?")

    assert decision.chosen is IntentName.GENERAL
    assert decision.outcome is RoutingOutcome.DIRECT
    assert decision.rule is RoutingRule.MATCHED
    assert _candidate(decision, IntentName.AVAILABILITY) < decision.confidence


def test_financing_does_not_capture_a_real_availability_question() -> None:
    """The financing weight must not swamp the workflow intents beside it."""
    decision = route("Do you have any availability tomorrow?")

    assert decision.chosen is IntentName.AVAILABILITY
    assert decision.outcome is RoutingOutcome.DIRECT
