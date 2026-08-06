"""Deterministic claim validation: prices and sensitive business facts (`RAG-007`).

The guard that matters: an answer's dollar amounts and coverage/permit
statements must be provable from the exact evidence the answer was given, with
no reliance on the model's honesty.
"""

from __future__ import annotations

from tenantchat.core.claims import (
    ClaimKind,
    ClaimVerdict,
    sensitive_claims,
    validate_sensitive_claims,
)

EVIDENCE = (
    "Clearview HVAC maintenance is $120 per visit. Repairs are covered by your"
    " home warranty only when the unit is under warranty.",
)


def test_quoted_price_present_in_evidence_is_supported() -> None:
    validation = validate_sensitive_claims("Our diagnostic visit is $120.", evidence_texts=EVIDENCE)
    assert validation.verdict is ClaimVerdict.SUPPORTED
    assert validation.unsupported == ()


def test_fabricated_price_is_unsupported() -> None:
    validation = validate_sensitive_claims("Our diagnostic visit is $89.", evidence_texts=EVIDENCE)
    assert validation.verdict is ClaimVerdict.UNSUPPORTED
    assert len(validation.unsupported) == 1
    assert validation.unsupported[0].kind is ClaimKind.PRICE
    assert validation.unsupported[0].value == "$89"


def test_price_grounded_in_trusted_prices_without_evidence_is_supported() -> None:
    validation = validate_sensitive_claims(
        "Our diagnostic visit is $120.",
        evidence_texts=(),
        trusted_prices=("$120 diagnostic visit",),
    )
    assert validation.verdict is ClaimVerdict.SUPPORTED


def test_every_price_is_validated_not_just_the_first() -> None:
    validation = validate_sensitive_claims(
        "Our diagnostic visit is $120 and the tune-up is $75.", evidence_texts=EVIDENCE
    )
    assert validation.verdict is ClaimVerdict.UNSUPPORTED
    assert [claim.value for claim in validation.unsupported] == ["$75"]


def test_coverage_claim_supported_by_evidence_is_allowed() -> None:
    validation = validate_sensitive_claims(
        "Repairs are covered by your home warranty only when the unit is under warranty.",
        evidence_texts=EVIDENCE,
    )
    assert validation.verdict is ClaimVerdict.SUPPORTED


def test_coverage_claim_invented_beyond_evidence_is_unsupported() -> None:
    validation = validate_sensitive_claims(
        "Every repair is covered at no cost to you.", evidence_texts=EVIDENCE
    )
    assert validation.verdict is ClaimVerdict.UNSUPPORTED
    assert validation.unsupported[0].kind is ClaimKind.COVERAGE


def test_permit_claim_without_evidence_is_unsupported() -> None:
    validation = validate_sensitive_claims(
        "Window cleaning needs no permit in Portland.", evidence_texts=EVIDENCE
    )
    assert validation.verdict is ClaimVerdict.UNSUPPORTED
    assert validation.unsupported[0].kind is ClaimKind.PERMIT


def test_sensitive_claims_enumerates_prices_and_keyword_sentences() -> None:
    answer = (
        "The diagnostic is $120 and the premium service costs $240."
        " Repairs are fully covered. A permit is required."
    )
    claims = sensitive_claims(answer)
    kinds = [claim.kind for claim in claims]
    assert kinds == [ClaimKind.PRICE, ClaimKind.PRICE, ClaimKind.COVERAGE, ClaimKind.PERMIT]


def test_plain_answer_without_sensitive_claims_is_supported() -> None:
    validation = validate_sensitive_claims("We will arrive on Tuesday morning.", evidence_texts=())
    assert validation.verdict is ClaimVerdict.SUPPORTED
