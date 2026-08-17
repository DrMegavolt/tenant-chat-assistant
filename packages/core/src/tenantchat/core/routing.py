"""The structured intent router (`AGENT-001`).

The prototype routed queries by keyword filters — a question mentioning
"financing" went to the financing agent and everything else went to the
dispatcher. That is a single winner with no record of the losers: when the
wrong agent answers, nothing says whether the right intent was never a
candidate, was scored and lost, or lost to a threshold.

This module replaces it with a scored, versioned routing policy. A
:class:`RoutingDecision` records *every* candidate intent with its score, the
chosen intent (when one is chosen), the confidence, the policy version, and the
thresholds that were applied. `OBS-004` reads that record to classify a turn as
a ``routing_error`` or something else, and the record alone must be enough to
answer "was the correct intent never a candidate, scored and lost, or below
threshold".

The policy is deterministic: the same message against the same policy version
produces the same decision, byte for byte. Routing is the one place a stochastic
model is a liability — a misrouted turn is only diagnosable if the same turn
would misroute identically on replay — so no model call happens here.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from enum import StrEnum

_INTENT_ORDER: dict[str, int] = {}


class IntentName(StrEnum):
    """The closed set of intents the router can choose.

    Closed so that a message is never routed to an intent nothing can handle;
    adding an intent is a reviewable change to this enum, the routing policy,
    and the agent registry together.
    """

    GENERAL = "general"
    SERVICE_AREA = "service_area"
    AVAILABILITY = "availability"
    BOOKING = "booking"
    LEAD = "lead"
    HANDOFF = "handoff"
    CANCEL = "cancel"


for _index, _member in enumerate(IntentName._member_map_.values()):
    _INTENT_ORDER[_member.value] = _index


class RoutingOutcome(StrEnum):
    """What the router decided to *do* with the turn."""

    DIRECT = "direct"
    CLARIFY = "clarify"
    HANDOFF = "handoff"


class RoutingRule(StrEnum):
    """Why the outcome was chosen — the diagnosable part of the decision.

    ``MATCHED`` and ``FALLBACK`` are both direct routes; the distinction is
    whether the message carried evidence for the chosen intent. A misrouted
    turn with ``rule=FALLBACK`` and ``chosen=general`` is a routing gap to fix
    in the policy; one with ``rule=MATCHED`` is evidence the policy scored it
    wrong. ``CONTINUATION`` records that the active workflow's intent was kept
    despite a weak signal, which is what makes "the same message, different
    route" explainable across workflow state. ``BOUNDED_CLARIFY`` means the
    previous turn already asked and this one is still ambiguous.
    """

    MATCHED = "matched"
    CONTINUATION = "continuation"
    FALLBACK = "fallback"
    CLARIFY = "clarify"
    BOUNDED_CLARIFY = "bounded_clarify"


@dataclass(frozen=True, slots=True)
class IntentSignal:
    """One pattern that contributes evidence for an intent.

    ``weight`` is positive evidence in raw units: the router scores an intent
    by how much of its signal set the message matched, so a message that hits
    several weak signals can beat one that hits a single strong one.
    """

    signal_id: str
    pattern: re.Pattern[str]
    weight: int


@dataclass(frozen=True, slots=True)
class IntentProfile:
    """One intent's evidence set."""

    intent: IntentName
    signals: tuple[IntentSignal, ...]


@dataclass(frozen=True, slots=True)
class IntentCandidate:
    """One scored candidate in a routing decision."""

    intent: IntentName
    score: float
    matched_signals: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    """The complete outcome of one routing pass.

    This whole value is what gets persisted per turn — not just the winner —
    because it is what distinguishes a misroute from a retrieval failure in the
    `OBS-004` taxonomy.
    """

    policy_version: str
    candidates: tuple[IntentCandidate, ...]
    chosen: IntentName | None
    confidence: float
    outcome: RoutingOutcome
    rule: RoutingRule
    direct_threshold: float
    clarify_threshold: float
    conflict_gap: float


def _compile(signal_id: str, pattern: str, weight: int) -> IntentSignal:
    return IntentSignal(
        signal_id=signal_id,
        pattern=re.compile(pattern, re.IGNORECASE),
        weight=weight,
    )


def _day(signal_id: str, day: str) -> IntentSignal:
    return _compile(signal_id, rf"\b{day}\b", 2)


# Days and times are shared evidence for booking and availability: "book
# Tuesday" and "come Tuesday" are both about a calendar, and which intent wins
# depends on the rest of the message.
_CALENDAR_WORDS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
    "tomorrow",
    "this week",
    "next week",
    "morning",
    "afternoon",
    "evening",
)


def _calendar_signals(prefix: str) -> tuple[IntentSignal, ...]:
    return tuple(
        _day(f"{prefix}-{signal_id}", word) for signal_id, word in enumerate(_CALENDAR_WORDS)
    )


def _boost_workflow_continuation(
    scored: list[IntentCandidate],
    previous_intent: IntentName,
    bonus: float,
) -> list[IntentCandidate]:
    """Add a continuation bonus to the active workflow's candidate."""
    cancel = next((c for c in scored if c.intent is IntentName.CANCEL), None)
    if cancel is not None and cancel.score >= 6:
        return scored
    return [
        IntentCandidate(
            intent=candidate.intent,
            score=candidate.score + bonus,
            matched_signals=candidate.matched_signals,
        )
        if candidate.intent is previous_intent and candidate.score > 0
        else candidate
        for candidate in scored
    ]


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    """A versioned routing policy: evidence sets plus the decision thresholds.

    The version string is stored against every decision, so a behavior change
    is a new version with its own evidence and thresholds, never an edit to a
    version already referenced by stored records.
    """

    version: str
    profiles: tuple[IntentProfile, ...]
    direct_threshold: float
    clarify_threshold: float
    conflict_gap: float

    def route(
        self,
        message: str,
        *,
        previous_intent: IntentName | None = None,
        clarification_pending: bool = False,
        disabled_intents: Collection[IntentName] = (),
    ) -> RoutingDecision:
        """Score the message against every intent and decide.

        The decision procedure, in order:

        1. The top candidate is strong and clearly ahead -> route to it.
        2. A previous intent exists and either leads or nothing scored above
           the clarify floor -> continue it. Continuation is what makes "about
           Tuesday?" mid-booking keep the booking instead of handing the
           conversation off for a message that carries no evidence.
        3. General chat is the clear topic (hours, pricing, phone, address)
           -> route to general.
        4. A workflow is active and the top candidate is general -> continue
           the workflow. A greeting or a "thanks" must not suspend a booking
           the customer is mid-way through.
        5. Something scored above the clarify floor but not clearly enough
           -> ask which intent was meant.
        6. Nothing matched at all -> general, answered by the assistant.

        An active workflow receives a continuation bonus against its own
        intent's candidate so that a message supplying workflow fields (a
        ZIP code during a booking, an address during lead capture) does not
        displace the workflow to a different agent.

        ``disabled_intents`` names intents the tenant does not offer — booking
        (and its availability precursor) for a tenant with booking disabled.
        Their profiles are not scored at all: a capability the tenant lacks is
        not a candidate that lost, so the recorded decision simply does not
        contain it, and it can never be chosen or continued.

        ``clarification_pending`` records that the previous turn already asked;
        an answer that is still ambiguous becomes a handoff instead of a second
        question, so a customer cannot be asked forever.

        Args:
            message: the visitor's latest message.
            previous_intent: the intent of the session's active workflow, if
                any. Makes the same message route differently across workflow
                state by design, and deterministically.
            clarification_pending: whether the previous turn was a clarification.
            disabled_intents: intents the tenant's capability policy forbids;
                these are never scored, chosen, or continued.

        Returns:
            The full decision: every candidate, the chosen intent, the
            confidence, and the thresholds that were applied.
        """
        scored = [
            _score_candidate(message, profile)
            for profile in self.profiles
            if profile.intent not in disabled_intents
        ]
        scored.sort(key=lambda candidate: (-candidate.score, _INTENT_ORDER[candidate.intent.value]))

        if previous_intent is not None:
            scored = _boost_workflow_continuation(scored, previous_intent, self.direct_threshold)
            scored.sort(
                key=lambda candidate: (-candidate.score, _INTENT_ORDER[candidate.intent.value])
            )

        top, second = scored[0], scored[1]
        gap = top.score - second.score

        chosen: IntentName | None
        if top.score >= self.direct_threshold and gap >= self.conflict_gap:
            chosen = top.intent
            outcome = RoutingOutcome.DIRECT
            rule = (
                RoutingRule.CONTINUATION
                if previous_intent is not None and top.intent is previous_intent
                else RoutingRule.MATCHED
            )
        elif (
            previous_intent is not None
            and previous_intent not in disabled_intents
            and (top.intent is previous_intent or top.score < self.clarify_threshold)
        ):
            rule = RoutingRule.CONTINUATION
            chosen = previous_intent
            outcome = RoutingOutcome.DIRECT
        elif top.intent is IntentName.GENERAL and top.score >= self.direct_threshold:
            rule = RoutingRule.MATCHED
            chosen = IntentName.GENERAL
            outcome = RoutingOutcome.DIRECT
        elif (
            previous_intent is not None
            and previous_intent not in disabled_intents
            and top.intent is IntentName.GENERAL
        ):
            rule = RoutingRule.CONTINUATION
            chosen = previous_intent
            outcome = RoutingOutcome.DIRECT
        elif top.score >= self.clarify_threshold:
            rule = RoutingRule.CLARIFY
            chosen = None
            outcome = RoutingOutcome.CLARIFY
        else:
            rule = RoutingRule.FALLBACK
            chosen = IntentName.GENERAL
            outcome = RoutingOutcome.DIRECT

        if clarification_pending and rule is RoutingRule.CLARIFY:
            rule = RoutingRule.BOUNDED_CLARIFY
            chosen = None
            outcome = RoutingOutcome.HANDOFF

        return RoutingDecision(
            policy_version=self.version,
            candidates=tuple(scored),
            chosen=chosen,
            confidence=top.score,
            outcome=outcome,
            rule=rule,
            direct_threshold=self.direct_threshold,
            clarify_threshold=self.clarify_threshold,
            conflict_gap=self.conflict_gap,
        )


def _score_candidate(message: str, profile: IntentProfile) -> IntentCandidate:
    matched: list[str] = []
    score = 0.0
    for signal in profile.signals:
        if signal.pattern.search(message):
            matched.append(signal.signal_id)
            score += signal.weight
    return IntentCandidate(intent=profile.intent, score=score, matched_signals=tuple(matched))


_INTENT_QUESTION = {
    IntentName.SERVICE_AREA: "check whether we serve your area",
    IntentName.AVAILABILITY: "check appointment availability",
    IntentName.BOOKING: "book an appointment",
    IntentName.LEAD: "request a callback",
    IntentName.HANDOFF: "speak to a person",
    IntentName.GENERAL: "ask about something else",
    IntentName.CANCEL: "cancel a request",
}


def clarify_question(decision: RoutingDecision) -> str:
    """The deterministic question asked when the router is unsure.

    Written from the two strongest candidates so the customer's answer can be
    routed; it quotes no content from the message, only intent labels.
    """
    top = decision.candidates[0].intent
    second = decision.candidates[1].intent
    return (
        "I want to make sure I help with the right thing — did you mean to "
        f"{_INTENT_QUESTION[top]}, or {_INTENT_QUESTION[second]}?"
    )


ROUTING_POLICY_VERSION = "intent-routing@1"


def _build_policy() -> RoutingPolicy:
    general = (
        _compile("greeting", r"\b(?:hello|hi|hey|good morning|good afternoon|good evening)\b", 2),
        _compile("thanks", r"\b(?:thank|thanks|thx)\b", 1),
        _compile("hours", r"\b(?:hours?|opening times)\b", 4),
        _compile("open", r"\bopen\b", 4),
        _compile("closed", r"\b(?:closed|closing|close)\b", 2),
        _compile("when-are-you", r"\bwhen are you\b", 4),
        _compile(
            "pricing",
            r"\b(?:price|prices|pricing|cost|how much|quote|estimate|rate|charge|fee)\b",
            4,
        ),
        _compile("phone", r"\b(?:phone|number)\b", 2),
        _compile("call", r"\bcall\b", 1),
        _compile("address", r"\b(?:address|located|location|directions|where are you)\b", 2),
        _compile("services", r"\b(?:services|offer|do you do|what do you do)\b", 2),
        _compile("help", r"\b(?:help|question)\b", 1),
    )
    service_area = (
        _compile("zip", r"\b\d{5}\b", 4),
        _compile("serve", r"\b(?:serve|serves|served)\b", 3),
        _compile("coverage", r"\b(?:coverage|cover|service area)\b", 3),
        _compile("near", r"\b(?:near me|in my area|my zip)\b", 2),
    )
    availability = (
        _compile("available", r"\b(?:available|availability)\b", 3),
        _compile("when-can", r"\bwhen (?:can|do|does|are)\b", 3),
        # `times` without an "s": a bare "what time" is a general hours
        # question, while "times/slots/openings" is availability evidence.
        _compile("slots", r"\b(?:slots?|openings?|times)\b", 3),
        _compile("visit", r"\b(?:come|visit|stop by)\b", 2),
        *_calendar_signals("av"),
    )
    booking = (
        _compile("book", r"\b(?:book|booking|reserve|reservation|schedule|appointment)\b", 4),
        # Service words are shared evidence with pricing questions ("how much
        # for a repair?"), so they weigh less than an explicit booking verb.
        _compile(
            "service-work",
            r"\b(?:repair|fix|install|replace|maintenance|service call|tune-up)\b",
            2,
        ),
        _compile(
            "service-category",
            r"\b(?:hvac|heating|cooling|a/?c|furnace|plumbing|plumber|leak|electrical|electric|wiring|window|roof)\b",
            2,
        ),
        _compile("need", r"\b(?:i need|need a|need an|need to|we need)\b", 3),
        *_calendar_signals("bk"),
    )
    lead = (
        _compile(
            "callback",
            r"\b(?:call me|call back|callback|have someone call|someone call|reach me|follow ?up"
            r"|contact me|get back to me)\b",
            6,
        ),
        _compile("quote-request", r"\b(?:need a callback|request a callback|call me back)\b", 6),
        _compile("waiting", r"\b(?:waiting on|pending)\b", 2),
    )
    handoff = (
        _compile("person", r"\b(?:person|human|representative|manager|real person)\b", 4),
        _compile("talk", r"\b(?:talk to|speak to|speak with)\b", 4),
        _compile("escalate", r"\b(?:escalate|complaint|talk to a manager)\b", 4),
    )
    cancel = (
        # Heavier than the booking verbs so "cancel the booking" cancels: the
        # booking-verb echo must not outweigh the explicit instruction.
        _compile("cancel", r"\b(?:cancel|cancellation)\b", 6),
        _compile(
            "abandon",
            r"\b(?:never ?mind|forget it|scratch that|hold off|abort|stop the)\b",
            6,
        ),
    )
    profiles = (
        IntentProfile(IntentName.GENERAL, general),
        IntentProfile(IntentName.SERVICE_AREA, service_area),
        IntentProfile(IntentName.AVAILABILITY, availability),
        IntentProfile(IntentName.BOOKING, booking),
        IntentProfile(IntentName.LEAD, lead),
        IntentProfile(IntentName.HANDOFF, handoff),
        IntentProfile(IntentName.CANCEL, cancel),
    )
    return RoutingPolicy(
        version=ROUTING_POLICY_VERSION,
        profiles=profiles,
        direct_threshold=4.0,
        clarify_threshold=2.5,
        conflict_gap=2.0,
    )


# The policy the deployed runtime routes with. A change to any signal or
# threshold is a new version, never an edit here.
ROUTING_POLICY = _build_policy()
