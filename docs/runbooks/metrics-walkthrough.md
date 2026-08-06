# The OBS-002 metrics plane

Every critical action — a model call, a retrieval, a tool execution, a routing
decision, a booking, a lead, a handoff — records a success count, a failure
count, and a latency histogram under the request's trace exemplar. This page
is the inventory, the label policy, and the queries that read it.

## The inventory

All metric names start with `tenantchat_`, follow the Prometheus suffix
convention (`_total` counters, `_seconds` histograms), and are a closed enum in
`tenantchat.core.metrics.MetricName` plus a collector definition in
`tenantchat.api.metrics.METRIC_DEFINITIONS`. A metric that exists in one but
not the other fails the contract test.

| Metric | Type | Labels | Recorded at |
| ------ | ---- | ------ | ----------- |
| `tenantchat_turn_latency_seconds` | histogram | `operation` | chat router, per `send`/`resume` |
| `tenantchat_turn_outcomes_total` | counter | `outcome` | graph nodes + router |
| `tenantchat_llm_calls_total` | counter | `status`, `template` | model adapter wrapper |
| `tenantchat_llm_latency_seconds` | histogram | `status`, `template` | model adapter wrapper |
| `tenantchat_llm_tokens_total` | counter | `kind`, `template` | model adapter wrapper |
| `tenantchat_retrieval_runs_total` | counter | `status`, `verdict` | retrieval adapter |
| `tenantchat_retrieval_latency_seconds` | histogram | `status` | retrieval adapter |
| `tenantchat_retrieval_candidates_total` | counter | — | retrieval adapter |
| `tenantchat_tool_calls_total` | counter | `tool`, `outcome` | tools node / booking commit |
| `tenantchat_tool_latency_seconds` | histogram | `tool`, `outcome` | tools node / booking commit |
| `tenantchat_routing_decisions_total` | counter | `intent`, `outcome`, `rule` | routing node |
| `tenantchat_business_actions_total` | counter | `operation`, `status` | idempotent action services |
| `tenantchat_business_latency_seconds` | histogram | `operation` | idempotent action services |
| `tenantchat_citation_validation_total` | counter | `verdict` | chat router |

Label value vocabulary (closed):

- `operation`: `turn`, `booking`, `lead`, `handoff`
- `outcome` (turns): `answered`, `clarified`, `abstained`, `handed_off`, `paused`
- `outcome` (tools): `succeeded`, `refused`, `failed`
- `status`: `ok`, `error`, `timeout`, `unavailable` (LLM) and
  `committed`, `replayed`, `refused`, `declined` (business actions)
- `verdict`: `sufficient`, `insufficient` (retrieval) and `valid`, `invalid` (citations)
- `intent`: the `IntentName` enum, or `none` for a clarification
- `rule`: the `RoutingRule` enum; `tool`: the `ToolName` enum or `unknown`
- `kind`: `prompt`, `completion`, `total`; `template`: a registry ref such as `dispatch-system@3`

## The label policy

`ADR-0010` gives the operational plane identifiers and no content; the metrics
plane is the measurement half of that. The enforcement is layered:

1. **Call sites pass enum values or registry refs only.** The model wrapper
   labels a call by its assembled prompt's template ref; the tools node labels
   an unresolvable tool call `unknown` rather than the model's free text.
2. **The adapter refuses anything outside the bounded charset** — a value with
   a space, an uppercase letter, or most punctuation raises
   `MetricLabelError` instead of reaching the registry.
3. **Tests close the vocabulary.** `TestLabelSafety` runs real turns carrying
   names, addresses, phone numbers, and emails and asserts every sample a
   scraper would read carries only vocabulary values; the cardinality test
   asserts the whole reachable vocabulary stays under `METRIC_CARDINALITY_CEILING`,
   so a new enum member cannot quietly multiply series. The histogram `le`
   bucket labels are collector-owned and asserted against the library's bucket
   definitions.

No metric label carries a session ID, a tenant ID, or free text. The tenant
pseudonym is available from the correlation context but is not used as a label;
per-tenant series are a deliberate future widening, bounded by the pseudonym's
fixed length.

## Exemplar drill-through

Latency histograms attach the request's trace ID as an exemplar
(`# {trace_id="…"} <value>` in OpenMetrics), so a spike resolves to one turn.
Prometheus keeps the most recent exemplar per bucket, so the drill-through
lands on the request that produced the latest sample.

The scrape endpoint serves OpenMetrics (`application/openmetrics-text`) when
the scraper accepts it — the only format that carries exemplars — and classic
text otherwise:

```bash
curl -H 'Accept: application/openmetrics-text' http://localhost:8080/metrics
```

```text
tenantchat_llm_latency_seconds_bucket{le="0.5",status="ok",template="dispatch-system@3"} 1.0 # {trace_id="9f0c4b2a31e14bea9f4c7f2a01b64d19"} 0.34 1786046123.456
```

The exemplar is an identifier only: the content of that turn (prompt,
evidence, answer) lives in the inference plane behind the trace, governed by
`PRIV-002`.

## Sample queries

```promql
# Model error rate by template, per five minutes
rate(tenantchat_llm_calls_total{status="error"}[5m])
  / rate(tenantchat_llm_calls_total[5m])

# Provider timeout rate
rate(tenantchat_llm_calls_total{status="timeout"}[5m])

# Tokens by template (prompt + completion)
sum by (template) (rate(tenantchat_llm_tokens_total[5m]))

# Abstention rate — the retrieval verdict declining to answer
sum(rate(tenantchat_turn_outcomes_total{outcome="abstained"}[5m]))
  / sum(rate(tenantchat_turn_outcomes_total[5m]))

# Retrieval verdicts and outages
sum by (verdict) (rate(tenantchat_retrieval_runs_total[5m]))
sum by (status) (rate(tenantchat_retrieval_runs_total[5m]))

# Citation-validation failure rate
sum(rate(tenantchat_citation_validation_total{verdict="invalid"}[5m]))
  / sum(rate(tenantchat_citation_validation_total[5m]))

# Booking funnel: committed vs refused vs declined, exactly-once semantics
sum by (status) (rate(tenantchat_business_actions_total{operation="booking"}[5m]))

# p95 turn latency by operation
histogram_quantile(0.95, sum by (le, operation) (rate(tenantchat_turn_latency_seconds_bucket[5m])))

# Tool failures per tool
sum by (tool) (rate(tenantchat_tool_calls_total{outcome="failed"}[5m]))

# Clarification rate — the router declining to guess
sum(rate(tenantchat_routing_decisions_total{outcome="clarify"}[5m]))
  / sum(rate(tenantchat_routing_decisions_total[5m]))
```

## Recording semantics

- **Execution counts.** A graph node that re-runs after a crash re-observes
  the work it re-executed — that is the honest operational count, and the
  crash window between a node starting and its checkpoint landing is the only
  place a count can over-report.
- **Exactly-once business counts.** Bookings, leads, and handoffs are counted
  by the idempotent action services, which know whether an attempt was
  committed, answered a duplicate key (`replayed`), or refused — a replayed
  turn never inflates `committed`.
- **Paused turns.** A booking confirmation records `paused` once from the
  router; the resumed turn records `answered` from the graph, each under its
  own request's trace.

## Verification

- `services/api/tests/test_metrics.py` — the contract suite: every metric in
  the inventory has a collector definition; label names per metric are closed;
  free text and unknown dimensions are refused; success/error/timeout/refusal
  paths all record; exemplars resolve to the request that produced them; the
  end-to-end PII test asserts no label of any real turn carries contact
  details or free text; the cardinality ceiling is asserted.
- `packages/core/tests/test_metrics_port.py` — the core vocabulary: names are
  unique and namespaced, every value passes the bounded charset, and enums
  that share a label name are disjoint.
- `test_openapi_contract.py` — unchanged: `/metrics` is an operations surface
  served outside the OpenAPI document, like the side services' metrics routes.
