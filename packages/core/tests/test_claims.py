"""Deterministic claim validation: prices and sensitive business facts (`RAG-007`).

The guard that matters: an answer's dollar amounts and coverage/permit
statements must be provable from the exact evidence the answer was given, with
no reliance on the model's honesty.
"""

from __future__ import annotations

import json
import os
import subprocess

from tenantchat.core.claims import (
    Claim,
    ClaimKind,
    ClaimVerdict,
    answer_rests_only_on_tool_verdicts,
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


class TestThousandsSeparators:
    """R-11: the price regex once stopped at the comma, so "$1,500" became the
    claim "$1", which substring-grounded on any passage quoting "$1xx"."""

    def test_a_price_with_a_thousands_separator_is_claimed_whole(self) -> None:
        claims = sensitive_claims("The full replacement is $1,500.")

        assert [claim.value for claim in claims] == ["$1,500"]

    def test_a_comma_price_does_not_ground_on_a_different_price(self) -> None:
        """The reproduced failure: "$1" grounded on a $120 passage."""
        validation = validate_sensitive_claims(
            "The full replacement is $1,500.",
            evidence_texts=("A diagnostic is $120 per visit.",),
        )

        assert validation.verdict is ClaimVerdict.UNSUPPORTED

    def test_a_comma_price_grounds_on_evidence_quoting_the_same_amount(self) -> None:
        validation = validate_sensitive_claims(
            "The full replacement is $1,500.",
            evidence_texts=("A full system replacement is $1,500 installed.",),
        )

        assert validation.verdict is ClaimVerdict.SUPPORTED

    def test_a_comma_price_grounded_in_trusted_prices_is_supported(self) -> None:
        validation = validate_sensitive_claims(
            "The full replacement is $1,500.",
            evidence_texts=(),
            trusted_prices=("Full replacement: $1,500",),
        )

        assert validation.verdict is ClaimVerdict.SUPPORTED

    def test_a_decimal_price_is_claimed_whole(self) -> None:
        claims = sensitive_claims("The inspection is $89.50.")

        assert [claim.value for claim in claims] == ["$89.50"]


class TestWholeTokenGrounding:
    """Grounding compares complete monetary tokens, never substrings."""

    def test_a_price_does_not_ground_on_a_superset_amount(self) -> None:
        """A passage quoting $1,500.50 must not vouch for a $1,500 claim."""
        validation = validate_sensitive_claims(
            "The replacement is $1,500.",
            evidence_texts=("It costs $1,500.50 with tax.",),
        )

        assert validation.verdict is ClaimVerdict.UNSUPPORTED

    def test_a_short_price_does_not_ground_inside_a_longer_one(self) -> None:
        """Substring containment once grounded "$120" on a "$1200" passage."""
        validation = validate_sensitive_claims(
            "The visit is $120.",
            evidence_texts=("The premium repair starts at $1200.",),
        )

        assert validation.verdict is ClaimVerdict.UNSUPPORTED


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


def test_one_claim_per_sentence_kinded_by_its_earliest_keyword() -> None:
    """Five keywords in one sentence yield one claim, not five: every claim is
    the same sentence, so per-keyword records would only duplicate the bytes
    stored in ``claims_invalid``."""
    sentence = "Coverage is insured, bonded, licensed, and warranted."
    assert sensitive_claims(sentence) == (Claim(ClaimKind.COVERAGE, sentence),)


def test_claim_order_is_stable_across_python_hash_seeds() -> None:
    """PYTHONHASHSEED reorders set iteration between interpreters, and the
    claim order lands in the persisted turn record; the bytes must not depend
    on the interpreter that recorded them. The review proved the old
    frozenset intersection differed under seeds 1 and 42."""
    program = (
        "import json, sys\n"
        "from tenantchat.core.claims import sensitive_claims\n"
        "print(json.dumps([(claim.kind.value, claim.value)"
        " for claim in sensitive_claims(sys.argv[1])]))\n"
    )
    answer = "The premium is covered and insured. A permit is required. The price is $120."
    outcomes = set()
    for seed in ("1", "42"):
        # The same pinned toolchain the suite runs under, with the hash seed
        # the test controls; bandit's untrusted-input warning does not apply.
        result = subprocess.run(  # noqa: S603
            ["uv", "run", "--frozen", "python", "-c", program, answer],  # noqa: S607
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        )
        outcomes.add(result.stdout.strip())
    assert outcomes == {
        json.dumps(
            [
                ["price", "$120"],
                ["coverage", "The premium is covered and insured."],
                ["permit", "A permit is required."],
            ]
        )
    }


def test_plain_answer_without_sensitive_claims_is_supported() -> None:
    validation = validate_sensitive_claims("We will arrive on Tuesday morning.", evidence_texts=())
    assert validation.verdict is ClaimVerdict.SUPPORTED


class TestServiceAreaClaims:
    """Whether a ZIP is served is decided by tenant policy, never by a document.

    The service-area tool answered `served: true`, the model said so,
    and the validator refused the whole answer because no retrieved passage
    repeated it. Retrieval cannot ground these claims, so the tool's own
    verdict has to.
    """

    def test_a_confirmed_zip_supports_the_claim_that_names_it(self) -> None:
        validation = validate_sensitive_claims(
            "Yes, we serve ZIP code 97205.",
            evidence_texts=(),
            confirmed_service_areas={"97205": True},
        )
        assert validation.verdict is ClaimVerdict.SUPPORTED

    def test_a_yes_about_a_zip_the_tool_refused_is_unsupported(self) -> None:
        """The failure that must never become publishable: contradicting the tool."""
        validation = validate_sensitive_claims(
            "Yes, we serve ZIP code 97205.",
            evidence_texts=(),
            confirmed_service_areas={"97205": False},
        )
        assert validation.verdict is ClaimVerdict.UNSUPPORTED

    def test_a_zip_the_tool_never_checked_is_unsupported(self) -> None:
        """A verdict for one ZIP must not vouch for a different one."""
        validation = validate_sensitive_claims(
            "Yes, we serve ZIP code 98103.",
            evidence_texts=(),
            confirmed_service_areas={"97205": True},
        )
        assert validation.verdict is ClaimVerdict.UNSUPPORTED

    def test_a_truthful_negative_is_supported(self) -> None:
        validation = validate_sensitive_claims(
            "We do not serve 98999.",
            evidence_texts=(),
            confirmed_service_areas={"98999": False},
        )
        assert validation.verdict is ClaimVerdict.SUPPORTED

    def test_a_negative_contradicting_the_tool_is_unsupported(self) -> None:
        validation = validate_sensitive_claims(
            "We do not serve 98999.",
            evidence_texts=(),
            confirmed_service_areas={"98999": True},
        )
        assert validation.verdict is ClaimVerdict.UNSUPPORTED

    def test_a_claim_naming_no_zip_still_needs_evidence(self) -> None:
        """ "We serve your area" names nothing the tool decided."""
        validation = validate_sensitive_claims(
            "Yes, we serve your area.",
            evidence_texts=(),
            confirmed_service_areas={"97205": True},
        )
        assert validation.verdict is ClaimVerdict.UNSUPPORTED

    def test_a_passage_cannot_overrule_a_refusing_tool_verdict(self) -> None:
        """The tool owns coverage, so agreeing prose is not a second opinion.

        A tenant document that describes the service area in the answer's own
        words would otherwise clear the token-overlap threshold and publish a
        "yes" the tool had just refused.
        """
        validation = validate_sensitive_claims(
            "Yes, we serve ZIP code 97205.",
            evidence_texts=("Yes, we serve ZIP code 97205 and the surrounding metro.",),
            confirmed_service_areas={"97205": False},
        )
        assert validation.verdict is ClaimVerdict.UNSUPPORTED

    def test_a_passage_cannot_vouch_for_a_zip_the_tool_never_checked(self) -> None:
        """Coverage the tool was never asked about has no authority behind it."""
        validation = validate_sensitive_claims(
            "Yes, we serve ZIP code 98103.",
            evidence_texts=("Yes, we serve ZIP code 98103 throughout the year.",),
            confirmed_service_areas={},
        )
        assert validation.verdict is ClaimVerdict.UNSUPPORTED

    def test_an_affirmative_idiom_is_not_a_negative_verdict(self) -> None:
        """ "No problem" opens a yes; reading it as a refusal withheld a true answer."""
        validation = validate_sensitive_claims(
            "No problem, we serve ZIP 97205.",
            evidence_texts=(),
            confirmed_service_areas={"97205": True},
        )
        assert validation.verdict is ClaimVerdict.SUPPORTED

    def test_an_affirmative_idiom_does_not_mask_a_real_negation(self) -> None:
        """Stripping the idiom must leave the sentence's actual polarity intact."""
        validation = validate_sensitive_claims(
            "No problem, we do not serve ZIP 98999.",
            evidence_texts=(),
            confirmed_service_areas={"98999": False},
        )
        assert validation.verdict is ClaimVerdict.SUPPORTED

    def test_tool_verdicts_do_not_ground_other_claim_kinds(self) -> None:
        """A service-area verdict must not license a price or coverage claim."""
        validation = validate_sensitive_claims(
            "Repairs in 97205 are covered by your warranty and cost $250.",
            evidence_texts=(),
            confirmed_service_areas={"97205": True},
        )
        assert validation.verdict is ClaimVerdict.UNSUPPORTED
        assert {claim.kind for claim in validation.unsupported} == {
            ClaimKind.PRICE,
            ClaimKind.COVERAGE,
        }


class TestToolGroundedProvenance:
    """Which answers earned a document citation and which were decided by a tool."""

    def test_a_tool_answered_service_area_reply_rests_on_the_tool(self) -> None:
        assert answer_rests_only_on_tool_verdicts("Yes, we serve ZIP code 97205.", {"97205": True})

    def test_an_answer_making_another_claim_keeps_its_documents(self) -> None:
        """Over-dropping would strip provenance from the half a passage did support."""
        assert not answer_rests_only_on_tool_verdicts(
            "Yes, we serve ZIP code 97205. HVAC diagnostics cost $120.",
            {"97205": True},
        )

    def test_an_unconfirmed_service_area_claim_does_not_rest_on_the_tool(self) -> None:
        assert not answer_rests_only_on_tool_verdicts("Yes, we serve ZIP code 97205.", {})

    def test_an_answer_with_no_sensitive_claim_keeps_its_documents(self) -> None:
        """An ordinary grounded answer is untouched by this rule."""
        assert not answer_rests_only_on_tool_verdicts(
            "We are open daily from 7 AM to 7 PM.", {"97205": True}
        )
