# L6 — Observability Dashboards as Code

The Grafana dashboards in `k8s/grafana/` provision the operational plane of
ADR-0010. Each dashboard resolves against the confirmed `tenantchat_*` metric
inventory (`packages/core/src/tenantchat/core/metrics.py`). Panels that require
metrics not yet emitted are marked with their lane dependency.

## Dashboards

| Dashboard | UID | Focus |
|---|---|---|
| **Chat Turn Outcomes** | `tenantchat-turn-outcomes` | Turn outcomes by class: answered, abstained, clarified, answer_refused, handed_off, paused, turn_failed. p95 latency by operation. |
| **Retrieval & Routing Quality** | `tenantchat-retrieval-routing` | Routing decisions by intent, retrieval verdicts, citation validation, retrieval latency. |
| **LLM Operations & Token Cost** | `tenantchat-llm-operations` | LLM call rate/error/latency by template, token consumption by kind, model fallback rate, cache hit rate. |
| **Exemplar → Trace → Explorer** | `tenantchat-exemplar-drillthrough` | Histogram bucket panels with trace_id exemplars, plus the drill-through workflow documentation. |
| **Safety & Governance** | `tenantchat-safety-governance` | Policy blocks by reason, budget alerts, feedback, response cache, dependency retries, business action funnel, tool failures. |

## Supported observability backends (current release)

Only the backends listed here are configured and expected to show application
data. Backends not listed may receive raw telemetry through the collector for
experimentation but are not part of the supported operator workflow.

| Backend | Status | Expected view |
|---|---|---|
| **Grafana** | Supported | Custom chat metrics via Prometheus (turn outcomes, LLM ops, routing, safety). Tenant Chat dashboards provisioned from `k8s/grafana/`. Metrics use a closed label set — no per-tenant breakdown. |
| **Tempo** | Supported | Trace search by trace ID, service name, or attribute. Every turn is one trace; drill from an exemplar in Grafana or a trace ID in the admin explorer. |
| **Phoenix** | Supported | GenAI trace grouping (LLM spans only — database/health spans form the bulk of trace count). Sessions group by `session.id`. LLM spans carry `gen_ai.*` semantic attributes and `openinference.span.kind = "LLM"` for categorization. Token counts and model identity appear on LLM spans; cost is computed by Phoenix from model pricing. |
| **Admin Explorer** | Supported | Turn record with prompt, evidence, output, verdicts, diagnosis. The inference plane is the authoritative content view per ADR-0010. |
| **MLflow** | Experimental | Experiment tracking over evaluation datasets. The current release sends metrics to MLflow through the OTLP HTTP exporter but does not configure a dedicated experiment — the `Default` experiment collects raw trace data. |
| **Pyroscope** | Not enabled | Application profiling is not configured. `observability/alloy` and `observability/pyroscope` system profiles appear, but the chat-backend process is not profiled. |

### What the generic APM panels show

The Generic Lightweight APM (Grafana's built-in HTTP/backend panels) expects
standard `http_server_*` metrics with a `service_name` label. Tenant Chat does
not emit these: it uses custom `tenantchat_*` metrics with a closed label set
(see above). The generic APM panels are therefore not expected to show
chat-backend data. Use the custom Grafana dashboards in `k8s/grafana/` instead.

### Which UI answers which question

The cluster runs five observability UIs. The split avoids turning the demo into
a tour of dashboards:

| Question | UI | Why |
|---|---|---|
| Rates and classes over time | **Grafana** | PromQL aggregates over Prometheus metrics — the operational plane's time-series answer |
| One request's shape and timing | **Tempo / Phoenix** | Span waterfall from OTLP traces — Tempo for trace search, Phoenix for GenAI attribute grouping |
| One turn's content and reasoning | **Admin Explorer (FEAT-015)** | Turn record in Postgres — the inference plane's authoritative answer with prompt, evidence, output, verdicts, and diagnosis causes |
| Evaluation experiments | **MLflow** | Experiment tracking over evaluation datasets — which prompt/model version wins for a given metric |

**Grafana** shows **trends and aggregates**. **Tempo/Phoenix** shows **one
request's structure**. The **admin explorer** shows **one turn's content** — the
prompt, evidence, and reasoning the operational plane deliberately excludes
(ADR-0010).

## L5 dependency notes

Two panels reference metrics that do not exist yet in the confirmed inventory:

| Panel | Metric needed | Lane |
|---|---|---|
| Router Confidence Distribution (p50/p95) | `tenantchat_router_confidence` histogram with `le` buckets | L5 |
| Diagnosis Causes Distribution | `tenantchat_diagnosis_causes_total` counter by `cause` label | L4/L5 |

The panel descriptions state the dependency explicitly. Once the emitting code
lands, the panels resolve with zero configuration change. Diagnosis causes are
currently stored in `turn_records.diagnosis_causes` (Postgres array) and are not
exposed as Prometheus metrics. Exposing them as a counter with a `cause` label
matching `DiagnosisCause` values (`stale_source`, `ingestion_or_index_error`,
`routing_error`, `query_rewrite_error`, `filter_exclusion`, `retrieval_miss`,
`retrieval_rank`, `context_truncation`, `prompt_regression`, `model_behavior`,
`grounding_or_citation_error`, `injection_quarantine`, `tool_error`,
`application_error`, `provider_failure`) would let this dashboard surface the
"diagnosis causes distribution" quality panel L6 targets.

## Provisioning

Provisioning runs automatically as the final step of `make deploy-local`. The
deployment calls `k8s/grafana/provision.sh --verify`, which creates ConfigMaps
and then polls the Grafana API until all five dashboard UIDs are confirmed
present. Re-running `deploy-local` or the script directly is safe — it replaces
the ConfigMaps with the latest JSON and re-verifies.

Provision manually (e.g. for a partial update without a full deployment):

```bash
./k8s/grafana/provision.sh --verify
```

Verify dashboards are present without re-provisioning:

```bash
make grafana-smoke
```

Set `GRAFANA_NAMESPACE` to override the namespace if Grafana runs elsewhere:

```bash
GRAFANA_NAMESPACE=monitoring ./k8s/grafana/provision.sh --verify
```

## Modifying dashboards

Edit `k8s/grafana/_generate_dashboards.py` — the canonical source — and re-run:

```bash
uv run python k8s/grafana/_generate_dashboards.py
```

The JSON files are the build artifact. They are checked in so that dashboard
changes are reviewable as JSON diffs and so that `kubectl create configmap
--from-file` can consume them directly. The generator script keeps the JSON
consistent, avoids hand-editing large JSON, and keeps the Python description
short enough to review panel structure without scrolling through nested dicts.

## Exported metrics (confirmed inventory)

All panels in dashboards 1–5 resolve against these metrics:

`tenantchat_turn_latency_seconds` (histogram, `operation`)  
`tenantchat_turn_outcomes_total` (counter, `outcome`)  
`tenantchat_llm_calls_total` (counter, `status`, `template`)  
`tenantchat_llm_latency_seconds` (histogram, `status`, `template`)  
`tenantchat_llm_tokens_total` (counter, `kind`, `template`)  
`tenantchat_retrieval_runs_total` (counter, `status`, `verdict`)  
`tenantchat_retrieval_latency_seconds` (histogram, `status`)  
`tenantchat_retrieval_candidates_total` (counter)  
`tenantchat_tool_calls_total` (counter, `tool`, `outcome`)  
`tenantchat_tool_latency_seconds` (histogram, `tool`, `outcome`)  
`tenantchat_node_latency_seconds` (histogram, `node`, `status`)  
`tenantchat_routing_decisions_total` (counter, `intent`, `outcome`, `rule`)  
`tenantchat_business_actions_total` (counter, `operation`, `status`)  
`tenantchat_business_latency_seconds` (histogram, `operation`)  
`tenantchat_citation_validation_total` (counter, `verdict`)  
`tenantchat_feedback_submitted_total` (counter, `rating`)  
`tenantchat_dependency_retries_total` (counter, `dependency`, `reason`)  
`tenantchat_circuit_state` (gauge, `dependency`, `state`)  
`tenantchat_policy_blocks_total` (counter, `reason`)  
`tenantchat_model_fallbacks_total` (counter, `reason`)  
`tenantchat_response_cache_total` (counter, `result`)  
`tenantchat_budget_alerts_total` (counter, `level`)

## Verification

- `make check` asserts the generator script and generated JSON are syntactically
  valid.
- Cluster acceptance: provision the dashboards, open Grafana, and verify each
  panel resolves. The `exemplar-drillthrough` dashboard's text panel documents the
  end-to-end drill-through workflow; verify an exemplar → trace → explorer round
  trip on the cluster.
