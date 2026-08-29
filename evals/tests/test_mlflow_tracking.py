"""The opt-in MLflow tracking of finished eval reports (`ML-01`).

Tracking is telemetry over a report that is already final, so these
specifications pin four boundaries. The unset env is a complete no-op — zero
client calls, byte-identical reports, unchanged exit codes — because the
hermetic gate and CI must never touch a network. The opted-in run carries
exactly the identifiers, versions, and metrics the task lists and no content
in any recorded field (the artifact's documented case-query exception is
asserted separately). A gate that never produced a final report records
nothing. And every client seam is a recording stub: no test reaches a server.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import functools
import io
import json
import logging
import re
import sys
from collections.abc import Mapping
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, cast
from unittest import mock

import pytest

from evals import gate, mlflow_tracking
from evals.compare import SCHEMA_VERSION, ComparisonReport, run_id
from evals.corpus import FixtureCorpus
from evals.dataset import DatasetSpec
from evals.exceptions import ExceptionRegistry
from evals.mlflow_tracking import TRACKING_URI_ENV, log_evaluation_run
from evals.runner import (
    build_retriever_entry,
    dataset_thresholds,
    resolve_dataset,
    run_evaluation,
)
from evals.scorer import EvaluationReport
from evals.versions import corpus_digest


class StubRun:
    """The context-manager handle one start_run call returns."""

    def __init__(self, run_id: str) -> None:
        self._run_id = run_id
        self.exit_exc: BaseException | None = None

    def __enter__(self) -> str:
        return self._run_id

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.exit_exc = exc_value
        return None


class StubClient:
    """A recording tracking client: every call is kept, nothing leaves the process."""

    def __init__(self) -> None:
        self.tracking_uris: list[str] = []
        self.experiments: list[str] = []
        self.run_names: list[str] = []
        self.tags: list[dict[str, str]] = []
        self.params: list[dict[str, str]] = []
        self.metrics: list[dict[str, float]] = []
        self.artifacts: list[tuple[str, str, str]] = []
        self.runs: list[StubRun] = []
        self.runs_started = 0

    def set_tracking_uri(self, tracking_uri: str) -> None:
        self.tracking_uris.append(tracking_uri)

    def set_experiment(self, experiment_name: str) -> object:
        self.experiments.append(experiment_name)
        return None

    def start_run(
        self, *, run_name: str | None = None, tags: Mapping[str, str] | None = None
    ) -> StubRun:
        self.runs_started += 1
        self.run_names.append(run_name or "")
        self.tags.append(dict(tags or {}))
        run = StubRun(f"stub-run-{self.runs_started}")
        self.runs.append(run)
        return run

    def log_params(self, params: Mapping[str, str]) -> None:
        self.params.append(dict(params))

    def log_metrics(self, metrics: Mapping[str, float]) -> None:
        self.metrics.append(dict(metrics))

    def log_artifact(self, local_path: str, *, artifact_path: str | None = None) -> None:
        # The content is read now, at log time: the tracker writes artifacts
        # into a temporary directory that is gone by assertion time.
        self.artifacts.append((local_path, artifact_path or "", Path(local_path).read_text()))

    def calls(self) -> int:
        """Every client call the tracker made, for the zero-call assertion."""
        return (
            len(self.tracking_uris)
            + len(self.experiments)
            + self.runs_started
            + len(self.params)
            + len(self.metrics)
            + len(self.artifacts)
        )


class FailingClient(StubClient):
    """A client whose upload fails after the run has started."""

    def log_metrics(self, metrics: Mapping[str, float]) -> None:
        raise RuntimeError("mlflow server unreachable")


if TYPE_CHECKING:
    # The stub must satisfy the protocol the tracker programs against.
    stub_conformance: mlflow_tracking.TrackingClient = StubClient()


def _install(monkeypatch: pytest.MonkeyPatch, *, uri: str | None) -> StubClient:
    """Point the tracker at a stub client and opt in (or not) via the env var."""
    stub = StubClient()
    monkeypatch.setattr(mlflow_tracking, "_resolve_client", lambda: stub)
    if uri is None:
        monkeypatch.delenv(TRACKING_URI_ENV, raising=False)
    else:
        monkeypatch.setenv(TRACKING_URI_ENV, uri)
    return stub


@functools.lru_cache(maxsize=1)
def _gate_fixtures() -> tuple[ComparisonReport, DatasetSpec, FixtureCorpus, ExceptionRegistry]:
    """One real gate comparison over the golden dataset, computed once."""
    registry = ExceptionRegistry.load()
    comparison, spec = gate._run_pair(
        dataset="golden-v1",
        k=5,
        baseline="lexical-overlap",
        candidate="hybrid",
        exceptions=registry,
    )
    _spec, corpus = resolve_dataset("golden-v1", 5)
    return comparison, spec, corpus, registry


@functools.lru_cache(maxsize=1)
def _adhoc_fixtures() -> tuple[EvaluationReport, DatasetSpec]:
    """One real scoreboard run over the golden dataset, computed once."""
    spec, corpus = resolve_dataset("golden-v1", 5)
    entry = build_retriever_entry(
        "lexical-overlap",
        corpus,
        5,
        cases=spec.cases,
        abstain_threshold_value=spec.abstain_threshold,
        vocabulary=spec.vocabulary,
    )
    min_recall, min_citation, min_abstention, min_grounding = dataset_thresholds(spec)
    report = asyncio.run(
        run_evaluation(
            retriever=entry.retriever,
            retriever_config=entry.config,
            corpus=corpus,
            cases=spec.cases,
            abstain_threshold_value=entry.abstain_threshold,
            min_recall=min_recall,
            min_citation_precision=min_citation,
            min_abstention=min_abstention,
            min_grounding=min_grounding,
            reranker=entry.reranker,
            parser_chunker=spec.parser_chunker,
            tenant_policy=spec.tenant_policy,
            vocabulary=spec.vocabulary,
        )
    )
    return report, spec


def _run_gate_cli(*extra: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    argv = ["evals.gate", *extra]
    with (
        mock.patch.object(sys, "argv", argv),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        code = gate.main()
    return code, stdout.getvalue(), stderr.getvalue()


def _expected_retriever_summary(side: Mapping[str, object]) -> str:
    components = cast("dict[str, object]", side["components"])
    retriever = cast("dict[str, object]", components["retriever"])
    return f"{retriever['name']}@{retriever['version']} k={retriever['k']}"


def _expected_retriever_params(side: Mapping[str, object]) -> str:
    components = cast("dict[str, object]", side["components"])
    retriever = cast("dict[str, object]", components["retriever"])
    return json.dumps(retriever["parameters"], sort_keys=True, separators=(",", ":"))


def _expected_template_ref(side: Mapping[str, object]) -> str | None:
    components = cast("dict[str, object]", side["components"])
    template = components.get("prompt_template")
    if not isinstance(template, Mapping):
        return None
    return f"{template['template_id']}@{template['version']}"


class TestOptIn:
    """The env var is the only switch, and off means completely off."""

    def test_unset_env_records_nothing_and_reports_stay_byte_identical(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        off_out = tmp_path / "off.json"
        stub = _install(monkeypatch, uri=None)
        code_off, stdout_off, stderr_off = _run_gate_cli(
            "--dataset", "golden-v1", "--out", str(off_out)
        )

        assert code_off == 0, stderr_off
        assert stub.calls() == 0, "a disabled tracker must never resolve a client"

        stub_on = _install(monkeypatch, uri="http://mlflow.test")
        on_out = tmp_path / "on.json"
        code_on, stdout_on, stderr_on = _run_gate_cli(
            "--dataset", "golden-v1", "--out", str(on_out)
        )

        assert code_on == code_off
        assert stub_on.tracking_uris == ["http://mlflow.test"]
        assert off_out.read_bytes() == on_out.read_bytes()
        assert stdout_off == stdout_on, "the printed report cannot move with tracking on"

    def test_unset_env_logs_exactly_one_debug_line(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        report, spec = _adhoc_fixtures()
        _install(monkeypatch, uri=None)
        with caplog.at_level(logging.DEBUG, logger="evals.mlflow_tracking"):
            result = log_evaluation_run(report, dataset=spec, role="adhoc")

        assert result is None
        disabled = [record for record in caplog.records if record.name == "evals.mlflow_tracking"]
        assert len(disabled) == 1
        assert TRACKING_URI_ENV in disabled[0].getMessage()

    def test_a_set_uri_without_mlflow_warns_and_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        report, spec = _adhoc_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        monkeypatch.setattr(mlflow_tracking, "_resolve_client", lambda: None)
        with caplog.at_level(logging.WARNING, logger="evals.mlflow_tracking"):
            result = log_evaluation_run(report, dataset=spec, role="adhoc")

        assert result is None
        assert stub.calls() == 0
        assert "uv sync --group evals" in caplog.text


class TestComparisonRun:
    """The gate's comparison becomes one run carrying the pinned surface."""

    def test_the_run_targets_the_experiment_with_the_documented_name(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comparison, spec, _corpus, _registry = _gate_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        result = log_evaluation_run(comparison, dataset=spec, role="candidate")

        assert result == "stub-run-1"
        assert stub.experiments == ["tenantchat-evals"]
        assert re.fullmatch(
            r"candidate/golden-v1/1/(?:[0-9a-f]{7}|unknown)", stub.run_names[0]
        ), stub.run_names[0]

    def test_the_params_carry_ids_versions_and_configs_not_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comparison, spec, corpus, registry = _gate_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        log_evaluation_run(
            comparison, dataset=spec, role="candidate", exceptions_digest=registry.digest()
        )

        params = stub.params[0]
        assert params["dataset_id"] == "golden-v1"
        assert params["dataset_version"] == str(spec.version)
        assert params["corpus_digest"] == corpus_digest(corpus)
        assert params["embedding_model"] == corpus.embedding_model
        assert params["baseline_retriever"] == _expected_retriever_summary(comparison.baseline)
        assert params["baseline_retriever_params"] == _expected_retriever_params(
            comparison.baseline
        )
        assert params["candidate_retriever"] == _expected_retriever_summary(comparison.candidate)
        assert params["candidate_retriever_params"] == _expected_retriever_params(
            comparison.candidate
        )
        assert params["scorer_version"] == str(SCHEMA_VERSION)
        assert "seeds 1 and 42" in params["python_hashseed"]
        assert params["exceptions_registry_digest"] == registry.digest()
        expected_ref = _expected_template_ref(comparison.candidate)
        assert params.get("template_ref") == expected_ref

    def test_the_metrics_carry_the_candidate_scores_and_gate_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comparison, spec, _corpus, _registry = _gate_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        log_evaluation_run(comparison, dataset=spec, role="candidate")

        candidate_scores = cast("dict[str, float]", comparison.candidate["scores"])
        metrics = stub.metrics[0]
        assert metrics["recall_mean"] == candidate_scores["recall_at_k"]
        assert metrics["precision_mean"] == candidate_scores["citation_precision"]
        assert metrics["abstain_rate"] == candidate_scores["abstention_correctness"]
        assert metrics["case_count"] == float(len(spec.cases))
        assert metrics["gate_pass"] == (1.0 if comparison.gate.passed else 0.0)
        assert metrics["delta_recall_at_k"] == comparison.aggregate_deltas["recall_at_k"]
        assert (
            metrics["delta_citation_precision"] == comparison.aggregate_deltas["citation_precision"]
        )
        assert metrics["regression_count"] == float(len(comparison.regressions))
        assert metrics["waiver_count"] == float(len(comparison.gate.exceptions_applied))

    def test_the_tags_join_mlflow_to_the_review_queue_through_gate_run_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comparison, spec, _corpus, _registry = _gate_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        log_evaluation_run(comparison, dataset=spec, role="candidate")

        tags = stub.tags[0]
        assert tags["dataset"] == "golden-v1"
        assert tags["role"] == "candidate"
        assert re.fullmatch(r"(?:[0-9a-f]{40}|unknown)", tags["git_sha"]), tags["git_sha"]
        assert re.fullmatch(r"eval-[0-9a-f]{16}", tags["gate_run_id"]), tags["gate_run_id"]
        assert tags["gate_run_id"] == cast("str", comparison.candidate["run_id"])
        assert tags["gate_run_id"] == run_id(
            cast("dict[str, object]", comparison.candidate["components"])
        )

    def test_the_artifact_is_exactly_the_final_report_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        stub = _install(monkeypatch, uri="http://mlflow.test")
        out = tmp_path / "verified.json"
        code, _stdout, stderr = _run_gate_cli("--verify-determinism", "--out", str(out))

        assert code == 0, stderr
        assert stub.runs_started == 1, "the seeded children never track; the parent logs once"
        artifact_path, artifact_target, artifact_content = stub.artifacts[0]
        assert artifact_target == "reports"
        assert artifact_path.endswith("comparison-report.json")
        assert (
            artifact_content == out.read_bytes().decode()
        ), "the tracked artifact must be the verified bytes themselves"
        tags = stub.tags[0]
        assert re.fullmatch(r"eval-[0-9a-f]{16}", tags["gate_run_id"])


class TestAdhocRun:
    """The scoreboard run logs as adhoc, without the gate-only surface."""

    def test_an_adhoc_report_carries_report_metrics_without_gate_keys(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, spec = _adhoc_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        result = log_evaluation_run(report, dataset=spec, role="adhoc")

        assert result == "stub-run-1"
        assert re.fullmatch(
            r"adhoc/golden-v1/\d+/(?:[0-9a-f]{7}|unknown)", stub.run_names[0]
        ), stub.run_names[0]
        metrics = stub.metrics[0]
        assert metrics["recall_mean"] == report.aggregate["recall_at_k"]
        assert metrics["precision_mean"] == report.aggregate["citation_precision"]
        assert metrics["abstain_rate"] == report.aggregate["abstention_correctness"]
        assert metrics["case_count"] == float(len(report.cases))
        assert metrics["passed"] == (1.0 if report.passed else 0.0)
        assert not any(key.startswith("delta_") for key in metrics), metrics
        assert "gate_pass" not in metrics
        assert "regression_count" not in metrics
        assert "waiver_count" not in metrics

    def test_an_adhoc_run_has_no_baseline_side_and_pins_its_own_gate_run_id(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, spec = _adhoc_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        log_evaluation_run(report, dataset=spec, role="adhoc")

        params = stub.params[0]
        assert "baseline_retriever" not in params
        assert "candidate_retriever" not in params
        assert params["retriever"] == (
            f"{report.retriever.name}@{report.retriever.version} k={report.retriever.k}"
        )
        assert params["thresholds"] == json.dumps(
            {
                "abstain_threshold": report.abstain_threshold,
                "min_recall": report.min_recall,
                "min_citation_precision": report.min_citation_precision,
                "min_abstention": report.min_abstention,
                "min_grounding": report.min_grounding,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        assert stub.tags[0]["gate_run_id"] == run_id(report.components)

    def test_the_adhoc_artifact_is_the_evaluation_report_json(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, spec = _adhoc_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        log_evaluation_run(report, dataset=spec, role="adhoc")

        artifact_path, _target, artifact_content = stub.artifacts[0]
        assert artifact_path.endswith("evaluation-report.json")
        assert artifact_content == report.to_json() + "\n"


class TestContentDiscipline:
    """No recorded field carries case or corpus content (the acceptance pin)."""

    def test_no_recorded_field_carries_case_or_corpus_content(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comparison, comparison_spec, corpus, _registry = _gate_fixtures()
        report, _spec = _adhoc_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        log_evaluation_run(comparison, dataset=comparison_spec, role="candidate")
        log_evaluation_run(report, dataset=comparison_spec, role="adhoc")

        fragments: list[str] = []
        for case in (*comparison_spec.cases, *(result.case for result in report.cases)):
            fragments.extend(
                text for text in (case.query, case.answer, *case.prior_turns) if text is not None
            )
        fragments.extend(chunk.text for chunk in corpus.chunks)
        lowered = [fragment.casefold() for fragment in fragments]

        recorded: list[str] = []
        for params in stub.params:
            recorded.extend(params)
            recorded.extend(params.values())
        for tags in stub.tags:
            recorded.extend(tags)
            recorded.extend(tags.values())
        recorded.extend(stub.run_names)
        recorded.extend(stub.experiments)
        for metrics in stub.metrics:
            recorded.extend(metrics)
            recorded.extend(str(value) for value in metrics)

        leaks = [
            (value, fragment)
            for value in recorded
            for fragment in lowered
            if fragment and fragment in value.casefold()
        ]
        assert leaks == [], "content reached MLflow params/metrics/tags"
        # The artifact is the documented exception: the comparison report
        # carries the case queries, acceptable while the corpus is sample
        # content — so the artifact content is deliberately not scanned here.

    def test_unscored_dimensions_are_omitted_not_logged_as_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        report, spec = _adhoc_fixtures()
        stub = _install(monkeypatch, uri="http://mlflow.test")
        log_evaluation_run(report, dataset=spec, role="adhoc")

        metrics = stub.metrics[0]
        for scored_key, metric_key in (
            ("recall_at_k", "recall_mean"),
            ("citation_precision", "precision_mean"),
        ):
            assert (metric_key in metrics) == report.scored.get(scored_key, True)
        assert "abstain_rate" in metrics, "abstention is always scored"


class TestGateBehaviour:
    """Tracking never moves the gate's verdict, and records only final reports."""

    def test_a_blocked_gate_records_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        stub = _install(monkeypatch, uri="http://mlflow.test")

        code, _stdout, stderr = _run_gate_cli("--dataset", "no-such-dataset")

        assert code == 1
        assert "gate BLOCKED" in stderr
        assert stub.calls() == 0, "a dataset that never produced a report has nothing to record"

    def test_a_tracking_failure_is_skipped_without_moving_the_verdict(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        failing = FailingClient()
        monkeypatch.setattr(mlflow_tracking, "_resolve_client", lambda: failing)
        monkeypatch.setenv(TRACKING_URI_ENV, "http://mlflow.test")
        with caplog.at_level(logging.WARNING, logger="evals.mlflow_tracking"):
            code, stdout, _stderr = _run_gate_cli("--dataset", "golden-v1")

        assert code == 0
        assert "gate passed" in stdout
        assert "RuntimeError" in caplog.text
        assert "mlflow tracking failed" in caplog.text

    def test_an_artifact_upload_failure_leaves_the_run_recorded_and_finished(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An artifact the client cannot deliver must not fail a recorded run.

        The real failure this pins: an experiment whose artifact root is a
        server-local path, so the upload raises after params, metrics, and the
        verdict already recorded. A run context that exits with that exception
        is marked FAILED on the server, contradicting its own gate_pass
        metric — the artifact is supplementary, so the run must finish without
        it and the warning must say what is missing.
        """

        class ArtifactFailingClient(StubClient):
            def log_artifact(self, local_path: str, *, artifact_path: str | None = None) -> None:
                raise OSError(30, "Read-only file system: '/mlflow-data'")

        failing = ArtifactFailingClient()
        monkeypatch.setattr(mlflow_tracking, "_resolve_client", lambda: failing)
        monkeypatch.setenv(TRACKING_URI_ENV, "http://mlflow.test")
        comparison, spec, _corpus, _registry = _gate_fixtures()
        with caplog.at_level(logging.WARNING, logger="evals.mlflow_tracking"):
            result = log_evaluation_run(comparison, dataset=spec, role="candidate")

        assert result == "stub-run-1"
        assert failing.runs[0].exit_exc is None, "the run must finish, not end FAILED"
        assert failing.params and failing.metrics, "the core record precedes the artifact"
        assert failing.artifacts == []
        assert "artifact logging failed" in caplog.text
        assert "the run is recorded without it" in caplog.text

    def test_the_seeded_determinism_children_never_track(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(TRACKING_URI_ENV, "http://mlflow.test")
        args = argparse.Namespace(
            dataset="golden-v1",
            k=5,
            baseline_retriever="lexical-overlap",
            candidate_retriever="hybrid",
            exceptions="evals/exceptions.json",
            judge_regression=[],
        )
        with mock.patch("evals.gate.subprocess.run") as spawned:
            spawned.return_value = mock.Mock(returncode=0, stderr="")
            code = gate._seeded_subprocess_run(args, seed="42", out=tmp_path / "report.json")

        assert code == 0
        child_env = spawned.call_args.kwargs["env"]
        assert TRACKING_URI_ENV not in child_env
        assert child_env["PYTHONHASHSEED"] == "42"
