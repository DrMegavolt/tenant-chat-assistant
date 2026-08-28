"""The gate's cross-process determinism check (`RAG-008` acceptance 3).

``--verify-determinism`` must prove what it claims: a second interpreter under
a different hash seed produces byte-identical report JSON. Two runs inside one
process cannot prove that — set iteration order is fixed at interpreter
startup by ``PYTHONHASHSEED`` — so the check must spawn a fresh interpreter.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals import gate


def _run_gate(*extra: str) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    argv = ["evals.gate", "--dataset", "golden-v1", *extra]
    with (
        mock.patch.object(sys, "argv", argv),
        contextlib.redirect_stdout(stdout),
        contextlib.redirect_stderr(stderr),
    ):
        code = gate.main()
    return code, stdout.getvalue(), stderr.getvalue()


class TestCrossProcessDeterminism(unittest.TestCase):
    def test_verify_determinism_reruns_the_pair_in_a_fresh_interpreter(self) -> None:
        code, stdout, stderr = _run_gate("--verify-determinism")

        self.assertEqual(code, 0, stderr)
        self.assertIn("determinism: exact", stdout)
        self.assertIn("gate passed", stdout)

    def test_a_changed_seeded_report_fails_the_check(self) -> None:
        def tampered(args: argparse.Namespace, *, seed: str, out: Path) -> int:
            out.write_text(f'{{"seed": {seed}}}\n')
            return 0

        with mock.patch("evals.gate._seeded_subprocess_run", side_effect=tampered):
            code, stdout, stderr = _run_gate("--verify-determinism")

        self.assertEqual(code, 1)
        self.assertIn("determinism FAILED", stderr)

    def test_the_seeded_run_spawns_a_fresh_gate_with_a_pinned_seed(self) -> None:
        args = argparse.Namespace(
            dataset="golden-v1",
            k=5,
            baseline_retriever="lexical-overlap",
            candidate_retriever="hybrid",
            exceptions="evals/exceptions.json",
            judge_regression=[],
        )
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "report.json"
            with mock.patch("evals.gate.subprocess.run") as spawned:
                spawned.return_value = mock.Mock(returncode=0, stderr="")
                code = gate._seeded_subprocess_run(args, seed="42", out=out)

        self.assertEqual(code, 0)
        command = spawned.call_args.args[0]
        self.assertEqual(command[:6], ["uv", "run", "--frozen", "python", "-m", "evals.gate"])
        self.assertNotIn("--verify-determinism", command, "the seeded run must not recurse")
        self.assertEqual(command[command.index("--out") + 1], str(out))
        self.assertEqual(spawned.call_args.kwargs["env"]["PYTHONHASHSEED"], "42")
        self.assertNotEqual(
            spawned.call_args.kwargs["env"]["PYTHONHASHSEED"],
            os.environ.get("PYTHONHASHSEED"),
            "the seeded run must use a different seed than this process",
        )


if __name__ == "__main__":
    unittest.main()
