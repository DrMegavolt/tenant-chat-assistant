"""Baseline-versus-candidate comparison and the release gate (`RAG-008`).

Two runs of the same dataset over different component manifests (a retriever
config, a template, a model) produce two :class:`EvaluationReport` instances;
:func:`compare_reports` turns them into one comparison report a reviewer can
read without correlating anything by hand:

- the **manifest diff** — every component the two runs pinned, with which
  side changed and to what;
- the **aggregate deltas** — per-score movement;
- the **regressions and improvements** — per-case, per-metric, each
  regression carrying the case's provenance (an ``OBS-004`` turn record, a
  ``FEAT-008`` review, or nothing for hand-labelled fixtures);
- the **judge table** — every registered LLM-judge scorer with its measured
  agreement, and whether it may gate;
- the **gate decision** — thresholds minus the reviewed exceptions.

The per-case pass predicate mirrors :func:`case_passes` from the review
workflow (the predicate the ``FEAT-008`` closure uses), so the case the gate
blocks on and the case a passing run closes are the same judgement; a test
asserts the two predicates agree per case so CI and closure cannot diverge.

Determinism: every input is deterministic, so two runs over unchanged inputs
produce byte-identical comparison JSON. The documented tolerance is exact;
nothing in the harness samples or randomizes.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

from evals.dataset import DatasetSpec
from evals.exceptions import ExceptionRegistry, ReviewException
from evals.judges import judges
from evals.scorer import CaseResult, EvaluationReport
from evals.versions import manifest_hash

_SCHEMA_VERSION = 1

_SCORED_METRICS: tuple[str, ...] = (
    "recall_at_k",
    "citation_precision",
    "abstention_correctness",
    "grounding_correctness",
    "cross_tenant_leaks",
)

# Metrics whose readings are scores; the boolean metrics are reported as
# deltas only when their pass/fail state flips.
_NUMERIC_METRICS: frozenset[str] = frozenset({"recall_at_k", "citation_precision"})


@dataclass(frozen=True, slots=True)
class CaseDelta:
    """One case's movement on one metric, with its provenance for review."""

    case: str
    scenario: str | None
    metric: str
    baseline: float | None
    candidate: float | None
    regressed: bool
    improved: bool
    trace: dict[str, str | None]
    prior_turns: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "case": self.case,
            "scenario": self.scenario,
            "metric": self.metric,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "regressed": self.regressed,
            "improved": self.improved,
            "trace": dict(self.trace),
            "prior_turns": list(self.prior_turns),
        }


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason the gate refuses the candidate, or refuses it no longer.

    ``waived`` is set only by a reviewed exception whose manifest hashes match
    this exact comparison; a waived blocker is recorded, not silent.
    """

    kind: str
    metric: str | None
    case: str | None
    baseline: float | None
    candidate: float | None
    threshold: float | None
    waived: bool = False
    waived_by: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "metric": self.metric,
            "case": self.case,
            "baseline": self.baseline,
            "candidate": self.candidate,
            "threshold": self.threshold,
            "waived": self.waived,
            "waived_by": self.waived_by,
        }


@dataclass(frozen=True, slots=True)
class GateResult:
    """The gate decision: blockers minus waivers, and what rode along."""

    passed: bool
    blockers: tuple[Blocker, ...]
    exceptions_applied: tuple[dict[str, object], ...]
    informational: tuple[dict[str, object], ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "blockers": [blocker.to_dict() for blocker in self.blockers],
            "exceptions_applied": [dict(item) for item in self.exceptions_applied],
            "informational": [dict(item) for item in self.informational],
        }


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """One baseline-versus-candidate comparison, fully deterministic."""

    dataset: dict[str, object]
    baseline: dict[str, object]
    candidate: dict[str, object]
    manifest_diff: tuple[dict[str, object], ...]
    aggregate_deltas: dict[str, float]
    regressions: tuple[CaseDelta, ...]
    improvements: tuple[CaseDelta, ...]
    score_changes: tuple[CaseDelta, ...]
    judges: tuple[dict[str, object], ...]
    judge_regressions: tuple[dict[str, object], ...]
    significance: str
    gate: GateResult

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "significance": self.significance,
            "dataset": dict(self.dataset),
            "baseline": dict(self.baseline),
            "candidate": dict(self.candidate),
            "manifest_diff": [dict(item) for item in self.manifest_diff],
            "aggregate_deltas": dict(self.aggregate_deltas),
            "regressions": [item.to_dict() for item in self.regressions],
            "improvements": [item.to_dict() for item in self.improvements],
            "score_changes": [item.to_dict() for item in self.score_changes],
            "judges": [dict(item) for item in self.judges],
            "judge_regressions": [dict(item) for item in self.judge_regressions],
            "gate": self.gate.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def to_text(self) -> str:
        lines = [
            f"dataset: {self.dataset['name']} v{self.dataset['version']}",
            f"baseline: {self.baseline['run_id']}",
            f"candidate: {self.candidate['run_id']}",
        ]
        for component in self.manifest_diff:
            marker = "~" if component["changed"] else "="
            lines.append(f"{marker} {component['component']}")
        lines.append(f"gate: {'PASSED' if self.gate.passed else 'BLOCKED'}")
        for blocker in self.gate.blockers:
            note = " (waived)" if blocker.waived else ""
            lines.append(f"  blocker: {blocker.kind} {blocker.case or blocker.metric or ''}{note}")
        return "\n".join(lines)


def _run_id(components: Mapping[str, object]) -> str:
    return f"eval-{manifest_hash(components)[:16]}"


def _diff_components(
    baseline: Mapping[str, object], candidate: Mapping[str, object]
) -> tuple[dict[str, object], ...]:
    diff: list[dict[str, object]] = []
    for key in sorted(set(baseline) | set(candidate)):
        before = baseline.get(key)
        after = candidate.get(key)
        diff.append(
            {
                "component": key,
                "changed": _json(before) != _json(after),
                "baseline": before,
                "candidate": after,
            }
        )
    return tuple(diff)


def metric_passes(row: CaseResult, report: EvaluationReport, metric: str) -> bool:
    """Whether one metric reading of one row clears the run's thresholds.

    Unscored metrics (no gold, no citation, no grounded answer) pass: a case
    cannot fail what it does not measure. The whole-row predicate mirrors
    :func:`case_passes`; the equivalence is asserted in the test suite.
    """
    components = _mapping(report.to_dict().get("components"))
    if metric == "recall_at_k":
        min_recall = _component_float(components, "min_recall", 0.0)
        return row.recall is None or row.recall >= min_recall
    if metric == "citation_precision":
        min_citation = _component_float(components, "min_citation_precision", 0.0)
        return row.citation_precision is None or row.citation_precision >= min_citation
    if metric == "abstention_correctness":
        return row.abstain_correct
    if metric == "grounding_correctness":
        return row.grounding_correct is not False
    if metric == "cross_tenant_leaks":
        return not row.cross_tenant_leaks
    raise ValueError(f"unknown scored metric {metric!r}")


def row_passes(row: CaseResult, report: EvaluationReport) -> bool:
    """The whole-row reading of the case's per-case thresholds."""
    return all(metric_passes(row, report, metric) for metric in _SCORED_METRICS)


def _metric_values(row: CaseResult) -> dict[str, float | None]:
    """The metric readings of one row, booleans as 1.0/0.0, unscored as None."""
    return {
        "recall_at_k": row.recall,
        "citation_precision": row.citation_precision,
        "abstention_correctness": 1.0 if row.abstain_correct else 0.0,
        "grounding_correctness": (
            None if row.grounding_correct is None else (1.0 if row.grounding_correct else 0.0)
        ),
        "cross_tenant_leaks": 1.0 if row.cross_tenant_leaks else 0.0,
    }


def _case_deltas(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
) -> tuple[tuple[CaseDelta, ...], tuple[CaseDelta, ...], tuple[CaseDelta, ...]]:
    baseline_by_case = {row.case.id: row for row in baseline.cases}
    regressions: list[CaseDelta] = []
    improvements: list[CaseDelta] = []
    score_changes: list[CaseDelta] = []
    for candidate_row in candidate.cases:
        baseline_row = baseline_by_case.get(candidate_row.case.id)
        if baseline_row is None:
            continue
        before_values = _metric_values(baseline_row)
        after_values = _metric_values(candidate_row)
        for metric in _SCORED_METRICS:
            before = before_values[metric]
            after = after_values[metric]
            if before is None and after is None:
                continue
            flipped_before = metric_passes(baseline_row, baseline, metric)
            flipped_after = metric_passes(candidate_row, candidate, metric)
            delta = CaseDelta(
                case=candidate_row.case.id,
                scenario=candidate_row.case.scenario,
                metric=metric,
                baseline=before,
                candidate=after,
                regressed=flipped_before and not flipped_after,
                improved=not flipped_before and flipped_after,
                trace=_provenance(candidate_row),
                prior_turns=candidate_row.case.prior_turns,
            )
            if delta.regressed:
                regressions.append(delta)
            elif delta.improved:
                improvements.append(delta)
            elif (
                metric in _NUMERIC_METRICS
                and before is not None
                and after is not None
                and after != before
            ):
                score_changes.append(delta)
    return tuple(regressions), tuple(improvements), tuple(score_changes)


def _provenance(row: CaseResult) -> dict[str, str | None]:
    """The regression's supporting record: turn, trace, or review.

    Promoted cases carry their ``review-<id>`` (and the source turn's trace
    when the promotion ingestion recorded one); hand-labelled cases carry
    nothing, and the report says so rather than inventing a link.
    """
    return {
        "review_id": row.case.review_id,
        "trace_id": row.case.trace_id,
        "turn_id": row.case.turn_id,
    }


def compare_reports(
    baseline: EvaluationReport,
    candidate: EvaluationReport,
    dataset: DatasetSpec,
    *,
    exceptions: ExceptionRegistry,
    thresholds: Mapping[str, float] | None = None,
    judge_regressions: Sequence[tuple[str, str]] = (),
) -> ComparisonReport:
    """One comparison plus its gate decision.

    ``thresholds`` defaults to the dataset manifest's; every candidate
    aggregate below its threshold blocks unless a matching reviewed exception
    waives it, and every case metric that flips from pass to fail blocks the
    same way. Cross-tenant leaks are never waivable.
    """
    baseline_components = _mapping(baseline.to_dict().get("components"))
    candidate_components = _mapping(candidate.to_dict().get("components"))
    baseline_hash = manifest_hash(baseline_components)
    candidate_hash = manifest_hash(candidate_components)
    threshold_map = dict(thresholds or dataset.thresholds)
    aggregate_deltas = {
        metric: round(
            float(candidate.aggregate.get(metric, 0.0))
            - float(baseline.aggregate.get(metric, 0.0)),
            4,
        )
        for metric in _SCORED_METRICS
    }
    regressions, improvements, score_changes = _case_deltas(baseline, candidate)

    blockers: list[Blocker] = []
    applied: list[ReviewException] = []
    for metric, threshold in sorted(threshold_map.items()):
        # A dimension the dataset never exercises has an empty aggregate and
        # must not read as a zero below threshold: the scorer's own pass rule
        # treats unscored dimensions the same way.
        if not candidate.scored.get(metric, True):
            continue
        score = float(candidate.aggregate.get(metric, 0.0))
        if score >= threshold:
            continue
        blockers.append(
            _waived_blocker(
                Blocker(
                    kind="aggregate_below_threshold",
                    metric=metric,
                    case=None,
                    baseline=float(baseline.aggregate.get(metric, 0.0)),
                    candidate=score,
                    threshold=threshold,
                ),
                metric=metric,
                case_id=None,
                baseline_hash=baseline_hash,
                candidate_hash=candidate_hash,
                exceptions=exceptions,
                applied=applied,
            )
        )
    if float(candidate.aggregate.get("cross_tenant_leaks", 0.0)) > 0.0:
        blockers.append(
            Blocker(
                kind="cross_tenant_leak",
                metric="cross_tenant_leaks",
                case=None,
                baseline=float(baseline.aggregate.get("cross_tenant_leaks", 0.0)),
                candidate=float(candidate.aggregate.get("cross_tenant_leaks", 0.0)),
                threshold=0.0,
            )
        )
    for delta in regressions:
        blockers.append(
            _waived_blocker(
                Blocker(
                    kind="case_regression",
                    metric=delta.metric,
                    case=delta.case,
                    baseline=delta.baseline,
                    candidate=delta.candidate,
                    threshold=None,
                ),
                metric=delta.metric,
                case_id=delta.case,
                baseline_hash=baseline_hash,
                candidate_hash=candidate_hash,
                exceptions=exceptions,
                applied=applied,
            )
        )
    informational: list[dict[str, object]] = []
    for judge_name, case_id in judge_regressions:
        profile = next((candidate for candidate in judges() if candidate.name == judge_name), None)
        if profile is None:
            informational.append({"judge": judge_name, "case": case_id, "state": "unregistered"})
            continue
        if profile.can_gate():
            blockers.append(
                Blocker(
                    kind="judge_regression",
                    metric=None,
                    case=case_id,
                    baseline=None,
                    candidate=None,
                    threshold=None,
                )
            )
        else:
            informational.append(
                {
                    "judge": judge_name,
                    "case": case_id,
                    "state": "unvalidated",
                    "agreement": profile.agreement,
                    "held_out_size": profile.held_out_size,
                }
            )
    gate = GateResult(
        passed=all(blocker.waived for blocker in blockers),
        blockers=tuple(blockers),
        exceptions_applied=tuple(waiver.to_json() for waiver in applied),
        informational=tuple(informational),
    )
    return ComparisonReport(
        dataset=dataset.as_dict(),
        baseline={
            "run_id": _run_id(baseline_components),
            "components": baseline_components,
            "scores": dict(baseline.aggregate),
            "scored": dict(baseline.scored),
            "passed": baseline.passed,
        },
        candidate={
            "run_id": _run_id(candidate_components),
            "components": candidate_components,
            "scores": dict(candidate.aggregate),
            "scored": dict(candidate.scored),
            "passed": candidate.passed,
        },
        manifest_diff=_diff_components(baseline_components, candidate_components),
        aggregate_deltas=aggregate_deltas,
        regressions=regressions,
        improvements=improvements,
        score_changes=score_changes,
        judges=tuple(profile.as_dict() for profile in judges()),
        judge_regressions=tuple(
            {"judge": judge, "case": case} for judge, case in judge_regressions
        ),
        significance=(
            "deterministic-exact: fixture scoring samples nothing, so material "
            "regressions are exact (pass-to-fail or below threshold), not statistical"
        ),
        gate=gate,
    )


def _waived_blocker(
    blocker: Blocker,
    *,
    metric: str,
    case_id: str | None,
    baseline_hash: str,
    candidate_hash: str,
    exceptions: ExceptionRegistry,
    applied: list[ReviewException],
) -> Blocker:
    """Return the blocker with its reviewed exception, when one covers it."""
    waivers = exceptions.applied_for(
        case_id=case_id, metric=metric, baseline=baseline_hash, candidate=candidate_hash
    )
    if not waivers:
        return blocker
    waiver = waivers[0]
    applied.append(waiver)
    return replace(blocker, waived=True, waived_by=waiver.waived_by)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, default=str)


def _mapping(value: object) -> Mapping[str, object]:
    return dict(value) if isinstance(value, Mapping) else {}


def _component_float(components: Mapping[str, object], name: str, default: float) -> float:
    value = components.get(name)
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return default
