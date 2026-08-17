"""Deterministic validation of sensitive financial and business claims (`RAG-007`).

Retrieval grounds the model's answer in evidence, but grounding is only a
prompt instruction; a fabricated price or a coverage statement invented by the
model must not reach a customer. This module turns that rule into a pure,
deterministic check: find every sensitive claim in an answer and require each
one to be verifiable against the exact evidence the answer's model call was
given.

Two families are recognized:

- **Prices** — dollar amounts. Every amount in the answer must appear verbatim
  in an admitted evidence passage or in the tenant's approved price list, which
  is server-owned and quoted to the model from the same prompt.
- **Coverage, permit, and insurance claims** — sentences that assert a
  business-sensitive fact. A claim sentence must be substantially supported by
  one evidence passage (most of its content words present in one passage);
  unsupported sentences fail the answer.

Service-area claims are the one family retrieval cannot ground: whether a ZIP
is served is decided by the tenant's own policy through a deterministic tool,
not written down in an approved document. They are checked against that tool's
verdict for the exact ZIP the sentence names, which is stronger evidence than
any passage — a claim about a ZIP the tool was not asked about, or one whose
polarity disagrees with the tool, still fails.

The verdict is applied to the whole answer: an unsupported price means the
published answer must not carry it, and the graph refuses the answer rather
than trying to surgically remove one sentence from a model's prose.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from tenantchat.core.text import query_words, tokenize

_PRICE_RE = re.compile(r"\$[0-9]+(?:\.[0-9]{2})?")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
_ZIP_RE = re.compile(r"\b\d{5}\b")

# Negation anywhere in the sentence flips the polarity the tool must agree
# with. An unrecognized negation makes the claim disagree with a `served: true`
# verdict and fail, which is the safe direction: the answer is withheld rather
# than published unverified. Model output uses both apostrophes.
_APOSTROPHE = "['\u2019]?"
_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|cannot|outside|beyond|unfortunately|unable"
    rf"|do(?:es)?n{_APOSTROPHE}t|can{_APOSTROPHE}t|is\s?n{_APOSTROPHE}t"
    rf"|are\s?n{_APOSTROPHE}t|wo\s?n{_APOSTROPHE}t)\b",
    re.IGNORECASE,
)

_EMPTY_SERVICE_AREAS: Mapping[str, bool] = MappingProxyType({})

# A sentence asserting one of these facts is business-sensitive: getting it
# wrong costs the customer money or an expectation the company must honor.
_SENSITIVE_KEYWORDS = frozenset(
    {
        "covered",
        "coverage",
        "insured",
        "insurance",
        "permit",
        "permits",
        "permitted",
        "licensed",
        "license",
        "certified",
        "warranty",
        "warranted",
        "guarantee",
        "guaranteed",
        "liability",
        "bonded",
        "serve",
        "serves",
        "served",
        "serving",
    }
)

# Multi-word phrases that trigger a service-area claim independent of
# single-word token matching. Checked against the raw sentence text.
_SERVICE_AREA_PHRASE_RE = re.compile(r"\bservice area\b", re.IGNORECASE)

# A keyword sentence counts as supported when at least this fraction of its
# content words appear in one passage. The threshold is deliberately high: a
# claim built on an invented detail fails even when it shares a topic.
_MIN_TOKEN_SUPPORT = 0.7


class ClaimKind(StrEnum):
    """The recognized family of one sensitive claim."""

    PRICE = "price"
    COVERAGE = "coverage"
    PERMIT = "permit"
    INSURANCE = "insurance"
    SERVICE_AREA = "service_area"

    @classmethod
    def of_keyword(cls, keyword: str) -> ClaimKind:
        """Map a matched keyword onto its family."""
        if keyword in {"permit", "permits", "permitted", "licensed", "license", "certified"}:
            return cls.PERMIT
        if keyword in {"insured", "insurance", "liability", "bonded"}:
            return cls.INSURANCE
        if keyword in {"serve", "serves", "served", "serving"}:
            return cls.SERVICE_AREA
        return cls.COVERAGE


@dataclass(frozen=True, slots=True)
class Claim:
    """One sensitive claim found in an answer.

    ``value`` is the exact text the validator must verify: the amount for a
    price, the sentence for a keyword claim.
    """

    kind: ClaimKind
    value: str


class ClaimVerdict(StrEnum):
    """The answer-level outcome of claim validation."""

    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True, slots=True)
class ClaimValidation:
    """The verdict plus the claims that failed it.

    ``unsupported`` names only the claim family and value — never the evidence
    or the answer — so a caller can log and audit the failure without carrying
    content into the operational plane.
    """

    verdict: ClaimVerdict
    unsupported: tuple[Claim, ...] = ()


def sensitive_claims(text: str) -> tuple[Claim, ...]:
    """Every sensitive claim an answer makes, prices then keyword sentences.

    Ordering contract: prices in order of appearance, then one claim per
    sentence in sentence order, kinded by the keyword that appears earliest in
    that sentence. Order is decided by text position alone, never by set
    iteration, so the same answer stores the same ``claims_invalid`` bytes in
    every interpreter regardless of ``PYTHONHASHSEED``.
    """
    prices = tuple(Claim(ClaimKind.PRICE, amount) for amount in _PRICE_RE.findall(text))
    keyword_claims: list[Claim] = []
    for sentence in _SENTENCE_RE.split(text):
        if _SERVICE_AREA_PHRASE_RE.search(sentence):
            keyword_claims.append(Claim(ClaimKind.SERVICE_AREA, sentence.strip()))
            continue
        kind = _earliest_keyword_kind(sentence)
        if kind is not None:
            keyword_claims.append(Claim(kind, sentence.strip()))
    return prices + tuple(keyword_claims)


def validate_sensitive_claims(
    answer: str,
    *,
    evidence_texts: Sequence[str],
    trusted_prices: Sequence[str] = (),
    confirmed_service_areas: Mapping[str, bool] = _EMPTY_SERVICE_AREAS,
) -> ClaimValidation:
    """Verify every sensitive claim in ``answer`` against the admitted evidence.

    Args:
        answer: The model's answer before citation markers are stripped.
        evidence_texts: The full content of the passages that were admitted to
            the answer's prompt context.
        trusted_prices: Server-owned approved price lines (from the tenant
            policy), which may ground a price claim without retrieval.
        confirmed_service_areas: ZIP to served verdict, as the service-area
            tool answered it during this turn. Only ZIPs the tool was actually
            asked about appear; a claim naming any other ZIP falls back to
            evidence and normally fails.

    Returns:
        :attr:`ClaimVerdict.UNSUPPORTED` when any claim fails, with the
        failing claims attached.
    """
    claims = sensitive_claims(answer)
    unsupported: list[Claim] = []
    for claim in claims:
        if claim.kind is ClaimKind.PRICE:
            grounded = any(claim.value in passage for passage in evidence_texts) or any(
                claim.value in line for line in trusted_prices
            )
        elif claim.kind is ClaimKind.SERVICE_AREA:
            grounded = _service_area_confirmed(
                claim.value, confirmed_service_areas
            ) or _sentence_supported(claim.value, evidence_texts)
        else:
            grounded = _sentence_supported(claim.value, evidence_texts)
        if not grounded:
            unsupported.append(claim)
    if unsupported:
        return ClaimValidation(ClaimVerdict.UNSUPPORTED, tuple(unsupported))
    return ClaimValidation(ClaimVerdict.SUPPORTED)


def _service_area_confirmed(sentence: str, confirmed: Mapping[str, bool]) -> bool:
    """Whether the service-area tool already decided exactly what this sentence says.

    Requires a ZIP: "we serve your area" names nothing checkable and is left to
    evidence. Every ZIP the sentence names must have been checked this turn,
    and the sentence's polarity must match every verdict — so a "yes" about an
    unserved ZIP, or a claim mixing a served and an unserved ZIP, still fails.
    """
    zips = _ZIP_RE.findall(sentence)
    if not zips:
        return False
    expected = not _NEGATION_RE.search(sentence)
    return all(zip_code in confirmed and confirmed[zip_code] is expected for zip_code in zips)


def _keyword_tokens(sentence: str) -> frozenset[str]:
    return frozenset(tokenize(sentence))


def _earliest_keyword_kind(sentence: str) -> ClaimKind | None:
    """The family of the sensitive keyword that appears earliest in ``sentence``.

    One claim per sentence needs one representative keyword; the earliest is
    the most salient because a sentence usually states its subject first.
    """
    earliest: tuple[int, str] | None = None
    for position, token in enumerate(tokenize(sentence)):
        if token in _SENSITIVE_KEYWORDS:
            candidate = (position, token)
            if earliest is None or candidate < earliest:
                earliest = candidate
    if earliest is None:
        return None
    return ClaimKind.of_keyword(earliest[1])


def _sentence_supported(sentence: str, evidence_texts: Sequence[str]) -> bool:
    """Whether most of a claim sentence's content words appear in one passage."""
    claim_tokens = _keyword_tokens(sentence)
    if not claim_tokens:
        return True
    for passage in evidence_texts:
        passage_tokens = query_words(passage)
        supported = sum(1 for token in claim_tokens if token in passage_tokens)
        if supported / len(claim_tokens) >= _MIN_TOKEN_SUPPORT:
            return True
    return False
