"""Generate Grafana dashboard JSON files from a compact Python description.

This script is the canonical source of the L6 Grafana dashboards. It writes
complete dashboard JSON files into k8s/grafana/ so the dashboards can be
provisioned as ConfigMaps. Run it from the repo root, then apply the generated
JSON through k8s/grafana/provision.sh.

Every panel resolves against a metric that exists in the inventory
(tenantchat.core.metrics.MetricName) except where explicitly marked as an
L4/L5 dependency.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent


def dash(title, uid, description, tags, panels):
    return {
        "title": title,
        "uid": uid,
        "description": description,
        "tags": tags,
        "timezone": "browser",
        "schemaVersion": 39,
        "refresh": "30s",
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "type": "datasource",
                    "query": "prometheus",
                    "current": {},
                    "hide": 0,
                    "label": "Data source",
                }
            ]
        },
        "panels": panels,
    }


def ts_panel(
    id_: int,
    title: str,
    x,
    y,
    w,
    h,
    targets: list[dict],
    *,
    unit: str = "reqps",
    overrides: list | None = None,
    draw: str = "line",
    fill: int = 10,
    stack: bool = False,
    desc: str = "",
) -> dict:
    fc: dict = {
        "defaults": {
            "unit": unit,
            "custom": {
                "drawStyle": draw,
                "lineInterpolation": "smooth",
                "fillOpacity": fill,
                "showPoints": "never",
                "stacking": {"mode": "normal"} if stack else {"mode": "none"},
            },
        },
    }
    if overrides:
        fc["overrides"] = overrides
    p: dict = {
        "id": id_,
        "title": title,
        "type": "timeseries",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": fc,
        "options": {
            "legend": {
                "displayMode": "table",
                "placement": "bottom",
                "calcs": ["mean", "lastNotNull"],
            },
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }
    if desc:
        p["description"] = desc
    return p


def stat_panel(
    id_: int,
    title: str,
    x,
    y,
    w,
    h,
    expr: str,
    *,
    unit: str = "percent",
    decimals: int = 1,
    thresholds: list | None = None,
    color_mode: str = "background",
    graph_mode: str = "area",
) -> dict:
    if thresholds is None:
        thresholds = [
            {"color": "green", "value": None},
        ]
    return {
        "id": id_,
        "title": title,
        "type": "stat",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"expr": expr, "refId": "A"}],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "decimals": decimals,
                "thresholds": {"mode": "absolute", "steps": thresholds},
            }
        },
        "options": {
            "textMode": "auto",
            "colorMode": color_mode,
            "graphMode": graph_mode,
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"]},
        },
    }


# --- OUTCOME COLOUR OVERRIDES ---
OUTCOME_COLORS = [
    ("answered", "green"),
    ("abstained", "orange"),
    ("clarified", "blue"),
    ("answer_refused", "red"),
    ("handed_off", "purple"),
    ("paused", "yellow"),
    ("turn_failed", "dark-red"),
]

OUTCOME_OVERRIDES = [
    {
        "matcher": {"id": "byName", "options": name},
        "properties": [{"id": "color", "value": {"fixedColor": c, "mode": "fixed"}}],
    }
    for name, c in OUTCOME_COLORS
]

RATE = "$__rate_interval"


# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 1 — TURN OUTCOMES
# ═══════════════════════════════════════════════════════════════════════════════
TURN_OUTCOMES = dash(
    title="Chat Turn Outcomes",
    uid="tenantchat-turn-outcomes",
    description="Turn outcomes by class — every terminal path is a panel. Summing across the outcome label equals total turns completed.",
    tags=["tenantchat", "quality", "turn"],
    panels=[
        ts_panel(
            1,
            "Turn Outcomes — Rate by Class",
            0,
            0,
            24,
            10,
            [
                {
                    "expr": f"sum by (outcome) (rate(tenantchat_turn_outcomes_total[{RATE}]))",
                    "legendFormat": "{{outcome}}",
                    "refId": "A",
                }
            ],
            overrides=OUTCOME_OVERRIDES,
        ),
        stat_panel(
            2,
            'Answered Rate — "The System Is Responding"',
            0,
            10,
            6,
            6,
            f'sum(rate(tenantchat_turn_outcomes_total{{outcome="answered"}}[{RATE}])) / sum(rate(tenantchat_turn_outcomes_total[{RATE}])) * 100',
            thresholds=[
                {"color": "red"},
                {"color": "orange", "value": 70.0},
                {"color": "green", "value": 90.0},
            ],
        ),
        stat_panel(
            3,
            'Abstention Rate — "Retrieval Verdict Refused"',
            6,
            10,
            6,
            6,
            f'sum(rate(tenantchat_turn_outcomes_total{{outcome="abstained"}}[{RATE}])) / sum(rate(tenantchat_turn_outcomes_total[{RATE}])) * 100',
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 5.0},
                {"color": "red", "value": 15.0},
            ],
        ),
        stat_panel(
            4,
            'Clarification Rate — "Router Declined to Guess"',
            12,
            10,
            6,
            6,
            f'sum(rate(tenantchat_turn_outcomes_total{{outcome="clarified"}}[{RATE}])) / sum(rate(tenantchat_turn_outcomes_total[{RATE}])) * 100',
            thresholds=[
                {"color": "green"},
                {"color": "blue", "value": 5.0},
                {"color": "orange", "value": 20.0},
            ],
        ),
        stat_panel(
            5,
            "Failure Rate — answer_refused + turn_failed",
            18,
            10,
            6,
            6,
            f'sum(rate(tenantchat_turn_outcomes_total{{outcome=~"answer_refused|turn_failed"}}[{RATE}])) / sum(rate(tenantchat_turn_outcomes_total[{RATE}])) * 100',
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 1.0},
                {"color": "red", "value": 5.0},
            ],
        ),
        ts_panel(
            6,
            "Turn Outcomes — Stacked Over Time",
            0,
            16,
            12,
            8,
            [
                {
                    "expr": f"sum by (outcome) (rate(tenantchat_turn_outcomes_total[{RATE}]))",
                    "legendFormat": "{{outcome}}",
                    "refId": "A",
                }
            ],
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "answered"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "turn_failed"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "dark-red", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "answer_refused"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}}
                    ],
                },
            ],
            stack=True,
        ),
        ts_panel(
            7,
            "Turn Outcomes — Ancillary Breakdown",
            12,
            16,
            12,
            8,
            [
                {
                    "expr": f'sum(rate(tenantchat_turn_outcomes_total{{outcome="handed_off"}}[{RATE}]))',
                    "legendFormat": "handed_off",
                    "refId": "A",
                },
                {
                    "expr": f'sum(rate(tenantchat_turn_outcomes_total{{outcome="paused"}}[{RATE}]))',
                    "legendFormat": "paused",
                    "refId": "B",
                },
            ],
        ),
        ts_panel(
            8,
            "p95 Turn Latency by Operation",
            0,
            24,
            24,
            8,
            [
                {
                    "expr": f"histogram_quantile(0.95, sum by (le, operation) (rate(tenantchat_turn_latency_seconds_bucket[{RATE}])))",
                    "legendFormat": "{{operation}}",
                    "refId": "A",
                }
            ],
            unit="s",
        ),
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 2 — RETRIEVAL & ROUTING QUALITY
# ═══════════════════════════════════════════════════════════════════════════════
RETRIEVAL_ROUTING = dash(
    title="Retrieval & Routing Quality",
    uid="tenantchat-retrieval-routing",
    description="Routing decisions, retrieval verdicts, and citation validation — the quality classes that carry the 'quality by class, not volume' narrative.",
    tags=["tenantchat", "quality", "retrieval", "routing"],
    panels=[
        ts_panel(
            1,
            "Routing Decisions — Rate by Intent",
            0,
            0,
            24,
            10,
            [
                {
                    "expr": f"sum by (intent) (rate(tenantchat_routing_decisions_total[{RATE}]))",
                    "legendFormat": "{{intent}}",
                    "refId": "A",
                }
            ],
        ),
        {
            "id": 2,
            "title": "Routing Decision Classes",
            "type": "piechart",
            "gridPos": {"x": 0, "y": 10, "w": 8, "h": 7},
            "targets": [
                {
                    "expr": f"sum by (outcome) (rate(tenantchat_routing_decisions_total[{RATE}]))",
                    "legendFormat": "{{outcome}}",
                    "refId": "A",
                }
            ],
            "options": {
                "displayLabels": ["name", "percent"],
                "legend": {"displayMode": "table", "placement": "right"},
            },
        },
        stat_panel(
            3,
            "Clarification Rate — Router Declining to Guess",
            8,
            10,
            4,
            3,
            f'sum(rate(tenantchat_routing_decisions_total{{outcome="clarify"}}[{RATE}])) / sum(rate(tenantchat_routing_decisions_total[{RATE}])) * 100',
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 5.0},
                {"color": "red", "value": 20.0},
            ],
        ),
        ts_panel(
            4,
            "Router Confidence Distribution",
            12,
            10,
            12,
            4,
            [
                {
                    "expr": f"histogram_quantile(0.50, sum by (le) (rate(tenantchat_router_confidence_bucket[{RATE}])))",
                    "legendFormat": "p50",
                    "refId": "A",
                },
                {
                    "expr": f"histogram_quantile(0.95, sum by (le) (rate(tenantchat_router_confidence_bucket[{RATE}])))",
                    "legendFormat": "p95",
                    "refId": "B",
                },
            ],
            unit="percentunit",
            desc="L5 dependency: metric tenantchat_router_confidence does not exist yet. Panel will resolve once L5 emits the histogram.",
        ),
        ts_panel(
            5,
            "Retrieval Runs — by Verdict",
            0,
            17,
            12,
            8,
            [
                {
                    "expr": f"sum by (verdict) (rate(tenantchat_retrieval_runs_total[{RATE}]))",
                    "legendFormat": "{{verdict}}",
                    "refId": "A",
                },
                {
                    "expr": f"sum by (status) (rate(tenantchat_retrieval_runs_total[{RATE}]))",
                    "legendFormat": "status={{status}}",
                    "refId": "B",
                },
            ],
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "sufficient"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "insufficient"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}}
                    ],
                },
            ],
        ),
        ts_panel(
            6,
            "Citation Validation Rate",
            12,
            17,
            6,
            4,
            [
                {
                    "expr": f"sum by (verdict) (rate(tenantchat_citation_validation_total[{RATE}]))",
                    "legendFormat": "{{verdict}}",
                    "refId": "A",
                }
            ],
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "valid"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "invalid"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}}
                    ],
                },
            ],
        ),
        stat_panel(
            7,
            "Citation Invalid Rate",
            18,
            17,
            6,
            4,
            f'sum(rate(tenantchat_citation_validation_total{{verdict="invalid"}}[{RATE}])) / sum(rate(tenantchat_citation_validation_total[{RATE}])) * 100',
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 5.0},
                {"color": "red", "value": 15.0},
            ],
        ),
        ts_panel(
            8,
            "Retrieval — Candidates Total Rate",
            0,
            25,
            12,
            6,
            [
                {
                    "expr": f"rate(tenantchat_retrieval_candidates_total[{RATE}])",
                    "legendFormat": "candidates/s",
                    "refId": "A",
                }
            ],
            draw="bars",
            fill=80,
        ),
        ts_panel(
            9,
            "p95 Retrieval Latency by Status",
            12,
            25,
            12,
            6,
            [
                {
                    "expr": f"histogram_quantile(0.95, sum by (le, status) (rate(tenantchat_retrieval_latency_seconds_bucket[{RATE}])))",
                    "legendFormat": "{{status}}",
                    "refId": "A",
                }
            ],
            unit="s",
        ),
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 3 — LLM OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════════
LLM_OPS = dash(
    title="LLM Operations & Token Cost",
    uid="tenantchat-llm-operations",
    description="LLM call rates, error rates, latency, and token consumption — the model plane in the operational metrics surface. Template refs (dispatch-system@N) separate versioned prompts.",
    tags=["tenantchat", "llm", "cost", "performance"],
    panels=[
        ts_panel(
            1,
            "LLM Calls — Rate by Template & Status",
            0,
            0,
            24,
            10,
            [
                {
                    "expr": f"sum by (template, status) (rate(tenantchat_llm_calls_total[{RATE}]))",
                    "legendFormat": "{{template}}/{{status}}",
                    "refId": "A",
                },
            ],
            desc="Every model adapter call is labelled by assembled prompt template ref and status (ok/error/timeout/unavailable).",
        ),
        stat_panel(
            2,
            "LLM Error Rate",
            0,
            10,
            4,
            4,
            f'sum(rate(tenantchat_llm_calls_total{{status="error"}}[{RATE}])) / sum(rate(tenantchat_llm_calls_total[{RATE}])) * 100',
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 1.0},
                {"color": "red", "value": 5.0},
            ],
        ),
        stat_panel(
            3,
            "LLM Timeout Rate",
            4,
            10,
            4,
            4,
            f'sum(rate(tenantchat_llm_calls_total{{status="timeout"}}[{RATE}])) / sum(rate(tenantchat_llm_calls_total[{RATE}])) * 100',
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 1.0},
                {"color": "red", "value": 3.0},
            ],
        ),
        stat_panel(
            4,
            "Model Fallback Rate",
            8,
            10,
            4,
            4,
            f"sum(rate(tenantchat_model_fallbacks_total[{RATE}])) / sum(rate(tenantchat_llm_calls_total[{RATE}])) * 100",
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 1.0},
                {"color": "red", "value": 5.0},
            ],
        ),
        stat_panel(
            5,
            "Response Cache Hit Rate",
            12,
            10,
            4,
            4,
            f'sum(rate(tenantchat_response_cache_total{{result="hit"}}[{RATE}])) / sum(rate(tenantchat_response_cache_total[{RATE}])) * 100',
            thresholds=[
                {"color": "red", "value": None},
                {"color": "green"},
                {"color": "green", "value": 1.0},
            ],
        ),
        ts_panel(
            6,
            "Token Rate — by Kind",
            0,
            14,
            12,
            8,
            [
                {
                    "expr": f"sum by (kind) (rate(tenantchat_llm_tokens_total[{RATE}]))",
                    "legendFormat": "{{kind}}",
                    "refId": "A",
                },
            ],
            desc="prompt = input tokens, completion = output tokens, total = prompt + completion.",
            unit="short",
        ),
        ts_panel(
            7,
            "Token Rate — by Template",
            12,
            14,
            12,
            8,
            [
                {
                    "expr": f"sum by (template) (rate(tenantchat_llm_tokens_total[{RATE}]))",
                    "legendFormat": "{{template}}",
                    "refId": "A",
                }
            ],
            unit="short",
            desc="Breakdown of token consumption per prompt template ref. High completion tokens from one template may signal verbose responses.",
        ),
        ts_panel(
            8,
            "p95 LLM Latency by Template",
            0,
            22,
            24,
            8,
            [
                {
                    "expr": f"histogram_quantile(0.95, sum by (le, template) (rate(tenantchat_llm_latency_seconds_bucket[{RATE}])))",
                    "legendFormat": "{{template}}",
                    "refId": "A",
                }
            ],
            unit="s",
        ),
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 4 — EXEMPLAR DRILL-THROUGH
# ═══════════════════════════════════════════════════════════════════════════════
EXEMPLAR = dash(
    title="Exemplar → Trace → Explorer Drill-Through",
    uid="tenantchat-exemplar-drillthrough",
    description="The beat that ties the operational plane to the inference plane: latency histograms carry trace_id exemplars. Click a histogram bar → inspect the exemplar → open Tempo with the trace_id → open the admin explorer with the same trace_id to see the turn's content and reasoning. Requires a Tempo datasource configured in Grafana (auto-discovered by kube-prometheus-stack when Tempo is in the same cluster).",
    tags=["tenantchat", "exemplar", "trace", "drillthrough"],
    panels=[
        ts_panel(
            1,
            "Turn Latency Histogram — p95 with Exemplars",
            0,
            0,
            24,
            8,
            [
                {
                    "expr": f"histogram_quantile(0.95, sum by (le, operation) (rate(tenantchat_turn_latency_seconds_bucket[{RATE}])))",
                    "legendFormat": "{{operation}}",
                    "refId": "A",
                }
            ],
            unit="s",
            desc="Each histogram bucket carries the latest trace_id as an exemplar. Click a point → Query inspector → Exemplars tab → copy the trace_id. Uses the bucket query (not histogram_quantile) for exemplar access: sum by (le, operation) (rate(tenantchat_turn_latency_seconds_bucket[$__rate_interval])).",
        ),
        ts_panel(
            2,
            "Turn Latency Buckets (for exemplar access)",
            0,
            8,
            24,
            6,
            [
                {
                    "expr": f"sum by (le, operation) (rate(tenantchat_turn_latency_seconds_bucket[{RATE}]))",
                    "legendFormat": "{{operation}} le={{le}}",
                    "refId": "A",
                }
            ],
            unit="short",
            desc="View exemplars: click any point, press Shift+drag to zoom, inspect the Prometheus query result, or open Query inspector → Exemplars. The trace_id links to Tempo when the Tempo datasource is configured.",
        ),
        ts_panel(
            3,
            "LLM Latency Buckets (for exemplar access)",
            0,
            14,
            24,
            6,
            [
                {
                    "expr": f"sum by (le, template) (rate(tenantchat_llm_latency_seconds_bucket[{RATE}]))",
                    "legendFormat": "{{template}} le={{le}}",
                    "refId": "A",
                }
            ],
            unit="short",
            desc="LLM call latency buckets with trace_id exemplars. A spike with an exemplar tags the exact turn that was slow.",
        ),
        ts_panel(
            4,
            "Retrieval Latency Buckets (for exemplar access)",
            0,
            20,
            24,
            6,
            [
                {
                    "expr": f"sum by (le, status) (rate(tenantchat_retrieval_latency_seconds_bucket[{RATE}]))",
                    "legendFormat": "{{status}} le={{le}}",
                    "refId": "A",
                }
            ],
            unit="short",
            desc="Retrieval latency buckets with trace_id exemplars. Correlate a slow retrieval with the turn's outcome.",
        ),
        {
            "id": 5,
            "title": "Drill-Through Workflow",
            "type": "text",
            "gridPos": {"x": 0, "y": 26, "w": 24, "h": 10},
            "options": {
                "content": """
# Exemplar → Trace ID → Explorer Drill-Through

This dashboard ties the operational plane (Grafana/Prometheus) to the inference plane (turn records in Postgres).

## Step 1: Find the exemplar
- Hover over any histogram panel above and press **Shift+drag** to select a time range
- Open **Query inspector → Exemplars** to see trace IDs attached to the latest samples

## Step 2: Open the trace in Tempo
- Copy the `trace_id` value from the exemplar
- Open **Tempo** (`http://192.168.1.170:3200` or your configured Tempo LB)
- Paste the trace ID and inspect the full span waterfall

## Step 3: Inspect the turn content in the admin explorer
- Take the same `trace_id`
- In the **admin explorer** (FEAT-015), query by trace ID:
  ```
  $ADMIN/api/admin/traces/by-trace-id/$TRACE_ID?tenant_id=<tenant>&reason=incident_investigation
  ```
- This returns the full turn record: prompt, retrieved evidence, model output, validator verdicts, diagnosis causes

## Which UI answers which question
| Question | Tool |
|---|---|
| Rates and classes over time | **Grafana** — dashboards in this file |
| One request's shape and timing | **Tempo/Phoenix** — span waterfall |
| One turn's content and reasoning | **Admin Explorer (FEAT-015)** — turn record |
| Evaluation datasets and experiments | **MLflow** — experiment tracking |

Grafana shows **trends and aggregates**. Tempo/Phoenix shows **one request's structure**.
The explorer shows **one turn's content** — the prompt, evidence, and reasoning the
operational plane deliberately excludes (ADR-0010).
                """.strip(),
                "mode": "markdown",
            },
        },
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 5 — SAFETY & GOVERNANCE
# ═══════════════════════════════════════════════════════════════════════════════
SAFETY = dash(
    title="Safety & Governance",
    uid="tenantchat-safety-governance",
    description="Policy blocks, model fallbacks, budget alerts, response cache, feedback, and dependency resilience — the guardrail and governance surface.",
    tags=["tenantchat", "safety", "governance", "ai-002"],
    panels=[
        ts_panel(
            1,
            "Policy Blocks — by Reason",
            0,
            0,
            16,
            8,
            [
                {
                    "expr": f"sum by (reason) (rate(tenantchat_policy_blocks_total[{RATE}]))",
                    "legendFormat": "{{reason}}",
                    "refId": "A",
                }
            ],
            desc="AI-002 safety surface: input_too_long, input_binary, output_too_long, budget_exhausted, action_limit, concurrency_limit. A spike in budget_exhausted means a tenant hit their spend cap.",
        ),
        stat_panel(
            2,
            "Budget Exhausted Rate",
            16,
            0,
            4,
            4,
            f'sum(rate(tenantchat_policy_blocks_total{{reason="budget_exhausted"}}[{RATE}]))',
            unit="reqps",
            decimals=3,
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 0.001},
                {"color": "red", "value": 0.01},
            ],
        ),
        stat_panel(
            3,
            "Content Blocks Rate",
            16,
            4,
            4,
            4,
            f'sum(rate(tenantchat_policy_blocks_total{{reason=~"input_too_long|input_binary|output_too_long"}}[{RATE}]))',
            unit="reqps",
            decimals=3,
            thresholds=[{"color": "green"}, {"color": "orange", "value": 0.01}],
        ),
        ts_panel(
            4,
            "Model Fallbacks — by Reason",
            0,
            8,
            12,
            7,
            [
                {
                    "expr": f"sum by (reason) (rate(tenantchat_model_fallbacks_total[{RATE}]))",
                    "legendFormat": "{{reason}}",
                    "refId": "A",
                }
            ],
        ),
        ts_panel(
            5,
            "Budget Alerts",
            12,
            8,
            12,
            7,
            [
                {
                    "expr": f"sum by (level) (rate(tenantchat_budget_alerts_total[{RATE}]))",
                    "legendFormat": "{{level}}",
                    "refId": "A",
                }
            ],
            desc="One-shot alerts per tenant per window: warn at first threshold, critical at hard limit. No repeating emissions.",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "warn"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "critical"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}}
                    ],
                },
            ],
        ),
        ts_panel(
            6,
            "Feedback — by Rating",
            0,
            15,
            8,
            6,
            [
                {
                    "expr": f"sum by (rating) (rate(tenantchat_feedback_submitted_total[{RATE}]))",
                    "legendFormat": "{{rating}}",
                    "refId": "A",
                }
            ],
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "up"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "down"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}}
                    ],
                },
            ],
        ),
        ts_panel(
            7,
            "Response Cache — by Result",
            8,
            15,
            8,
            6,
            [
                {
                    "expr": f"sum by (result) (rate(tenantchat_response_cache_total[{RATE}]))",
                    "legendFormat": "{{result}}",
                    "refId": "A",
                }
            ],
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "hit"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "miss"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "blue", "mode": "fixed"}}
                    ],
                },
            ],
        ),
        ts_panel(
            8,
            "Dependency Retries — by Dependency",
            16,
            15,
            8,
            6,
            [
                {
                    "expr": f"sum by (dependency) (rate(tenantchat_dependency_retries_total[{RATE}]))",
                    "legendFormat": "{{dependency}}",
                    "refId": "A",
                }
            ],
        ),
        ts_panel(
            9,
            "Business Actions — Booking Funnel",
            0,
            21,
            12,
            8,
            [
                {
                    "expr": f'sum by (status) (rate(tenantchat_business_actions_total{{operation="booking"}}[{RATE}]))',
                    "legendFormat": "{{status}}",
                    "refId": "A",
                },
            ],
            desc="Exactly-once business counts: committed (first time), replayed (duplicate), refused (policy/validation), declined (customer said no).",
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "committed"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "declined"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "orange", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "refused"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}}
                    ],
                },
            ],
        ),
        ts_panel(
            10,
            "Tool Call Failures — by Tool",
            12,
            21,
            12,
            8,
            [
                {
                    "expr": f'sum by (tool) (rate(tenantchat_tool_calls_total{{outcome="failed"}}[{RATE}]))',
                    "legendFormat": "{{tool}}",
                    "refId": "A",
                }
            ],
            desc="Tool failures per tool. Tools include booking commit, lead capture, handoff. An unknown tool (model hallucination) labels unknown.",
        ),
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# WRITE
# ═══════════════════════════════════════════════════════════════════════════════

DASHBOARDS = {
    "turn-outcomes.json": TURN_OUTCOMES,
    "retrieval-routing.json": RETRIEVAL_ROUTING,
    "llm-operations.json": LLM_OPS,
    "exemplar-drillthrough.json": EXEMPLAR,
    "safety-governance.json": SAFETY,
}


def main() -> None:
    for filename, data in DASHBOARDS.items():
        path = OUT_DIR / filename
        path.write_text(json.dumps(data, indent=2) + "\n")
        print(f"Wrote {path}")


if __name__ == "__main__":
    main()
