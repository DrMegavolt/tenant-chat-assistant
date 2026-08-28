"""The gate's dataset contract and its judge-verdict entry point (`RAG-008`).

A dataset the gate cannot score honestly must fail the run loudly: a gold
chunk id that does not resolve in the corpus used to score like a deliberately
empty gold set (recall ``None``, the case silently excluded from the
aggregate), and a manifest truncated below a useful size gated vacuously on
whatever cases survived. Judge verdicts, in turn, enter the gate only through
the CLI flag — the hermetic gate invents none.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals import gate
from evals.dataset import DatasetError, known_datasets, validate_against_corpus
from evals.exceptions import ExceptionRegistry
from evals.runner import resolve_dataset

_PROBE = Path("evals/datasets/__gate_probe.json")


def _write_probe(cases: list[dict[str, object]]) -> None:
    manifest = {
        "name": "__gate_probe",
        "version": 1,
        "source": "hand-labelled",
        "abstain_threshold": 0.5,
        "thresholds": {"recall_at_k": 0.6},
        "pii_check": {"policy": "PRIV-002", "method": "probe"},
        "cases": cases,
    }
    _PROBE.write_text(json.dumps(manifest), encoding="utf-8")


def _probe_case(case_id: str, gold: list[str]) -> dict[str, object]:
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


class TestGateDatasetContract(unittest.TestCase):
    """Every gate run scores a resolvable, large-enough dataset or refuses it."""

    def tearDown(self) -> None:
        _PROBE.unlink(missing_ok=True)

    def test_a_gold_chunk_id_outside_the_corpus_fails_the_run(self) -> None:
        _write_probe([_probe_case("probe-1", ["no-such-chunk-id"])])

        with self.assertRaisesRegex(DatasetError, "no-such-chunk-id"):
            _run_pair("__gate_probe")

    def test_a_dataset_below_the_case_floor_is_refused(self) -> None:
        _write_probe([_probe_case("probe-1", [])])

        with self.assertRaisesRegex(DatasetError, "at least"):
            _run_pair("__gate_probe")

    def test_the_cli_blocks_an_unscoreable_dataset_with_a_clean_error(self) -> None:
        _write_probe([_probe_case("probe-1", ["no-such-chunk-id"])])

        code, _stdout, stderr = _run_cli("--dataset", "__gate_probe")

        self.assertEqual(code, 1)
        self.assertIn("gate BLOCKED", stderr)
        self.assertIn("no-such-chunk-id", stderr)

    def test_every_shipped_dataset_resolves_and_clears_the_case_floor(self) -> None:
        """New datasets ride the gate automatically (the Makefile derives the
        list from ``known_datasets()``), so one that ships unresolvable gold or
        too few cases must fail here before it ever reaches the gate."""
        for name in known_datasets():
            with self.subTest(dataset=name):
                spec, corpus = resolve_dataset(name, 5)
                self.assertEqual(
                    validate_against_corpus(spec, [chunk.chunk_id for chunk in corpus.chunks]),
                    (),
                )
                self.assertGreaterEqual(len(spec.cases), gate._MIN_GATE_CASES)


class TestJudgeVerdictEntry(unittest.TestCase):
    """Judge verdicts ride the report; an unregistered one never gates."""

    def test_an_unregistered_judge_informs_without_gating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            code, _stdout, stderr = _run_cli(
                "--dataset",
                "golden-v1",
                "--judge-regression",
                "ghost-judge=apex-hvac-heating-repair",
                "--out",
                str(out),
            )

            self.assertEqual(code, 0, stderr)
            report = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(report["gate"]["informational"][0]["state"], "unregistered")
        self.assertEqual(
            report["judge_regressions"],
            [{"judge": "ghost-judge", "case": "apex-hvac-heating-repair"}],
        )

    def test_a_malformed_judge_entry_is_refused_at_the_cli(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            _run_cli("--dataset", "golden-v1", "--judge-regression", "no-case-id")

        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
