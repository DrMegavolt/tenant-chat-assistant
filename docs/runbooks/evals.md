# The offline eval plane

The `evals/` package scores the RAG path on versioned datasets, hermetically —
no network, database, LLM, or embedding service — so it belongs to
`make check` exactly like the rest of the quality gate.

- `evals.runner` (`RAG-009`) is the single-run scoreboard: recall@k, citation
  precision, abstention correctness, and grounding over one dataset and
  retriever. `make eval` runs it for the baseline and the hybrid.
- `evals.gate` (`RAG-008`) is the release gate: the same dataset through a
  baseline and a candidate retriever, a comparison report (manifest diff,
  aggregate deltas, per-case regressions with provenance, judge table), the
  reviewed-exception registry applied on top, and a non-zero exit when the
  candidate is below threshold or regressed a case without a waiver.
  `make eval-gate` runs it over every dataset in `evals/datasets/` with
  `--verify-determinism`, which re-runs the pair in two fresh interpreters
  under `PYTHONHASHSEED` 1 and 42 and requires byte-identical reports.
- Thresholds come from each dataset manifest; `RAG_EVAL_MIN_*` env overrides
  tighten them for a release without editing a dataset.

## Comparing versions in MLflow (ML-01)

Finished reports land in MLflow as first-class runs when — and only when —
`EVAL_MLFLOW_TRACKING_URI` is set. Unset or empty, tracking is a no-op that
never touches the network, which is why the gate stays hermetic and green in
CI. The client is an optional dependency (the `evals` dependency group); the
evals package stays importable without it.

To compare two prompt/retriever versions on the local cluster's MLflow
(the `mlflow-lb` service, `http://192.168.1.183`):

```bash
uv sync --group evals   # once; installs mlflow-skinny
EVAL_MLFLOW_TRACKING_URI=http://192.168.1.183 make eval
EVAL_MLFLOW_TRACKING_URI=http://192.168.1.183 make eval
# ...change the retriever/template variant between the two runs, or:
EVAL_MLFLOW_TRACKING_URI=http://192.168.1.183 make eval-gate
```

Then open MLflow -> experiment `tenantchat-evals`, select the runs, Compare.
Runs are named `<role>/<dataset>/<dataset-version>/<short-sha>` (`adhoc/` for
scoreboard runs, `candidate/` for gate runs), so runs that set the same
params line up and the Compare view diffs what changed: the retriever
configs, thresholds, and template ref in params, and `recall_mean`,
`precision_mean`, `abstain_rate`, `gate_pass`, the `delta_*` movements,
regression count, and waiver count in metrics.

One identifier joins the three records: the report's candidate run id
(`eval-<hash16>`, derived from the pinned component manifest) is the same id
the FEAT-008 review closure (`apply_eval_report`) stamps on closed reviews
and the tracked run's `gate_run_id` tag — MLflow run, review queue, and
report all resolve to the same comparison.

## What MLflow receives

Identifiers, versions, and metrics only — no chunk text, no model output, no
visitor content. MLflow is the same measurement plane as the operational
metrics (`ADR-0010`): content lives in the inference trace store, governed by
`PRIV-002`. The one deliberate exception is the artifact: the tracked run
carries the comparison report JSON, which contains the case queries. The
corpus is sample content (every dataset passes the PRIV-002 gate at load), so
that is acceptable today; flip to a metrics-only summary if the corpus ever
stops being sample content. The artifact also needs an artifact root the
client can write — an experiment whose location is `mlflow-artifacts:/` (the
tracking server proxies it) or a filesystem shared with the client. An upload
failure is logged, the run stays recorded and finished without it, and the
params, metrics, and verdict are already on the server by then.

Tracking failures are logged and skipped — never flipped into the gate's
verdict — and the determinism verification stays byte-identical, because
recording happens strictly after a report is final. The seeded determinism
reruns never track, so one verified comparison records exactly one run.

Verification: `evals/tests/test_mlflow_tracking.py` — stub-client tests pin
the no-op behavior, the emitted params/metrics/tags, the no-content rule over
every recorded field, and unchanged gate exit codes with logging on and off.
