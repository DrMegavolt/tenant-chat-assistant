"""Opt-in MLflow tracking of finished evaluation reports (`ML-01`).

One call per final report turns a scored run into a first-class MLflow run, so
"which prompt/retriever version wins" is a side-by-side MLflow comparison
instead of a hand-read JSON diff. The entry point is:

    log_evaluation_run(report, *, dataset, role)

where ``report`` is an :class:`~evals.scorer.EvaluationReport` (a single
ad-hoc run), a :class:`~evals.compare.ComparisonReport` (a gate run), or the
comparison report's parsed JSON (the shape ``ComparisonReport.to_dict``
produces, which is what ``--verify-determinism`` holds after proving two
seeded reports byte-identical). ``role`` is ``"adhoc"`` for scoreboard runs
and ``"candidate"`` for the gate's comparison — the gate's run records the
candidate under test; the baseline rides the ``baseline_*`` params and the
``delta_*`` metrics.

**Opt-in only.** Nothing happens unless ``EVAL_MLFLOW_TRACKING_URI`` names a
tracking server (unset or empty: one debug log line, no client, no network),
so the hermetic gates and CI never touch MLflow. The client itself is an
optional dependency (the ``evals`` dependency group, ``mlflow-skinny``); a set
URI without the group installed is a logged skip, and a failed log call is a
logged warning — telemetry must never flip a verdict that has already been
recorded.

**Call placement is the invariant that keeps the deterministic path
deterministic.** Call this strictly *after* the report is final — after
``to_json`` bytes exist — never from inside ``run_evaluation`` /
``compare_reports``. Byte-identical reports stay byte-identical because
tracking cannot read or write them.

**What a run carries (ML-01.3)** — identifiers, versions, and metrics only,
the same two-plane discipline as the operational metrics (`ADR-0010`): no
chunk text, no model output, no visitor content.

- params: dataset id and version, ``corpus_digest``, embedding model, the
  baseline and candidate retriever configs (name, k, parameters, reranker),
  the score thresholds, the scorer (report schema) version, a
  ``PYTHONHASHSEED`` invariance note, the exceptions-registry digest when the
  caller gated against one, and the template ref when the manifest pins one.
- metrics: ``recall_mean``, ``precision_mean``, ``abstain_rate``, case count,
  ``gate_pass`` (0/1, comparisons) or ``passed`` (single runs), the
  ``delta_*`` aggregate movements baseline->candidate, regression count, and
  waiver count. Unscored dimensions are omitted rather than logged as a
  misleading zero, mirroring the scorer's own pass rule.
- tags: ``dataset``, ``role``, ``git_sha``, and ``gate_run_id`` — the
  server-minted ``eval-<hash16>`` id (``evals.compare.run_id``) that the
  FEAT-008 review closure stamps on closed reviews. One id joins the MLflow
  run, the review queue, and the report.
- artifact: the comparison report JSON under ``reports/``. The report carries
  the case queries; the corpus is sample content (every dataset passes the
  PRIV-002 gate at load), so that is acceptable today — flip to a
  metrics-only summary if the corpus ever stops being sample content.

**Convention (ML-01.4):** experiment ``tenantchat-evals``, run name
``<role>/<dataset>/<dataset-version>/<short-sha>``. Two runs that set the same
params — the thing the Compare view diffs — line up under the same name, so
comparing two retriever or template versions is: run ``make eval`` twice with
the variant, open MLflow -> tenantchat-evals -> select the runs -> Compare.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol

from evals.compare import SCHEMA_VERSION, ComparisonReport, run_id
from evals.dataset import DatasetSpec
from evals.scorer import EvaluationReport

type Role = Literal["baseline", "candidate", "adhoc"]

TRACKING_URI_ENV = "EVAL_MLFLOW_TRACKING_URI"
EXPERIMENT_NAME = "tenantchat-evals"
_ARTIFACT_PATH = "reports"

# A run is reproducible across interpreters (the gate proves it with pinned
# seeds), so the note travels with every run instead of an ambient seed value.
_HASHSEED_NOTE = (
    "invariant; --verify-determinism requires byte-identical reports under seeds 1 and 42"
)

_THRESHOLD_KEYS: tuple[str, ...] = (
    "abstain_threshold",
    "min_recall",
    "min_citation_precision",
    "min_abstention",
    "min_grounding",
)

_LOGGER = logging.getLogger(__name__)


class TrackingClient(Protocol):
    """The slice of the MLflow tracking API the tracker calls.

    A Protocol rather than the mlflow module type: the dependency is optional,
    so the real client's typing varies by environment, and the tests
    substitute a recording stub here.
    """

    def set_tracking_uri(self, tracking_uri: str) -> None: ...

    def set_experiment(self, experiment_name: str) -> object: ...

    def start_run(
        self, *, run_name: str | None = None, tags: Mapping[str, str] | None = None
    ) -> AbstractContextManager[str]: ...

    def log_params(self, params: Mapping[str, str]) -> None: ...

    def log_metrics(self, metrics: Mapping[str, float]) -> None: ...

    def log_artifact(self, local_path: str, *, artifact_path: str | None = None) -> None: ...


class _ActiveRun:
    """The run-id handle of one active mlflow run."""

    def __init__(self, active: Any) -> None:
        self._active = active

    def __enter__(self) -> str:
        return str(self._active.info.run_id)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        # Delegating ends the run (finished, or failed on error) because the
        # mlflow fluent run terminates itself when its context exits; returning
        # None keeps exceptions propagating.
        self._active.__exit__(exc_type, exc_value, traceback)


class _MlflowTracking:
    """The :class:`TrackingClient` bound to the real mlflow fluent API.

    The module arrives as ``Any`` on purpose: its presence — and therefore its
    typing — depends on the optional ``evals`` dependency group, and this
    adapter is the single place that touches it.
    """

    def __init__(self, module: Any) -> None:
        self._module = module

    def set_tracking_uri(self, tracking_uri: str) -> None:
        self._module.set_tracking_uri(tracking_uri)

    def set_experiment(self, experiment_name: str) -> object:
        return self._module.set_experiment(experiment_name)

    def start_run(
        self, *, run_name: str | None = None, tags: Mapping[str, str] | None = None
    ) -> AbstractContextManager[str]:
        return _ActiveRun(self._module.start_run(run_name=run_name, tags=dict(tags or {})))

    def log_params(self, params: Mapping[str, str]) -> None:
        self._module.log_params(dict(params))

    def log_metrics(self, metrics: Mapping[str, float]) -> None:
        self._module.log_metrics(dict(metrics))

    def log_artifact(self, local_path: str, *, artifact_path: str | None = None) -> None:
        self._module.log_artifact(local_path, artifact_path=artifact_path)


def _resolve_client() -> TrackingClient | None:
    """The mlflow-backed client, or ``None`` when the dependency is absent.

    The import is guarded because the default toolchain environment does not
    install the ``evals`` group: the evals package must stay importable
    without mlflow, exactly like the harness degrades optional version
    annotations in ``evals.versions``.
    """
    try:
        import mlflow
    except ImportError:
        return None
    return _MlflowTracking(mlflow)


@dataclass(frozen=True, slots=True)
class _RunPayload:
    """Everything one MLflow run needs, extracted before any client exists."""

    dataset_id: str
    dataset_version: str | None
    params: dict[str, str]
    metrics: dict[str, float]
    gate_run_id: str
    artifact_name: str
    artifact_json: str


def log_evaluation_run(
    report: EvaluationReport | ComparisonReport | Mapping[str, object],
    *,
    dataset: DatasetSpec | str,
    role: Role,
    exceptions_digest: str | None = None,
) -> str | None:
    """Record one final report as an MLflow run; the run id, or ``None``.

    Opt-in via ``EVAL_MLFLOW_TRACKING_URI``; disabled runs are a no-op that
    logs one debug line and never resolves a client. The caller passes the
    ``dataset`` it scored (the :class:`DatasetSpec` supplies id and version; a
    bare id logs without a version) and, when it gated against a reviewed
    exception registry, that registry's ``digest()``. Failures to record are
    logged and swallowed: the report is already final and its verdict stands.
    """
    tracking_uri = os.environ.get(TRACKING_URI_ENV, "").strip()
    if not tracking_uri:
        _LOGGER.debug("mlflow tracking disabled: %s is unset or empty", TRACKING_URI_ENV)
        return None
    if isinstance(report, EvaluationReport):
        payload = _evaluation_payload(report, dataset, exceptions_digest)
    elif isinstance(report, ComparisonReport):
        payload = _comparison_payload(report.to_dict(), dataset, exceptions_digest)
    else:
        payload = _comparison_payload(report, dataset, exceptions_digest)
    client = _resolve_client()
    if client is None:
        _LOGGER.warning(
            "mlflow tracking skipped: %s is set but mlflow is not installed "
            "(install it with `uv sync --group evals`)",
            TRACKING_URI_ENV,
        )
        return None
    try:
        return _emit(client, tracking_uri=tracking_uri, payload=payload, role=role)
    except Exception as error:
        # The report and its verdict are already final; a tracking outage is a
        # warning about telemetry, never a change to the recorded result.
        _LOGGER.warning(
            "mlflow tracking failed and was skipped: %s: %s", type(error).__name__, error
        )
        return None


def _emit(client: TrackingClient, *, tracking_uri: str, payload: _RunPayload, role: Role) -> str:
    client.set_tracking_uri(tracking_uri)
    client.set_experiment(EXPERIMENT_NAME)
    sha = _git_sha()
    run_name = f"{role}/{payload.dataset_id}/{payload.dataset_version or 'unknown'}/{sha[:7]}"
    tags = {
        "dataset": payload.dataset_id,
        "role": role,
        "git_sha": sha,
        "gate_run_id": payload.gate_run_id,
    }
    with client.start_run(run_name=run_name, tags=tags) as logged_run_id:
        client.log_params(payload.params)
        client.log_metrics(payload.metrics)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / payload.artifact_name
            path.write_text(payload.artifact_json + "\n", encoding="utf-8")
            client.log_artifact(str(path), artifact_path=_ARTIFACT_PATH)
        return logged_run_id


def _git_sha() -> str:
    """The commit the run's code came from, ``unknown`` outside a repository.

    Best-effort by design: a run recorded without a sha is still worth having,
    and a version annotation must never fail a finished evaluation.
    """
    command = ["git", "rev-parse", "HEAD"]
    # The arguments are fixed constants, never input; `git` is the one external
    # binary the tracker may shell out to, and a missing one degrades to
    # `unknown` below.
    result = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        check=False,
    )
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unknown"


def _evaluation_payload(
    report: EvaluationReport, dataset: DatasetSpec | str, exceptions_digest: str | None
) -> _RunPayload:
    components = report.components
    params = _dataset_params(dataset)
    build = _mapping(components.get("build"))
    params["corpus_digest"] = _text(build.get("corpus_digest")) or "unknown"
    params["embedding_model"] = report.embedding_model
    params |= _retriever_params("", components)
    params["thresholds"] = _thresholds_param(components)
    params["scorer_version"] = str(SCHEMA_VERSION)
    params["python_hashseed"] = _HASHSEED_NOTE
    ref = _template_ref(components)
    if ref is not None:
        params["template_ref"] = ref
    if exceptions_digest is not None:
        params["exceptions_registry_digest"] = exceptions_digest
    metrics: dict[str, float] = {}
    if report.scored.get("recall_at_k", True):
        metrics["recall_mean"] = report.aggregate["recall_at_k"]
    if report.scored.get("citation_precision", True):
        metrics["precision_mean"] = report.aggregate["citation_precision"]
    metrics["abstain_rate"] = report.aggregate["abstention_correctness"]
    metrics["case_count"] = float(len(report.cases))
    metrics["passed"] = 1.0 if report.passed else 0.0
    return _RunPayload(
        dataset_id=params["dataset_id"],
        dataset_version=params.get("dataset_version"),
        params=params,
        metrics=metrics,
        gate_run_id=run_id(components),
        artifact_name="evaluation-report.json",
        artifact_json=report.to_json(),
    )


def _comparison_payload(
    report: Mapping[str, object], dataset: DatasetSpec | str, exceptions_digest: str | None
) -> _RunPayload:
    baseline = _mapping(report.get("baseline"))
    candidate = _mapping(report.get("candidate"))
    baseline_components = _mapping(baseline.get("components"))
    candidate_components = _mapping(candidate.get("components"))
    params = _dataset_params(dataset)
    params["corpus_digest"] = (
        _text(_mapping(candidate_components.get("build")).get("corpus_digest")) or "unknown"
    )
    params["embedding_model"] = _text(candidate_components.get("embedding")) or "unknown"
    params |= _retriever_params("baseline", baseline_components)
    params |= _retriever_params("candidate", candidate_components)
    params["thresholds"] = _thresholds_param(candidate_components)
    schema_version = report.get("schema_version")
    if schema_version is None:
        schema_version = SCHEMA_VERSION
    params["scorer_version"] = str(schema_version)
    params["python_hashseed"] = _HASHSEED_NOTE
    candidate_ref = _template_ref(candidate_components)
    if candidate_ref is not None:
        params["template_ref"] = candidate_ref
    baseline_ref = _template_ref(baseline_components)
    if baseline_ref is not None and baseline_ref != candidate_ref:
        params["baseline_template_ref"] = baseline_ref
    if exceptions_digest is not None:
        params["exceptions_registry_digest"] = exceptions_digest

    gate_body = _mapping(report.get("gate"))
    scores = _mapping(candidate.get("scores"))
    scored = _mapping(candidate.get("scored"))
    metrics: dict[str, float] = {}
    if scored.get("recall_at_k", True):
        recall = _float(scores.get("recall_at_k"))
        if recall is not None:
            metrics["recall_mean"] = recall
    if scored.get("citation_precision", True):
        precision = _float(scores.get("citation_precision"))
        if precision is not None:
            metrics["precision_mean"] = precision
    abstain = _float(scores.get("abstention_correctness"))
    if abstain is not None:
        metrics["abstain_rate"] = abstain
    if isinstance(dataset, DatasetSpec):
        metrics["case_count"] = float(len(dataset.cases))
    metrics["gate_pass"] = 1.0 if gate_body.get("passed") is True else 0.0
    for metric, value in _mapping(report.get("aggregate_deltas")).items():
        delta = _float(value)
        if delta is not None:
            metrics[f"delta_{metric}"] = delta
    metrics["regression_count"] = float(_count(report.get("regressions")))
    metrics["waiver_count"] = float(_count(gate_body.get("exceptions_applied")))
    return _RunPayload(
        dataset_id=params["dataset_id"],
        dataset_version=params.get("dataset_version"),
        params=params,
        metrics=metrics,
        gate_run_id=_text(candidate.get("run_id")) or run_id(candidate_components),
        artifact_name="comparison-report.json",
        artifact_json=json.dumps(report, indent=2, sort_keys=True),
    )


def _dataset_params(dataset: DatasetSpec | str) -> dict[str, str]:
    if isinstance(dataset, DatasetSpec):
        return {"dataset_id": dataset.name, "dataset_version": str(dataset.version)}
    return {"dataset_id": dataset}


def _retriever_params(prefix: str, components: Mapping[str, object]) -> dict[str, str]:
    """One side's retriever config as retriever params.

    The summary (``name@version k``) is what the Compare view aligns across
    runs; the canonical JSON of the tuned parameters is what it diffs. A gate
    comparison carries both sides under ``baseline_*``/``candidate_*``; a
    single ad-hoc report has one side, so the keys go unprefixed.
    """
    key = f"{prefix}_" if prefix else ""
    retriever = _mapping(components.get("retriever"))
    name = _text(retriever.get("name")) or "unknown"
    version = _text(retriever.get("version")) or "unknown"
    k = _int(retriever.get("k"))
    return {
        f"{key}retriever": f"{name}@{version} k={k if k is not None else 'unknown'}",
        f"{key}retriever_params": _canonical(retriever.get("parameters")),
        f"{key}reranker": _text(components.get("reranker")) or "none",
    }


def _thresholds_param(components: Mapping[str, object]) -> str:
    return _canonical({key: components.get(key) for key in _THRESHOLD_KEYS})


def _template_ref(components: Mapping[str, object]) -> str | None:
    """The pinned prompt template as a registry ref (``dispatch-system@N``)."""
    template = _mapping(components.get("prompt_template"))
    template_id = _text(template.get("template_id"))
    version = template.get("version")
    if template_id is None or version is None:
        return None
    return f"{template_id}@{version}"


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _float(value: object) -> float | None:
    if isinstance(value, int | float) and not isinstance(value, bool):
        return float(value)
    return None


def _int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def _count(value: object) -> int:
    return len(value) if isinstance(value, list) else 0
