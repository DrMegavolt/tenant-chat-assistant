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
