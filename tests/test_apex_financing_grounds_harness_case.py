"""The apex financing seed must ground the live harness financing answer.

``scripts/harness_live.py`` case-1 asks every tenant "What financing options are
available for a major HVAC replacement?" and requires an answered outcome with
at least one citation. Apex is ``PricingPolicy.NEVER``, so at the finalize node
its ``trusted_prices`` list is empty and every dollar amount the live model
writes must appear verbatim as a monetary token in an admitted passage
(``_price_grounded`` whole-token matching, R-11). The seeded financing document
once carried no dollar amounts at all, so any amount the model wrote failed
grounding and the case was refused with ``grounding_or_citation_error`` — live,
in front of the demo.

These tests hold the document's published terms against the real parse-and-chunk
seed seam, so an edit that renames, reformats, or drops an amount fails here in
CI rather than at the kiosk.
"""

from __future__ import annotations

from pathlib import Path

from scripts.harness_live import CASES
from scripts.seed_knowledge import _TENANTS
from tenantchat.api.parsing.adapters import MarkdownParser
from tenantchat.api.parsing.chunker import chunk_document
from tenantchat.api.registry import TenantRegistry
from tenantchat.core.claims import (
    ClaimKind,
    ClaimVerdict,
    sensitive_claims,
    validate_sensitive_claims,
)
from tenantchat.core.tenant import PricingPolicy

_ROOT = Path(__file__).resolve().parents[1]

# The figures the harness answer quotes. "$4,500" is comma-grouped on purpose:
# whole-token matching treats "$4,500" and "$4500" as different tokens, so the
# published formatting is part of the content contract.
PUBLISHED_TERMS = ("$0", "$89", "$4,500")

_HARNESS_CASE = next(case for case in CASES if case["id"] == "case-1-grounded")


def _apex_document_text() -> str:
    _, filepath, _title = next(entry for entry in _TENANTS if entry[0] == "apex")
    return (_ROOT / filepath).read_text(encoding="utf-8")


def _apex_passages() -> tuple[str, ...]:
    """The passages the governed pipeline chunks this seed into."""
    parsed = MarkdownParser().parse(
        _apex_document_text().encode("utf-8"),
        title="Apex financing options",
        media_type="text/markdown",
    )
    return tuple(chunk.text for chunk in chunk_document(parsed))


def _price_tokens(text: str) -> frozenset[str]:
    return frozenset(
        claim.value for claim in sensitive_claims(text) if claim.kind is ClaimKind.PRICE
    )


def test_apex_quotes_no_prices_off_policy_so_grounding_must_come_from_the_seed() -> None:
    """Empty ``trusted_prices`` at the finalize node is why the document alone
    can ground a price for this tenant; a policy flip silently changes what
    this file's other tests prove.
    """
    policy = TenantRegistry.seeded().get("apex").policy
    assert policy.pricing_policy is PricingPolicy.NEVER


def test_the_harness_case_still_requires_an_answered_grounded_reply() -> None:
    assert _HARNESS_CASE["outcomes"] == ("answered",)
    assert int(_HARNESS_CASE["min_citations"]) == 1


def test_each_published_financing_figure_is_a_whole_monetary_token_in_the_seed() -> None:
    passages = _apex_passages()
    for amount in PUBLISHED_TERMS:
        assert any(amount in _price_tokens(passage) for passage in passages), (
            f"{amount} is no longer published as a whole monetary token in the apex "
            "financing seed; a live harness answer quoting it cannot ground and the "
            "case is refused"
        )


def test_the_terms_are_published_in_one_passage_so_an_admitted_set_grounds_them() -> None:
    """Retrieval admits a subset of the seed's chunks and the model cites only
    what it was given. Figures scattered across chunks mean an admitted set can
    lack one of them and refuse an otherwise correct answer.
    """
    assert any(
        set(PUBLISHED_TERMS) <= _price_tokens(passage) for passage in _apex_passages()
    ), "no single passage carries all published figures; split the terms back together"


def test_the_harness_case_answer_passes_claim_validation_on_the_seeded_passages() -> None:
    answer = (
        "Apex publishes its promotional financing terms for a qualifying HVAC system "
        "replacement: $0 down and $89/month for 72 months, on approved credit, plus a "
        "$4,500 system credit for replacing an older system with a qualifying "
        "high-efficiency model. [evidence:financing-terms]"
    )
    validation = validate_sensitive_claims(
        answer,
        evidence_texts=_apex_passages(),
        trusted_prices=(),
    )
    assert validation.verdict is ClaimVerdict.SUPPORTED, (
        f"the seeded financing document no longer grounds the harness answer: "
        f"{validation.unsupported}"
    )


def test_an_amount_the_document_does_not_publish_fails_grounding() -> None:
    """The guard the content relies on stays closed: an amount the seed does not
    carry, and a reformatted amount ("$4,500.00" is a different token than
    "$4,500"), are both unsupported — which is why the document tells the
    assistant never to reformat a published figure.
    """
    answer = (
        "Apex financing runs $95/month for 72 months, and the system credit is "
        "$4,500.00. [evidence:financing-terms]"
    )
    validation = validate_sensitive_claims(
        answer,
        evidence_texts=_apex_passages(),
        trusted_prices=(),
    )
    failed = {claim.value for claim in validation.unsupported}
    assert validation.verdict is ClaimVerdict.UNSUPPORTED
    assert {"$95", "$4,500.00"} <= failed
