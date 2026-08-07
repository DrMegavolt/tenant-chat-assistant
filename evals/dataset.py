"""Versioned evaluation datasets: manifest, cases, and the PII gate.

A dataset is one JSON manifest under ``evals/datasets/`` naming ``name``,
``version``, ``source`` (``hand-labelled`` or ``promoted``), the PRIV-002
check attestation, the abstention threshold, and the release thresholds.
Cases come inline or from a ``cases_file`` (the golden v1 dataset *is* the
``RAG-009`` fixture file, referenced rather than copied so the two cannot
diverge); the corpus comes from an optional ``corpus_file`` or the shared
fixture corpus.

Multi-turn behavior (`RAG-006`): a case carries ``prior_turns`` and a ``query``
that is the *raw* follow-up. The runner resolves the pair into a standalone
retrieval query with ``tenantchat.core.planning.plan_query`` before scoring,
using the dataset's per-tenant ``vocabulary`` — the server-approved known terms
the planner may carry out of history. A dataset with no vocabulary is scored
single-turn, as before.

The PRIV-002 gate is enforced here, at load: every free-text case field
(``query``, ``scenario``, ``answer``, ``prior_turns``) is scanned with the
same ``core.reviews.payload_contains_pii`` patterns the promotion path uses,
so a dataset that would carry a phone or email address cannot load. Promoted
datasets are checked twice by design — once at promotion (FEAT-008) and once
here (defense in depth).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from pathlib import Path

from evals.scorer import EvalCase
from tenantchat.core.reviews import payload_contains_pii

_DATASETS_DIR = Path(__file__).parent / "datasets"

_SOURCES: frozenset[str] = frozenset({"hand-labelled", "promoted"})

# Account/card numbers and the seed tenants' documented ZIP ranges, the same
# patterns the RAG-009 fixture test asserts; kept here so any dataset, not
# only the fixtures, is held to them.
_ACCOUNT_RE = re.compile(r"\b\d{9,}\b")
_ZIP_RE = re.compile(r"\b\d{5}\b")
_KNOWN_ZIPS = re.compile(r"9810[1-5]|97035|9720[1-5]")

_FREE_TEXT_FIELDS: tuple[str, ...] = ("query", "scenario", "answer")


class DatasetError(ValueError):
    """A dataset manifest is malformed or carries content it must not."""


@dataclass(frozen=True, slots=True)
class DatasetSpec:
    """One loaded dataset: the manifest plus its parsed, PII-checked cases."""

    name: str
    version: int
    source: str
    pii_check: dict[str, object]
    abstain_threshold: float
    thresholds: dict[str, float]
    documentation: str
    cases: tuple[EvalCase, ...]
    corpus_file: str | None
    parser_chunker: str | None
    tenant_policy: str | None
    vocabulary: dict[str, tuple[str, ...]] = dataclass_field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "source": self.source,
            "abstain_threshold": self.abstain_threshold,
            "thresholds": dict(self.thresholds),
            "pii_check": dict(self.pii_check),
        }


def datasets_dir() -> Path:
    """Where dataset manifests live, for tests and the release tooling."""
    return _DATASETS_DIR


def known_datasets() -> tuple[str, ...]:
    """Every dataset manifest, sorted for a stable listing.

    Only files that declare themselves datasets count: the directory also
    holds corpus files (e.g. ``adversarial-corpus.json``) that must never be
    loaded as datasets.
    """
    names: list[str] = []
    for path in _DATASETS_DIR.glob("*.json"):
        raw = json.loads(path.read_text())
        if isinstance(raw, Mapping) and "source" in raw:
            names.append(path.stem)
    return tuple(sorted(names))


def load_dataset(name: str) -> DatasetSpec:
    """Load one dataset by manifest name, refusing any case that carries PII.

    Raises:
        DatasetError: the manifest is unknown, malformed, or fails the
            PRIV-002 gate on any free-text case field.
    """
    path = _DATASETS_DIR / f"{name}.json"
    if not path.is_file():
        raise DatasetError(f"no dataset named {name!r}; known: {', '.join(known_datasets())}")
    raw = json.loads(path.read_text())
    if not isinstance(raw, Mapping):
        raise DatasetError(f"dataset {name!r} is not a JSON object")
    manifest = dict(raw)
    source = str(manifest.get("source", ""))
    if source not in _SOURCES:
        raise DatasetError(f"dataset {name!r} source {source!r} must be one of {sorted(_SOURCES)}")
    pii_check = manifest.get("pii_check")
    if not isinstance(pii_check, Mapping) or not pii_check.get("policy"):
        raise DatasetError(f"dataset {name!r} must attest its PRIV-002 check")
    version = manifest.get("version")
    if not isinstance(version, int):
        raise DatasetError(f"dataset {name!r} version must be an integer")
    abstain = float(manifest["abstain_threshold"])
    if not 0.0 < abstain <= 1.0:
        raise DatasetError(f"dataset {name!r} abstain_threshold must be in (0, 1]")
    thresholds = _thresholds(manifest.get("thresholds"))
    cases = _load_cases(path, manifest)
    _assert_no_pii(name, cases)
    return DatasetSpec(
        name=name,
        version=version,
        source=source,
        pii_check=dict(pii_check),
        abstain_threshold=abstain,
        thresholds=thresholds,
        documentation=str(manifest.get("documentation", "")),
        cases=cases,
        corpus_file=None if manifest.get("corpus_file") is None else str(manifest["corpus_file"]),
        parser_chunker=None
        if manifest.get("parser_chunker") is None
        else str(manifest["parser_chunker"]),
        tenant_policy=None
        if manifest.get("tenant_policy") is None
        else str(manifest["tenant_policy"]),
        vocabulary=_vocabulary(manifest.get("vocabulary")),
    )


def _vocabulary(raw: object) -> dict[str, tuple[str, ...]]:
    """The per-tenant known terms a multi-turn dataset may carry over.

    A vocabulary is optional and must be a mapping of tenant id to a list of
    terms; anything else fails the load, because a malformed vocabulary would
    silently change which history the planner is allowed to use.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise DatasetError("vocabulary must be a per-tenant mapping")
    parsed: dict[str, tuple[str, ...]] = {}
    for tenant, terms in dict(raw).items():
        if not isinstance(terms, list | tuple) or not all(isinstance(term, str) for term in terms):
            raise DatasetError(f"vocabulary for tenant {tenant!r} must be a list of terms")
        parsed[str(tenant)] = tuple(str(term) for term in terms)
    return parsed


def validate_against_corpus(dataset: DatasetSpec, known_chunks: Sequence[str]) -> tuple[str, ...]:
    """The dataset's references resolve against the corpus it will run on.

    Returns the unresolvable references (empty when healthy): gold chunks and
    every citation except a deliberately fabricated one must name a chunk the
    corpus indexes.
    """
    known = frozenset(known_chunks)
    missing: list[str] = []
    for case in dataset.cases:
        missing.extend(chunk for chunk in case.gold_chunk_ids if chunk not in known)
        if case.scenario != "fabricated_citation":
            missing.extend(chunk for chunk in case.citations if chunk not in known)
    return tuple(sorted(set(missing)))


def _load_cases(path: Path, manifest: Mapping[str, object]) -> tuple[EvalCase, ...]:
    raw_cases: object = manifest.get("cases")
    cases_file = manifest.get("cases_file")
    if raw_cases is not None and cases_file is not None:
        raise DatasetError(f"dataset {path.stem!r} must not mix inline cases and cases_file")
    if raw_cases is not None:
        if not isinstance(raw_cases, list):
            raise DatasetError(f"dataset {path.stem!r} cases must be a list")
        return tuple(EvalCase.from_json(dict(item)) for item in raw_cases)
    if cases_file is None:
        raise DatasetError(f"dataset {path.stem!r} carries no cases")
    fixture = json.loads((path.parent / str(cases_file)).read_text())
    if not isinstance(fixture, Mapping) or not isinstance(fixture.get("cases"), list):
        raise DatasetError(f"dataset {path.stem!r} cases_file is not the fixture shape")
    return tuple(EvalCase.from_json(dict(item)) for item in fixture["cases"])


def _thresholds(raw: object) -> dict[str, float]:
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise DatasetError("thresholds must be an object")
    return {str(key): float(value) for key, value in dict(raw).items()}


def _assert_no_pii(name: str, cases: Sequence[EvalCase]) -> None:
    for case in cases:
        for field in _FREE_TEXT_FIELDS:
            text = getattr(case, field)
            if text is None:
                continue
            if payload_contains_pii({"query": text}):
                raise DatasetError(f"dataset {name!r} case {case.id!r} carries PII in {field}")
        for turn in case.prior_turns:
            if payload_contains_pii({"query": turn}):
                raise DatasetError(f"dataset {name!r} case {case.id!r} carries PII in prior_turns")
        combined = "\n".join(filter(None, (case.query, case.scenario or "", case.answer or "")))
        if _ACCOUNT_RE.search(combined):
            raise DatasetError(f"dataset {name!r} case {case.id!r} carries an account number")
        if _ZIP_RE.search(_KNOWN_ZIPS.sub("", combined)):
            raise DatasetError(f"dataset {name!r} case {case.id!r} carries an unexpected ZIP")
