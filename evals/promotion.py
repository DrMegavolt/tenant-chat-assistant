"""Materializing `FEAT-008` promoted projections into versioned dataset cases.

The production flywheel produces a reviewed case as a ``turn_record_projections``
row of kind ``eval_dataset``: the payload is the anonymized case shape
(``id = review-<review_id>``, ``query``, ``gold_chunk_ids``, ``citations``,
``scenario``, ``expect_abstain``). This module is the documented path from
that projection to a scoreable dataset case (`RAG-008`):

1. resolve the gold chunk texts from the knowledge base — the projection
   stores chunk ids, so the harness needs the tenant's chunk texts to score
   retrieval evidence against the corpus (`FEAT-008`'s follow-up note);
2. re-run the PRIV-002 check on the free-text fields (defense in depth: the
   promotion path checked once, the dataset ingestion checks again, and the
   dataset loader checks every load);
3. attach the provenance the comparison report links regressions to: the
   review id and, when the source turn record is available, its trace id and
   turn id.

The knowledge-base lookup is injected as a mapping so the hermetic tests can
feed the fixture corpus while the release tooling feeds the tenant's indexed
chunks. ``RAG-006`` is not built, so a promoted case is materialized as the
single resolved query the current runner scores; if a future payload carries
a corrected answer (``answer``/``expect_grounded``), it is passed through to
the grounding scorer unchanged.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from evals.dataset import DatasetError
from tenantchat.core.reviews import payload_contains_pii

_REVIEW_ID_PREFIX = "review-"


class PromotedCaseError(DatasetError):
    """A promoted projection cannot become a dataset case as it stands."""


def materialize_promoted_case(
    payload: Mapping[str, object],
    *,
    chunk_texts: Mapping[str, str],
    pii_check: Callable[[Mapping[str, object]], bool] = payload_contains_pii,
    turn_record: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """One projection payload into a scoreable, re-checked dataset case.

    Raises:
        PromotedCaseError: the payload is not the promoted case shape, a gold
            chunk has no text in the knowledge base, or the PRIV-002 re-check
            finds contact data in a free-text field.
    """
    case_id = str(payload.get("id", ""))
    if not case_id.startswith(_REVIEW_ID_PREFIX):
        raise PromotedCaseError(
            f"promoted case {case_id!r} must be a review-derived id ({_REVIEW_ID_PREFIX}<id>)"
        )
    try:
        tenant_id = str(payload["tenant_id"])
        query = str(payload["query"])
        gold = tuple(str(item) for item in _list(payload.get("gold_chunk_ids")))
        citations = tuple(str(item) for item in _list(payload.get("citations")))
        scenario = str(payload.get("scenario", "reviewed-turn"))
        expect_abstain = bool(payload.get("expect_abstain", False))
    except (KeyError, TypeError) as error:
        raise PromotedCaseError(
            f"promoted case {case_id!r} is not the eval payload shape"
        ) from error
    missing = tuple(chunk for chunk in gold if chunk not in chunk_texts)
    if missing:
        raise PromotedCaseError(
            f"promoted case {case_id!r} gold chunks have no knowledge-base text: {missing}"
        )
    case_payload = eval_case_payload(
        case_id=case_id,
        tenant_id=tenant_id,
        query=query,
        gold_chunk_ids=gold,
        citations=citations,
        scenario=scenario,
        expect_abstain=expect_abstain,
        answer=None if payload.get("answer") is None else str(payload["answer"]),
        expect_grounded=(
            None if payload.get("expect_grounded") is None else bool(payload["expect_grounded"])
        ),
    )
    if pii_check(case_payload):
        raise PromotedCaseError(
            f"promoted case {case_id!r} fails the PRIV-002 re-check on ingestion"
        )
    case_payload["source"] = "promoted"
    case_payload["review_id"] = case_id
    if turn_record is not None:
        for field in ("trace_id", "turn_id"):
            value = turn_record.get(field)
            if value is not None:
                case_payload[field] = str(value)
    return case_payload


def projection_dataset(
    projections: Sequence[Mapping[str, object]],
    *,
    tenant_id: str,
    chunk_texts: Mapping[str, str],
    pii_check: Callable[[Mapping[str, object]], bool] = payload_contains_pii,
    turn_records: Mapping[str, Mapping[str, object]] | None = None,
    version: int = 1,
) -> dict[str, object]:
    """A versioned dataset manifest from the tenant's promoted projections.

    Each projection is materialized with its provenance (turn records keyed
    by projection id carry the source trace), and the manifest attests the
    second PRIV-002 pass. The release tooling writes this as
    ``evals/datasets/promoted-<tenant>-v<version>.json``; the loader's own
    check then runs a third time at every load.
    """
    cases: list[dict[str, Any]] = []
    for projection in projections:
        turn_record = turn_records.get(str(projection.get("id", ""))) if turn_records else None
        cases.append(
            materialize_promoted_case(
                projection,
                chunk_texts=chunk_texts,
                pii_check=pii_check,
                turn_record=turn_record,
            )
        )
    return {
        "name": f"promoted-{tenant_id}-v{version}",
        "version": version,
        "source": "promoted",
        "abstain_threshold": 0.5,
        "thresholds": {
            "recall_at_k": 0.6,
            "citation_precision": 0.8,
            "abstention_correctness": 0.9,
            "grounding_correctness": 0.9,
        },
        "pii_check": {
            "policy": "PRIV-002",
            "method": "core.reviews.payload_contains_pii at promotion (FEAT-008), at "
            "dataset ingestion (evals.promotion), and at every load (evals.dataset)",
            "enforced_at": "promotion-ingest-and-load",
        },
        "cases": cases,
    }


def _list(value: object) -> list[object]:
    return list(value) if isinstance(value, Sequence) else []


def eval_case_payload(
    *,
    case_id: str,
    tenant_id: str,
    query: str,
    gold_chunk_ids: Sequence[str],
    citations: Sequence[str],
    scenario: str,
    expect_abstain: bool,
    answer: str | None = None,
    expect_grounded: bool | None = None,
) -> dict[str, object]:
    """The dataset-case shape the PRIV-002 re-check and the loader both parse."""
    payload: dict[str, object] = {
        "id": case_id,
        "tenant_id": tenant_id,
        "query": query,
        "gold_chunk_ids": list(gold_chunk_ids),
        "citations": list(citations),
        "scenario": scenario,
        "expect_abstain": expect_abstain,
    }
    if answer is not None:
        payload["answer"] = answer
        payload["expect_grounded"] = expect_grounded
    return payload
