"""The reviewed-exception registry: which regression was waived, by whom, for
which report.

A waiver is a human record, not a configuration knob: it names the case (or
aggregate metric) it waives, the exact baseline and candidate manifests the
waiver was reviewed against, the reviewer, and the reason. The gate applies a
waiver only when the manifest hashes match the run under review, so a new
regression from a changed candidate cannot inherit an old waiver — the
registry is the audit trail of "this regression was seen and accepted".

The shipped registry carries the two golden-v1 recall regressions of the
hybrid retriever (``apex-hvac-heating-repair``, ``clearview-hvac-current-pricing``):
they are the documented 1.0-to-0.95 aggregate recall delta of `RAG-009`'s
completion notes, reviewed and accepted at baseline, and bound to the exact
baseline/candidate manifests that reproduce them. Any candidate that changes
the manifest without fixing them is still blocked, because the hashes no
longer match.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from evals.versions import manifest_hash

_REGISTRY_PATH = Path(__file__).parent / "exceptions.json"


class ExceptionRegistryError(ValueError):
    """A waiver is malformed or references a manifest that is not pinned."""


@dataclass(frozen=True, slots=True)
class ReviewException:
    """One waived regression, bound to the report it was reviewed against."""

    case_id: str | None
    metric: str
    baseline_manifest_hash: str
    candidate_manifest_hash: str
    waived_by: str
    waived_at: str
    reason: str

    @classmethod
    def from_json(cls, raw: Mapping[str, object]) -> ReviewException:
        case_id = raw.get("case_id")
        for key in (
            "metric",
            "baseline_manifest_hash",
            "candidate_manifest_hash",
            "waived_by",
            "waived_at",
            "reason",
        ):
            if not raw.get(key):
                raise ExceptionRegistryError(f"waiver for {case_id!r} is missing {key!r}")
        return cls(
            case_id=None if case_id is None else str(case_id),
            metric=str(raw["metric"]),
            baseline_manifest_hash=str(raw["baseline_manifest_hash"]),
            candidate_manifest_hash=str(raw["candidate_manifest_hash"]),
            waived_by=str(raw["waived_by"]),
            waived_at=str(raw["waived_at"]),
            reason=str(raw["reason"]),
        )

    def to_json(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "metric": self.metric,
            "baseline_manifest_hash": self.baseline_manifest_hash,
            "candidate_manifest_hash": self.candidate_manifest_hash,
            "waived_by": self.waived_by,
            "waived_at": self.waived_at,
            "reason": self.reason,
        }

    def covers(self, *, case_id: str | None, metric: str, baseline: str, candidate: str) -> bool:
        """Whether this waiver applies to one regression of one report.

        The manifest hashes are the "which report" binding: a regression of
        the same case under a different candidate manifest is a different
        regression, and no waiver survives the change.
        """
        return (
            self.case_id == case_id
            and self.metric == metric
            and self.baseline_manifest_hash == baseline
            and self.candidate_manifest_hash == candidate
        )


class ExceptionRegistry:
    """The loaded waiver set plus the deterministic gate logic over it."""

    def __init__(self, waivers: tuple[ReviewException, ...]) -> None:
        self.waivers = waivers

    @classmethod
    def load(cls, path: Path | None = None) -> ExceptionRegistry:
        source = path or _REGISTRY_PATH
        raw = json.loads(source.read_text())
        waivers = raw.get("waivers") if isinstance(raw, Mapping) else None
        if not isinstance(waivers, list):
            raise ExceptionRegistryError(f"{source} carries no waivers list")
        return cls(tuple(ReviewException.from_json(dict(item)) for item in waivers))

    def applied_for(
        self, *, case_id: str | None, metric: str, baseline: str, candidate: str
    ) -> tuple[ReviewException, ...]:
        return tuple(
            waiver
            for waiver in self.waivers
            if waiver.covers(case_id=case_id, metric=metric, baseline=baseline, candidate=candidate)
        )

    def digest(self) -> str:
        """A content fingerprint of the loaded waiver set, stable across runs.

        The MLflow tracker records it as a run param (ML-01) so a tracked run
        states which waiver set it was gated against — a tracked pass under a
        registry that later gained a waiver is not evidence the regression was
        reviewed. Like the manifest hash it covers only the waivers' own
        fields, never case content.
        """
        canonical = json.dumps(
            [waiver.to_json() for waiver in self.waivers], sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mint_waiver(
    *,
    case_id: str | None,
    metric: str,
    baseline_components: Mapping[str, object],
    candidate_components: Mapping[str, object],
    waived_by: str,
    reason: str,
    waived_at: str | None = None,
) -> ReviewException:
    """A waiver pinned to the manifests it was reviewed against."""
    return ReviewException(
        case_id=case_id,
        metric=metric,
        baseline_manifest_hash=manifest_hash(baseline_components),
        candidate_manifest_hash=manifest_hash(candidate_components),
        waived_by=waived_by,
        waived_at=waived_at or datetime.now(UTC).date().isoformat(),
        reason=reason,
    )
