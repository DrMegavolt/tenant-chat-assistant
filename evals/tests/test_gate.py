"""The gate's dataset contract and its judge-verdict entry point (`RAG-008`).

A dataset the gate cannot score honestly must fail the run loudly: a gold
chunk id that does not resolve in the corpus used to score like a deliberately
empty gold set (recall ``None``, the case silently excluded from the
aggregate), and a manifest truncated below a useful size gated vacuously on
whatever cases survived. Judge verdicts, in turn, enter the gate only through
the CLI flag — the hermetic gate invents none.

The probe manifests these tests use live only in a temporary directory: a
probe carries a ``source`` key, which is exactly what :func:`known_datasets`
counts as a dataset, so a probe written into the repository's datasets
directory would poison every later gate run that derives its dataset list
from there (a hard-killed test run never reaches its cleanup).
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from evals import dataset as eval_dataset
from evals import gate
from evals.dataset import DatasetError, known_datasets, validate_against_corpus
from evals.exceptions import ExceptionRegistry
from evals.runner import resolve_dataset


def _write_probe(tmp_path: Path, cases: list[dict[str, Any]]) -> None:
    manifest = {
        "name": "__gate_probe",
        "version": 1,
        "source": "hand-labelled",
        "abstain_threshold": 0.5,
        "thresholds": {"recall_at_k": 0.6},
        "pii_check": {"policy": "PRIV-002", "method": "probe"},
        "cases": cases,
    }
    (tmp_path / "__gate_probe.json").write_text(json.dumps(manifest), encoding="utf-8")


def _probe_datasets_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the dataset loader at the temporary tree, never the repository."""
    monkeypatch.setattr(eval_dataset, "_DATASETS_DIR", tmp_path)


def _probe_case(case_id: str, gold: list[str]) -> dict[str, Any]:
    return {
        "id": case_id,
        "tenant_id": "apex",
        "query": f"probe query for {case_id}",
        "gold_chunk_ids": gold,
        "expect_abstain": not gold,
        "citations": [],
        "scenario": "probe",
    }


def _run_pair(dataset: str) -> None:
    gate._run_pair(
        dataset=dataset,
        k=5,
        baseline="lexical-overlap",
        candidate="hybrid",
        exceptions=ExceptionRegistry(()),
    )


def _run_cli(*extra: str) -> tuple[int, str, str]:
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


def test_a_gold_chunk_id_outside_the_corpus_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_probe(tmp_path, [_probe_case("probe-1", ["no-such-chunk-id"])])
    _probe_datasets_dir(tmp_path, monkeypatch)

    with pytest.raises(DatasetError, match="no-such-chunk-id"):
        _run_pair("__gate_probe")


def test_a_dataset_below_the_case_floor_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_probe(tmp_path, [_probe_case("probe-1", [])])
    _probe_datasets_dir(tmp_path, monkeypatch)

    with pytest.raises(DatasetError, match="at least"):
        _run_pair("__gate_probe")


def test_the_cli_blocks_an_unscoreable_dataset_with_a_clean_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_probe(tmp_path, [_probe_case("probe-1", ["no-such-chunk-id"])])
    _probe_datasets_dir(tmp_path, monkeypatch)

    code, _stdout, stderr = _run_cli("--dataset", "__gate_probe")

    assert code == 1
    assert "gate BLOCKED" in stderr
    assert "no-such-chunk-id" in stderr


def test_a_probe_manifest_never_reaches_the_repository_datasets_dir(tmp_path: Path) -> None:
    """The probe declares a ``source``, which is what makes a manifest a
    dataset to ``known_datasets()`` — the Makefile's gate list. This is the
    regression: a probe written into the repository tree survived a hard-killed
    run and blocked every later `make check` with `gate BLOCKED: dataset
    '__gate_probe'`. The probe must exist only outside the tree, and the real
    dataset listing must be untouched after the probe is written and read."""
    real = known_datasets()
    assert "__gate_probe" not in real

    _write_probe(tmp_path, [_probe_case("probe-1", [])])
    with mock.patch.object(eval_dataset, "_DATASETS_DIR", tmp_path):
        assert known_datasets() == ("__gate_probe",)

    assert known_datasets() == real


def test_every_shipped_dataset_resolves_and_clears_the_case_floor() -> None:
    """New datasets ride the gate automatically (the Makefile derives the
    list from ``known_datasets()``), so one that ships unresolvable gold or
    too few cases must fail here before it ever reaches the gate."""
    for name in known_datasets():
        spec, corpus = resolve_dataset(name, 5)
        assert (
            validate_against_corpus(spec, [chunk.chunk_id for chunk in corpus.chunks]) == ()
        ), f"dataset {name!r} references chunks the corpus does not index"
        assert len(spec.cases) >= gate._MIN_GATE_CASES, (
            f"dataset {name!r} carries {len(spec.cases)} cases; "
            f"the gate requires at least {gate._MIN_GATE_CASES}"
        )


class TestJudgeVerdictEntry:
    """Judge verdicts ride the report; an unregistered one never gates."""

    def test_an_unregistered_judge_informs_without_gating(self, tmp_path: Path) -> None:
        out = tmp_path / "report.json"
        code, _stdout, stderr = _run_cli(
            "--dataset",
            "golden-v1",
            "--judge-regression",
            "ghost-judge=apex-hvac-heating-repair",
            "--out",
            str(out),
        )

        assert code == 0, stderr
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["gate"]["informational"][0]["state"] == "unregistered"
        assert report["judge_regressions"] == [
            {"judge": "ghost-judge", "case": "apex-hvac-heating-repair"}
        ]

    def test_a_malformed_judge_entry_is_refused_at_the_cli(self) -> None:
        with pytest.raises(SystemExit) as raised:
            _run_cli("--dataset", "golden-v1", "--judge-regression", "no-case-id")

        assert raised.value.code == 2
