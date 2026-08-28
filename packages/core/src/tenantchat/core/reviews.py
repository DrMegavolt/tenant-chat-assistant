"""The `FEAT-008` review-queue domain rules: what fails, how it is ranked, and
how a reviewer's diagnosis overlay stays distinct from the detector's.

Everything here is a pure function over typed inputs — never a store write and
never a provider call. The queue's decision procedure is deliberately
deterministic: two operators reviewing the same turn from the same store state
must reach the same verdict, which is what lets an audit trail answer "who
decided, and against what".

The priority score is the documented formula the queue sorts by:

``score = 10 * min(severity, 3) + 5 * min(recurrence, 3) + 3 * committed + 2 * novel``

- **severity** (0..3) — the most severe technical cause among the turn's
  automatic diagnoses: ``provider_failure``/``application_error`` are 3,
  ``ingestion_or_index_error``/``tool_error`` are 2, anything else the
  detector can prove is 1, and a turn with no automatic diagnosis scores 0.
  The cap keeps a single cause from dominating the whole formula.
- **recurrence** (0..3) — how many earlier queue entries this tenant has for
  the same component-manifest hash, so a build that keeps failing outranks a
  one-off even when the failure itself is mundane.
- **committed** (0/1) — whether the turn committed a business action (booking,
  lead, handoff): a failure at the point of sale outranks a browse.
- **novel** (0/1) — whether the manifest hash is new to this tenant's review
  queue, i.e. the failure may be a regression the current candidate shipped.

The score ranges 0..50 as an integer; ties break by enqueue time, oldest
first, so the ordering depends on no id and no sub-second clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from tenantchat.core.contact import EMAIL_IN_TEXT, PHONE_IN_TEXT
from tenantchat.core.errors import ValidationError

# The causes that make a turn a technical failure rather than a quality
# disagreement. These are the detector's proof-point causes only: a
# ``suspected`` or ``inconclusive`` record is a review note, not a queue item.
TECHNICAL_CAUSES: Final[frozenset[str]] = frozenset(
    {
        "provider_failure",
        "application_error",
        "ingestion_or_index_error",
        "tool_error",
    }
)

# Statuses that count as the detector having *proved* the cause.
CONFIRMING_STATUSES: Final[frozenset[str]] = frozenset({"detected", "confirmed"})

# Severity per technical cause; anything not listed is 1. Bounded by the cap
# in :func:`priority_score`, so extending this table cannot unbalance the sort.
_CAUSE_SEVERITY: Final[Mapping[str, int]] = {
    "provider_failure": 3,
    "application_error": 3,
    "ingestion_or_index_error": 2,
    "tool_error": 2,
}

# The promotion PII check reads the shared free-text recognition rules from
# :mod:`tenantchat.core.contact` — the same source erasure reads — so the
# promotion gate and the erasure worker cannot silently disagree about what
# counts as contact data.


class FeedbackRating(StrEnum):
    """How the visitor rated one turn, as a closed value."""

    UP = "up"
    DOWN = "down"


class ReviewSource(StrEnum):
    """Why a turn entered the queue: the visitor said so, or the detector did."""

    USER_FEEDBACK = "user_feedback"
    AUTOMATIC = "automatic"


class ReviewStatus(StrEnum):
    """The review lifecycle; ``resolved`` is reachable only through an
    evaluation run that passed the promoted case (acceptance 5)."""

    OPEN = "open"
    IN_REVIEW = "in_review"
    AWAITING_FIX = "awaiting_fix"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class ReviewVerdict(StrEnum):
    """The reviewer's decision about the automatic diagnosis set as a whole.

    ``CONFIRMED`` and ``REJECTED`` are clean agreements and disagreements;
    ``AMENDED`` means the reviewer corrected or extended the set. The
    per-record decisions the verdict summarizes are stored row by row in
    :data:`DiagnosisDecision`, never inferred from it.
    """

    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    AMENDED = "amended"


class DiagnosisRelationship(StrEnum):
    """How one reviewer diagnosis record relates to the detector's record.

    ``CONFIRMS``/``REJECTS``/``AMENDS`` reference one automatic diagnosis by
    its index in the turn's ``diagnoses`` list; ``ADDS`` is a diagnosis the
    detector never emitted. Automatic and reviewer records stay separate rows
    everywhere — a disagreement is surfaced by the relationship, never by
    rewriting the original record.
    """

    CONFIRMS = "confirms"
    REJECTS = "rejects"
    AMENDS = "amends"
    ADDS = "adds"


def is_technical_failure(diagnoses: Sequence[Mapping[str, object]]) -> bool:
    """Whether the detector proved a technical cause on this turn.

    Only detector-proof statuses count (acceptance 3): a turn the detector
    merely suspects is a review candidate for a human, not an automatic queue
    entry. The automatic enqueue trigger in the chat surface calls this with
    the trace's own ``diagnoses`` list.
    """
    return any(
        str(diagnosis.get("cause")) in TECHNICAL_CAUSES
        and str(diagnosis.get("status")) in CONFIRMING_STATUSES
        for diagnosis in diagnoses
        if isinstance(diagnosis, Mapping)
    )


def technical_severity(causes: Sequence[str]) -> int:
    """The bounded severity of a cause list, for the priority formula."""
    if not causes:
        return 0
    return max((_CAUSE_SEVERITY.get(cause, 1) for cause in causes), default=1)


def priority_score(
    *,
    severity: int,
    recurrence: int,
    committed: bool,
    novel: bool,
) -> int:
    """The deterministic queue rank documented in the module docstring."""
    return (
        10 * min(severity, 3)
        + 5 * min(recurrence, 3)
        + (3 if committed else 0)
        + (2 if novel else 0)
    )


@dataclass(frozen=True, slots=True)
class DiagnosisDecision:
    """One reviewer decision about one automatic diagnosis, or a new one.

    ``automatic_index`` indexes the turn's stored ``diagnoses`` list and must
    be ``None`` exactly when the reviewer is adding a diagnosis the detector
    never emitted. The reviewer's replacement fields are required for an
    ``AMENDS`` row and meaningless for a ``CONFIRMS``/``REJECTS`` one.
    """

    automatic_index: int | None
    relationship: DiagnosisRelationship
    cause: str
    stage: str = "outcome"
    role: str = "primary"
    status: str = "confirmed"
    confidence: str = "medium"
    evidence: tuple[str, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewSubmission:
    """Everything a reviewer submits for one queue entry.

    ``status`` is the destination state: ``awaiting_fix`` when the reviewer
    documented a fix (the case stays visibly open until an evaluation run
    passes it), ``rejected`` when the reviewer dismissed the problem. The
    corrected answer is a new record beside the turn — the original trace is
    immutable and this text never overwrites it.
    """

    verdict: ReviewVerdict
    status: ReviewStatus
    decisions: tuple[DiagnosisDecision, ...] = ()
    note: str | None = None
    corrected_answer: str | None = None
    proposed_fix: str | None = None

    def __post_init__(self) -> None:
        if self.status not in (ReviewStatus.AWAITING_FIX, ReviewStatus.REJECTED):
            raise ValueError("a review submission must end awaiting_fix or rejected")


def validate_decisions(*, automatic_count: int, submission: ReviewSubmission) -> None:
    """Check a submission's decisions cover the automatic diagnosis set exactly.

    Raises:
        ValidationError: an index is out of range, a relationship contradicts
            its index, an ``AMENDS`` row carries no replacement fields, or the
            automatic set is left uncovered — the acceptance that a reviewer
            confirms, rejects, or amends *every* diagnosis record.
    """
    for decision in submission.decisions:
        if decision.relationship is DiagnosisRelationship.ADDS:
            if decision.automatic_index is not None:
                raise ValidationError(detail="an added diagnosis must not name an automatic index")
            continue
        if decision.automatic_index is None:
            raise ValidationError(detail="a confirm/reject/amend decision must name an index")
        if not 0 <= decision.automatic_index < automatic_count:
            raise ValidationError(detail="diagnosis decision index out of range")
        if decision.relationship is DiagnosisRelationship.AMENDS and not (
            decision.cause and decision.stage
        ):
            raise ValidationError(detail="an amended diagnosis must carry replacement fields")
    if automatic_count == 0:
        if submission.verdict is ReviewVerdict.CONFIRMED:
            raise ValidationError(detail="nothing automatic to confirm on this turn")
        if any(
            decision.relationship is not DiagnosisRelationship.ADDS
            for decision in submission.decisions
        ):
            raise ValidationError(detail="no automatic diagnosis on this turn to decide on")
        return
    covered = frozenset(
        decision.automatic_index
        for decision in submission.decisions
        if decision.automatic_index is not None
    )
    if covered != frozenset(range(automatic_count)):
        raise ValidationError(
            detail="every automatic diagnosis must be confirmed, rejected, or amended"
        )


def eval_case_payload(
    *,
    case_id: str,
    tenant_id: str,
    query: str,
    gold_chunk_ids: Sequence[str],
    citations: Sequence[str],
    scenario: str,
    expect_abstain: bool,
) -> dict[str, object]:
    """The anonymized evaluation case, shaped like the harness ``cases.json``.

    The payload mirrors :class:`evals.scorer.EvalCase` so the harness can load
    a promoted case with no shim: ``id``, ``tenant_id``, ``query``,
    ``gold_chunk_ids``, ``citations``, ``scenario``, ``expect_abstain``. The
    case id is server-minted from the review id, so a promotion is traceable
    back to the review that produced it.
    """
    return {
        "id": case_id,
        "tenant_id": tenant_id,
        "query": query,
        "gold_chunk_ids": list(gold_chunk_ids),
        "citations": list(citations),
        "scenario": scenario,
        "expect_abstain": expect_abstain,
    }


def payload_contains_pii(payload: Mapping[str, object]) -> bool:
    """Whether the promoted case still carries contact data.

    The privacy check for acceptance 6: promotion runs this over the case
    payload and refuses when it is true. Redacting markers instead of refusing
    would silently mutate a case the reviewer approved; refusing sends the
    reviewer back to anonymize the query.
    """
    for field in ("query", "scenario"):
        text = str(payload.get(field, ""))
        if PHONE_IN_TEXT.search(text) or EMAIL_IN_TEXT.search(text):
            return True
    return False
