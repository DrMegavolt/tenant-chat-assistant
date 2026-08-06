"""The `FEAT-008` review domain rules: what enqueues, how the queue sorts,
and what a review submission may contain.

These are the pure rules the store and the routes are built on: a turn's
technical-failure verdict, the documented priority formula, the coverage
requirement that makes automatic and reviewer diagnoses stay distinct, and
the privacy gate that promotion must pass.
"""

from __future__ import annotations

import pytest

from tenantchat.core.errors import ValidationError
from tenantchat.core.reviews import (
    DiagnosisDecision,
    DiagnosisRelationship,
    FeedbackRating,
    ReviewSource,
    ReviewStatus,
    ReviewSubmission,
    ReviewVerdict,
    eval_case_payload,
    is_technical_failure,
    payload_contains_pii,
    priority_score,
    technical_severity,
    validate_decisions,
)


def _diagnosis(cause: str, status: str = "detected") -> dict[str, object]:
    return {
        "cause": cause,
        "stage": "model",
        "role": "primary",
        "status": status,
        "confidence": "high",
        "evidence": [],
    }


class TestTechnicalFailure:
    def test_a_proven_provider_failure_is_technical(self) -> None:
        assert is_technical_failure([_diagnosis("provider_failure", "confirmed")])

    def test_a_suspected_technical_cause_does_not_enqueue(self) -> None:
        """A merely suspected cause is a review note, not a queue trigger."""
        assert not is_technical_failure([_diagnosis("provider_failure", "suspected")])

    def test_non_technical_causes_never_enqueue(self) -> None:
        assert not is_technical_failure([_diagnosis("grounding_or_citation_error")])

    def test_an_empty_diagnosis_set_is_not_a_failure(self) -> None:
        assert not is_technical_failure([])


class TestPriorityFormula:
    def test_severity_is_bounded_by_the_documented_cap(self) -> None:
        assert priority_score(severity=99, recurrence=0, committed=False, novel=False) == 30

    def test_recurrence_is_bounded_by_the_documented_cap(self) -> None:
        assert priority_score(severity=0, recurrence=99, committed=False, novel=False) == 15

    def test_a_committed_failure_outranks_a_browse(self) -> None:
        browsing = priority_score(severity=3, recurrence=1, committed=False, novel=False)
        committed = priority_score(severity=3, recurrence=1, committed=True, novel=False)
        assert committed == browsing + 3

    def test_a_novel_manifest_outranks_a_known_one(self) -> None:
        known = priority_score(severity=3, recurrence=2, committed=True, novel=False)
        novel = priority_score(severity=3, recurrence=2, committed=True, novel=True)
        assert novel == known + 2

    def test_the_score_is_a_deterministic_integer_in_range(self) -> None:
        """Two identical inputs produce the same 0..50 score."""
        first = priority_score(severity=2, recurrence=3, committed=True, novel=True)
        second = priority_score(severity=2, recurrence=3, committed=True, novel=True)
        assert first == second == 40
        assert 0 <= first <= 50

    def test_severity_mapping_is_bounded(self) -> None:
        assert technical_severity(("provider_failure", "application_error")) == 3
        assert technical_severity(("tool_error", "ingestion_or_index_error")) == 2
        assert technical_severity(("grounding_or_citation_error",)) == 1
        assert technical_severity(()) == 0


class TestSubmissionCoverage:
    def test_every_automatic_diagnosis_must_be_decided(self) -> None:
        """The acceptance that a reviewer confirms, rejects, or amends every
        diagnosis record — an uncovered index is a silently undecided record."""
        submission = ReviewSubmission(
            verdict=ReviewVerdict.CONFIRMED,
            status=ReviewStatus.AWAITING_FIX,
            decisions=(
                DiagnosisDecision(
                    automatic_index=0,
                    relationship=DiagnosisRelationship.CONFIRMS,
                    cause="provider_failure",
                ),
            ),
        )
        with pytest.raises(ValidationError):
            validate_decisions(automatic_count=2, submission=submission)

    def test_an_out_of_range_index_is_rejected(self) -> None:
        submission = ReviewSubmission(
            verdict=ReviewVerdict.CONFIRMED,
            status=ReviewStatus.REJECTED,
            decisions=(
                DiagnosisDecision(
                    automatic_index=3,
                    relationship=DiagnosisRelationship.REJECTS,
                    cause="provider_failure",
                ),
            ),
        )
        with pytest.raises(ValidationError):
            validate_decisions(automatic_count=1, submission=submission)

    def test_an_amendment_must_carry_replacement_fields(self) -> None:
        submission = ReviewSubmission(
            verdict=ReviewVerdict.AMENDED,
            status=ReviewStatus.AWAITING_FIX,
            decisions=(
                DiagnosisDecision(
                    automatic_index=0,
                    relationship=DiagnosisRelationship.AMENDS,
                    cause="",
                    stage="",
                ),
            ),
        )
        with pytest.raises(ValidationError):
            validate_decisions(automatic_count=1, submission=submission)

    def test_a_confirmed_verdict_needs_something_automatic(self) -> None:
        submission = ReviewSubmission(
            verdict=ReviewVerdict.CONFIRMED,
            status=ReviewStatus.REJECTED,
            decisions=(),
        )
        with pytest.raises(ValidationError):
            validate_decisions(automatic_count=0, submission=submission)

    def test_added_diagnoses_are_how_a_reviewer_disagrees_without_an_automatic_set(
        self,
    ) -> None:
        submission = ReviewSubmission(
            verdict=ReviewVerdict.AMENDED,
            status=ReviewStatus.AWAITING_FIX,
            decisions=(
                DiagnosisDecision(
                    automatic_index=None,
                    relationship=DiagnosisRelationship.ADDS,
                    cause="stale_source",
                    stage="retrieval",
                ),
            ),
        )
        validate_decisions(automatic_count=0, submission=submission)

    def test_a_complete_agreement_set_passes(self) -> None:
        submission = ReviewSubmission(
            verdict=ReviewVerdict.CONFIRMED,
            status=ReviewStatus.AWAITING_FIX,
            decisions=(
                DiagnosisDecision(
                    automatic_index=0,
                    relationship=DiagnosisRelationship.CONFIRMS,
                    cause="provider_failure",
                ),
                DiagnosisDecision(
                    automatic_index=1,
                    relationship=DiagnosisRelationship.CONFIRMS,
                    cause="tool_error",
                ),
            ),
        )
        validate_decisions(automatic_count=2, submission=submission)

    def test_an_added_row_may_not_name_an_automatic_index(self) -> None:
        submission = ReviewSubmission(
            verdict=ReviewVerdict.AMENDED,
            status=ReviewStatus.AWAITING_FIX,
            decisions=(
                DiagnosisDecision(
                    automatic_index=0,
                    relationship=DiagnosisRelationship.ADDS,
                    cause="stale_source",
                ),
            ),
        )
        with pytest.raises(ValidationError):
            validate_decisions(automatic_count=1, submission=submission)

    def test_the_submission_destination_is_closed(self) -> None:
        with pytest.raises(ValueError):
            ReviewSubmission(
                verdict=ReviewVerdict.CONFIRMED,
                status=ReviewStatus.RESOLVED,
            )


class TestPromotionPrivacy:
    def test_a_clean_case_passes_the_privacy_check(self) -> None:
        payload = eval_case_payload(
            case_id="review-1",
            tenant_id="clearview",
            query="What are your hours?",
            gold_chunk_ids=("clearview-hvac-2",),
            citations=(),
            scenario="reviewed-turn",
            expect_abstain=False,
        )
        assert not payload_contains_pii(payload)

    def test_a_query_carrying_a_phone_number_is_refused(self) -> None:
        """The acceptance-6 privacy check: promotion refuses contact data
        rather than silently redacting a case the reviewer approved."""
        payload = eval_case_payload(
            case_id="review-2",
            tenant_id="clearview",
            query="Call 555-222-1919 about my furnace",
            gold_chunk_ids=(),
            citations=(),
            scenario="reviewed-turn",
            expect_abstain=False,
        )
        assert payload_contains_pii(payload)

    def test_a_query_carrying_an_email_is_refused(self) -> None:
        payload = eval_case_payload(
            case_id="review-3",
            tenant_id="clearview",
            query="Email dana@example.com the quote",
            gold_chunk_ids=(),
            citations=(),
            scenario="reviewed-turn",
            expect_abstain=False,
        )
        assert payload_contains_pii(payload)

    def test_the_payload_shapes_like_the_harness_case(self) -> None:
        """A promoted case must load through `EvalCase.from_json` unchanged."""
        payload = eval_case_payload(
            case_id="review-9",
            tenant_id="apex",
            query="Do you service Portland?",
            gold_chunk_ids=("apex-hvac-1",),
            citations=("apex-hvac-1",),
            scenario="reviewed-turn",
            expect_abstain=False,
        )
        assert payload["id"] == "review-9"
        assert payload["tenant_id"] == "apex"
        assert payload["gold_chunk_ids"] == ["apex-hvac-1"]
        assert payload["citations"] == ["apex-hvac-1"]
        assert payload["expect_abstain"] is False


class TestClosedVocabularies:
    def test_feedback_ratings_are_closed(self) -> None:
        assert {FeedbackRating.UP.value, FeedbackRating.DOWN.value} == {"up", "down"}

    def test_queue_sources_are_closed(self) -> None:
        assert {source.value for source in ReviewSource} == {"user_feedback", "automatic"}

    def test_queue_statuses_are_closed(self) -> None:
        assert {status.value for status in ReviewStatus} == {
            "open",
            "in_review",
            "awaiting_fix",
            "rejected",
            "resolved",
        }
