"""The release gate: baseline-versus-candidate comparison on versioned
datasets (`RAG-008`).

Runs the same dataset through the baseline retriever and the candidate
retriever, emits the comparison report (manifest diff, aggregate deltas,
regressions with their trace linkage, improvements, judge table), applies
the reviewed-exception registry, and exits non-zero when the candidate is
below thresholds or regressed a case without a waiver.

Hermetic: no network, database, LLM, or embedding service — it belongs to
``make check`` exactly like the RAG-009 runner. ``--verify-determinism`` runs
the pair in two fresh interpreters under pinned, different ``PYTHONHASHSEED``
values (1 and 42) and requires byte-identical reports, which is the documented
stability check of the acceptance criteria: two runs inside one process always
agree because set iteration order is fixed at interpreter startup, so the
check proves nothing unless fresh interpreters exist.

The FEAT-008 closure rides the same candidate report: the store the release
pipeline calls through is injected by the composition root, and
:func:`close_passing_reviews` hands the report to ``apply_eval_report`` with
a server-minted run id so every promoted case the run passes closes its
``awaiting_fix`` review exactly once.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from evals.compare import ComparisonReport, compare_reports
from evals.dataset import DatasetSpec
from evals.exceptions import ExceptionRegistry
from evals.runner import build_retriever_entry, dataset_thresholds, resolve_dataset, run_evaluation
from evals.scorer import EvaluationReport
from tenantchat.api.review import apply_eval_report
from tenantchat.api.store import ReviewQueueStore

_DEFAULT_EXCEPTIONS = Path(__file__).parent / "exceptions.json"


async def close_passing_reviews(
    reviews: ReviewQueueStore,
    tenant_id: str,
    *,
    run_id: str,
    report: Mapping[str, object],
) -> tuple[str, ...]:
    """Close every awaiting_fix review whose promoted case the report passes.

    The wiring for acceptance 5 of `FEAT-008`: the gate's candidate report is
    exactly the shape ``apply_eval_report`` reads (``components`` thresholds
    plus per-case rows), and the store's guard keeps the first passing run's
    reference immutable.
    """
    return await apply_eval_report(reviews, tenant_id, run_id=run_id, report=report)


def _run_pair(
    *,
    dataset: str,
    k: int,
    baseline: str,
    candidate: str,
    exceptions: ExceptionRegistry,
) -> tuple[ComparisonReport, DatasetSpec]:
    spec, corpus = resolve_dataset(dataset, k)
    thresholds = dataset_thresholds(spec)
    reports: dict[str, EvaluationReport] = {}
    for side, name in (("baseline", baseline), ("candidate", candidate)):
        entry = build_retriever_entry(
            name, corpus, k, cases=spec.cases, abstain_threshold_value=spec.abstain_threshold
        )
        reports[side] = asyncio.run(
            run_evaluation(
                retriever=entry.retriever,
                retriever_config=entry.config,
                corpus=corpus,
                cases=spec.cases,
                abstain_threshold_value=entry.abstain_threshold,
                min_recall=thresholds[0],
                min_citation_precision=thresholds[1],
                min_abstention=thresholds[2],
                min_grounding=thresholds[3],
                reranker=entry.reranker,
                parser_chunker=spec.parser_chunker,
                tenant_policy=spec.tenant_policy,
            )
        )
    comparison = compare_reports(
        reports["baseline"],
        reports["candidate"],
        spec,
        exceptions=exceptions,
    )
    return comparison, spec


def _seeded_subprocess_run(args: argparse.Namespace, *, seed: str, out: Path) -> int:
    """Re-run the pair in a fresh interpreter under a pinned hash seed.

    The spawned command is the gate itself minus ``--verify-determinism`` (it
    must not recurse), writing its report JSON to ``out``. The child's exit
    code is returned and its stderr forwarded, so a blocked or crashed run
    fails the parent exactly as if it had run here.
    """
    command = [
        "uv",
        "run",
        "--frozen",
        "python",
        "-m",
        "evals.gate",
        "--dataset",
        args.dataset,
        "--k",
        str(args.k),
        "--baseline-retriever",
        args.baseline_retriever,
        "--candidate-retriever",
        args.candidate_retriever,
        "--exceptions",
        str(args.exceptions),
        "--out",
        str(out),
    ]
    # Every element is a validated argparse constant; `uv run --frozen` is the
    # pinned toolchain the whole gate runs under, never user input.
    result = subprocess.run(  # noqa: S603
        command,
        env={**os.environ, "PYTHONHASHSEED": seed},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default="golden-v1", help="the versioned dataset to gate")
    parser.add_argument("--k", type=int, default=5, help="retrieval depth for recall@k")
    parser.add_argument(
        "--baseline-retriever", choices=("lexical-overlap", "hybrid"), default="lexical-overlap"
    )
    parser.add_argument(
        "--candidate-retriever", choices=("lexical-overlap", "hybrid"), default="hybrid"
    )
    parser.add_argument(
        "--exceptions",
        default=str(_DEFAULT_EXCEPTIONS),
        help="the reviewed-exception registry path",
    )
    parser.add_argument(
        "--verify-determinism",
        action="store_true",
        help="run the pair in two fresh interpreters under PYTHONHASHSEED "
        "1 and 42 and require byte-identical reports",
    )
    parser.add_argument("--out", default=None, help="write the comparison JSON to a file")
    args = parser.parse_args()

    if args.verify_determinism:
        return _verify_main(args)

    exceptions = ExceptionRegistry.load(Path(args.exceptions))
    comparison, _ = _run_pair(
        dataset=args.dataset,
        k=args.k,
        baseline=args.baseline_retriever,
        candidate=args.candidate_retriever,
        exceptions=exceptions,
    )
    sys.stdout.write(comparison.to_text() + "\n\n")
    sys.stdout.write("determinism: not verified\n\n")
    sys.stdout.write(comparison.to_json() + "\n")
    if args.out:
        Path(args.out).write_text(comparison.to_json() + "\n")
    if not comparison.gate.passed:
        sys.stderr.write("gate BLOCKED: candidate regressions below thresholds\n")
        return 1
    sys.stdout.write("gate passed\n")
    return 0


def _verify_main(args: argparse.Namespace) -> int:
    """The cross-process stability run: two fresh interpreters, two seeds.

    Both runs happen in fresh interpreters under pinned, different hash seeds
    and must produce byte-identical report JSON. Comparing one of them to an
    in-process run would also fold whatever ambient state this process carries
    (registered judges, prior imports) into the verdict; the two seeded runs
    are clean by construction.
    """
    with tempfile.TemporaryDirectory() as tmp:
        reports: dict[str, str] = {}
        for seed in ("1", "42"):
            out = Path(tmp) / f"report-{seed}.json"
            child_code = _seeded_subprocess_run(args, seed=seed, out=out)
            if child_code != 0:
                return child_code
            reports[seed] = out.read_text().rstrip("\n")
    if reports["1"] != reports["42"]:
        sys.stderr.write("determinism FAILED: the two seeded runs differ\n")
        return 1
    sys.stdout.write("determinism: exact (fresh interpreters under PYTHONHASHSEED 1 and 42)\n\n")
    sys.stdout.write(reports["1"] + "\n")
    if args.out:
        Path(args.out).write_text(reports["1"] + "\n")
    sys.stdout.write("gate passed\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
