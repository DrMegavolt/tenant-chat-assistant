"""Generate or verify Grafana dashboard JSON from a compact description.

This script is the canonical source of the Grafana dashboards. It writes
complete dashboard JSON files into k8s/grafana/ so the dashboards can be
provisioned as ConfigMaps. ``--check`` compares the committed files with the
canonical render without modifying the worktree.

Every panel resolves against a metric that exists in the inventory
(tenantchat.core.metrics.MetricName) except where explicitly marked as an
optional observability dependency.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

OUT_DIR = Path(__file__).resolve().parent


def dash(
    title: str,
    uid: str,
    description: str,
    tags: Sequence[str],
    panels: Sequence[dict[str, Any]],
    *,
    variables: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
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
                },
                *variables,
            ]
        },
        "panels": panels,
    }


def ts_panel(
    id_: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: list[dict[str, Any]],
    *,
    unit: str = "reqps",
    overrides: Sequence[dict[str, Any]] | None = None,
    draw: str = "line",
    fill: int = 10,
    stack: bool = False,
    desc: str = "",
    links: Sequence[dict[str, Any]] | None = None,
    thresholds: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fc: dict[str, Any] = {
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
    if thresholds:
        fc["defaults"]["thresholds"] = {"mode": "absolute", "steps": list(thresholds)}
    if overrides:
        fc["overrides"] = overrides
    if links:
        fc["defaults"]["links"] = list(links)
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
    x: int,
    y: int,
    w: int,
    h: int,
    expr: str,
    *,
    unit: str = "percent",
    decimals: int = 1,
    thresholds: Sequence[dict[str, Any]] | None = None,
    color_mode: str = "background",
    graph_mode: str = "area",
    desc: str = "",
) -> dict[str, Any]:
    if thresholds is None:
        thresholds = [
            {"color": "green", "value": None},
        ]
    p: dict = {
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
    if desc:
        p["description"] = desc
    return p


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


def zero_line(expr: str, label: str) -> str:
    """Healthy-zero fallback (L-O05): a counter with no series yet renders as a
    0 line under the synthetic ``none`` class instead of "No data"."""
    return f'{expr} or label_replace(vector(0), "{label}", "none", "", "")'


# ═══════════════════════════════════════════════════════════════════════════════
# LAB DASHBOARD HELPERS (Datadog-view layer — GD-60)
#
# Panels below resolve against Kubernetes/infra metrics (node-exporter,
# kube-state-metrics, cAdvisor, traefik, blackbox, postgres/elasticsearch
# exporters, and the lab:* recording rules from k8s/lab-prometheusrules.yaml).
# None of these are tenantchat.core.metrics inventory metrics: they follow the
# established optional observability dependency convention instead.
# ═══════════════════════════════════════════════════════════════════════════════

UPDOWN_MAPPINGS = [
    {
        "type": "value",
        "options": {
            "0": {"text": "DOWN", "color": "red", "index": 0},
            "1": {"text": "UP", "color": "green", "index": 1},
        },
    }
]

# "No data" must read as grey (no signal), never as the base threshold colour
# (red for most tiles) — grey = not applicable, red = actually degraded.
NO_DATA_GREY = [
    {
        "type": "special",
        "options": {"match": "null", "result": {"color": "text", "index": 0}},
    }
]


def template_var(name: str, query: str, *, multi: bool = False) -> dict[str, Any]:
    """Query variable over the Prometheus datasource (uid pinned by the
    kube-prom-stack sidecar; verified in the runbook)."""
    return {
        "name": name,
        "type": "query",
        "datasource": {"type": "prometheus", "uid": "prometheus"},
        "query": {"query": query, "refId": f"{name}-Variable-Query"},
        "definition": query,
        "label": name,
        "hide": 0,
        "multi": multi,
        "includeAll": True,
        "allValue": ".*",
        "refresh": 1,
        "sort": 1,
        "current": {},
    }


def row_panel(id_: int, title: str, y: int, *, repeat: str | None = None) -> dict[str, Any]:
    p: dict[str, Any] = {
        "id": id_,
        "title": title,
        "type": "row",
        "collapsed": False,
        "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
        "panels": [],
    }
    if repeat:
        p["repeat"] = repeat
    return p


def updown_history(
    id_: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    expr: str,
    *,
    legend: str = "{{job}}",
    desc: str = "",
) -> dict[str, Any]:
    """Availability strip: one coloured band per target over the time range —
    green = up, red = down, gap = gone. Chosen over repeating stat tiles
    because it stays readable at 30+ targets and shows *when* something was
    down, not just that it is."""
    p: dict[str, Any] = {
        "id": id_,
        "title": title,
        "type": "status-history",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": [{"expr": expr, "legendFormat": legend, "refId": "A"}],
        "fieldConfig": {
            "defaults": {
                "unit": "none",
                "decimals": 0,
                "color": {"mode": "thresholds"},
                "mappings": UPDOWN_MAPPINGS,
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}],
                },
            },
            "overrides": [],
        },
        "options": {
            "showValue": "never",
            "mergeValues": True,
            "legend": {"displayMode": "list", "placement": "right", "showLegend": False},
            "tooltip": {"mode": "single", "sort": "none"},
        },
    }
    if desc:
        p["description"] = desc
    return p


def updown_tile(
    id_: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    expr: str,
    *,
    legend: str = "{{job}}",
    desc: str = "",
    repeat: str | None = None,
) -> dict[str, Any]:
    """Single-target UP/DOWN stat tile (catalog tiles): instant query so the
    tile shows the current state, not the last sample of a dead series."""
    p = stat_panel(
        id_,
        title,
        x,
        y,
        w,
        h,
        expr,
        unit="none",
        decimals=0,
        thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
        color_mode="background",
        graph_mode="none",
    )
    p["fieldConfig"]["defaults"]["mappings"] = UPDOWN_MAPPINGS + NO_DATA_GREY
    p["targets"][0]["instant"] = True
    p["targets"][0]["legendFormat"] = legend
    if repeat:
        p["repeat"] = repeat
        p["repeatDirection"] = "h"
    if desc:
        p["description"] = desc
    return p


def service_stat(
    id_: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    expr: str,
    *,
    unit: str = "none",
    decimals: int = 1,
    thresholds: Sequence[dict[str, Any]] | None = None,
    desc: str = "",
    legend: str | None = None,
    repeat: str | None = None,
) -> dict[str, Any]:
    """One catalog tile: a stat scoped to the repeated $namespace/$service.
    The legend names the value — without it Grafana renders the raw label set
    of the series as the tile caption. Repeat is per panel (Grafana ignores
    row.repeat on provisioned open rows) and repeats horizontally so the
    catalog reads as a Datadog-style tile grid."""
    if thresholds is None:
        thresholds = [{"color": "green", "value": None}]
    p = stat_panel(
        id_, title, x, y, w, h, expr, unit=unit, decimals=decimals, thresholds=thresholds
    )
    p["fieldConfig"]["defaults"]["mappings"] = NO_DATA_GREY
    if legend is not None:
        p["targets"][0]["legendFormat"] = legend
    if repeat:
        p["repeat"] = repeat
        p["repeatDirection"] = "h"
    if desc:
        p["description"] = desc
    return p


def table_panel(
    id_: int,
    title: str,
    x: int,
    y: int,
    w: int,
    h: int,
    targets: list[dict[str, Any]],
    *,
    unit: str = "none",
    desc: str = "",
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "id": id_,
        "title": title,
        "type": "table",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": {"unit": unit, "custom": {"align": "auto"}}, "overrides": []},
        "options": {"showHeader": True, "cellHeight": "sm"},
    }
    if desc:
        p["description"] = desc
    return p


def text_panel(
    id_: int, title: str, x: int, y: int, w: int, h: int, content: str
) -> dict[str, Any]:
    return {
        "id": id_,
        "title": title,
        "type": "text",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "options": {"content": content, "mode": "markdown"},
    }


def instant(expr: str, legend: str, ref_id: str) -> dict[str, Any]:
    """Instant-vector query for tables (current state, not a time range)."""
    return {
        "expr": expr,
        "legendFormat": legend,
        "refId": ref_id,
        "instant": True,
        "format": "table",
    }


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
            f'sum(rate(tenantchat_turn_outcomes_total{{outcome="answered"}}[{RATE}])) / sum(rate(tenantchat_turn_outcomes_total[{RATE}])) * 100 or vector(0)',
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
            f'sum(rate(tenantchat_turn_outcomes_total{{outcome="abstained"}}[{RATE}])) / sum(rate(tenantchat_turn_outcomes_total[{RATE}])) * 100 or vector(0)',
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
            f'sum(rate(tenantchat_turn_outcomes_total{{outcome="clarified"}}[{RATE}])) / sum(rate(tenantchat_turn_outcomes_total[{RATE}])) * 100 or vector(0)',
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
            f'sum(rate(tenantchat_turn_outcomes_total{{outcome=~"answer_refused|turn_failed"}}[{RATE}])) / sum(rate(tenantchat_turn_outcomes_total[{RATE}])) * 100 or vector(0)',
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
            f'sum(rate(tenantchat_routing_decisions_total{{outcome="clarify"}}[{RATE}])) / sum(rate(tenantchat_routing_decisions_total[{RATE}])) * 100 or vector(0)',
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
            unit="none",
            desc="Router confidence is a raw routing score (direct threshold 4.0, clarify 2.5), not a 0-1 ratio — rendered as a plain number, not a percentage.",
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
            f'sum(rate(tenantchat_citation_validation_total{{verdict="invalid"}}[{RATE}])) / sum(rate(tenantchat_citation_validation_total[{RATE}])) * 100 or vector(0)',
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
            f'sum(rate(tenantchat_llm_calls_total{{status="error"}}[{RATE}])) / sum(rate(tenantchat_llm_calls_total[{RATE}])) * 100 or vector(0)',
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
            f'sum(rate(tenantchat_llm_calls_total{{status="timeout"}}[{RATE}])) / sum(rate(tenantchat_llm_calls_total[{RATE}])) * 100 or vector(0)',
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
            f"sum(rate(tenantchat_model_fallbacks_total[{RATE}])) / sum(rate(tenantchat_llm_calls_total[{RATE}])) * 100 or vector(0)",
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
            f'sum(rate(tenantchat_response_cache_total{{result="hit"}}[{RATE}])) / sum(rate(tenantchat_response_cache_total[{RATE}])) * 100 or vector(0)',
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
- Open **Tempo** (`http://192.168.1.177:3200` on the current local cluster, or your configured Tempo LB)
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
                    "expr": zero_line(
                        f"sum by (reason) (rate(tenantchat_policy_blocks_total[{RATE}]))", "reason"
                    ),
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
            f'sum(rate(tenantchat_policy_blocks_total{{reason="budget_exhausted"}}[{RATE}])) or vector(0)',
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
            f'sum(rate(tenantchat_policy_blocks_total{{reason=~"input_too_long|input_binary|output_too_long"}}[{RATE}])) or vector(0)',
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
                    "expr": zero_line(
                        f"sum by (reason) (rate(tenantchat_model_fallbacks_total[{RATE}]))",
                        "reason",
                    ),
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
                    "expr": zero_line(
                        f"sum by (level) (rate(tenantchat_budget_alerts_total[{RATE}]))", "level"
                    ),
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
                    "expr": zero_line(
                        f"sum by (rating) (rate(tenantchat_feedback_submitted_total[{RATE}]))",
                        "rating",
                    ),
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
                    "expr": zero_line(
                        f"sum by (result) (rate(tenantchat_response_cache_total[{RATE}]))", "result"
                    ),
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
                    "expr": zero_line(
                        f"sum by (dependency) (rate(tenantchat_dependency_retries_total[{RATE}]))",
                        "dependency",
                    ),
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
                    "expr": zero_line(
                        f'sum by (status) (rate(tenantchat_business_actions_total{{operation="booking"}}[{RATE}]))',
                        "status",
                    ),
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
                    "expr": zero_line(
                        f'sum by (tool) (rate(tenantchat_tool_calls_total{{outcome="failed"}}[{RATE}]))',
                        "tool",
                    ),
                    "legendFormat": "{{tool}}",
                    "refId": "A",
                }
            ],
            desc="Tool failures per tool. Tools include booking commit, lead capture, handoff. An unknown tool (model hallucination) labels unknown.",
        ),
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 6 — LAB · INFRASTRUCTURE (the Host Map / Infrastructure page)
# ═══════════════════════════════════════════════════════════════════════════════

NODE_CPU = '100 * (1 - avg(rate(node_cpu_seconds_total{mode="idle"}[5m])))'
NODE_MEM = "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)"
NODE_DISK = (
    '100 * (1 - node_filesystem_avail_bytes{mountpoint="/", fstype!~"tmpfs|overlay"}'
    ' / node_filesystem_size_bytes{mountpoint="/", fstype!~"tmpfs|overlay"})'
)

INFRA = dash(
    title="Lab · Infrastructure",
    uid="lab-infra-overview",
    description=(
        "The Datadog 'Infrastructure' page for the whole lab: is anything down, "
        "is the box healthy, which namespace is eating it, what is restarting. "
        "Built on node-exporter, kube-state-metrics and cAdvisor (optional "
        "observability dependencies, not inventory metrics)."
    ),
    tags=["lab", "infra", "nodes", "kubernetes"],
    panels=[
        row_panel(100, "Availability — is anything down?", 0),
        updown_history(
            1,
            "Scraped targets",
            0,
            1,
            24,
            11,
            'up{job!~"lab-http(-redirect)?-probes"}',
            legend="{{job}}",
            desc="One band per Prometheus scrape target across every namespace. Red = failing scrape; a band that ends = target removed.",
        ),
        updown_history(
            2,
            "HTTP probes (blackbox)",
            0,
            12,
            24,
            7,
            'probe_success{job=~"lab-http(-redirect)?-probes"}',
            legend="{{instance}}",
            desc="Synthetic availability per HTTP surface (GD-02): web, keycloak health, grafana, tempo, loki, phoenix, mlflow, kibana, kafka-ui, registry, argocd, canals.",
        ),
        row_panel(101, "Node — USE method (GD-10)", 19),
        stat_panel(
            3,
            "CPU used",
            0,
            20,
            6,
            4,
            NODE_CPU,
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 70.0},
                {"color": "red", "value": 90.0},
            ],
        ),
        stat_panel(
            4,
            "Memory used",
            6,
            20,
            6,
            4,
            NODE_MEM,
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 70.0},
                {"color": "red", "value": 90.0},
            ],
        ),
        stat_panel(
            5,
            "Disk used (/)",
            12,
            20,
            6,
            4,
            NODE_DISK,
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 70.0},
                {"color": "red", "value": 85.0},
            ],
        ),
        stat_panel(
            6,
            "Load (1m)",
            18,
            20,
            6,
            4,
            "node_load1",
            unit="none",
            decimals=2,
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 4.0},
                {"color": "red", "value": 8.0},
            ],
        ),
        ts_panel(
            7,
            "Node CPU & memory",
            0,
            24,
            12,
            8,
            [
                {"expr": NODE_CPU, "legendFormat": "cpu %", "refId": "A"},
                {"expr": NODE_MEM, "legendFormat": "memory %", "refId": "B"},
            ],
            unit="percent",
        ),
        ts_panel(
            8,
            "Network throughput",
            12,
            24,
            12,
            8,
            [
                {
                    "expr": 'sum by (device) (rate(node_network_receive_bytes_total{device!~"lo|veth.*|docker.*|cali.*"}[5m]))',
                    "legendFormat": "rx {{device}}",
                    "refId": "A",
                },
                {
                    "expr": 'sum by (device) (rate(node_network_transmit_bytes_total{device!~"lo|veth.*|docker.*|cali.*"}[5m]))',
                    "legendFormat": "tx {{device}}",
                    "refId": "B",
                },
            ],
            unit="Bps",
        ),
        ts_panel(
            9,
            "Disk I/O",
            0,
            32,
            12,
            8,
            [
                {
                    "expr": 'rate(node_disk_read_bytes_total{device!~"loop.*|ram.*"}[5m])',
                    "legendFormat": "read {{device}}",
                    "refId": "A",
                },
                {
                    "expr": 'rate(node_disk_written_bytes_total{device!~"loop.*|ram.*"}[5m])',
                    "legendFormat": "written {{device}}",
                    "refId": "B",
                },
            ],
            unit="Bps",
        ),
        ts_panel(
            10,
            "Load average",
            12,
            32,
            12,
            8,
            [
                {"expr": "node_load1", "legendFormat": "load1", "refId": "A"},
                {"expr": "node_load5", "legendFormat": "load5", "refId": "B"},
            ],
            unit="none",
        ),
        row_panel(102, "Kubernetes fleet (GD-11)", 40),
        ts_panel(
            11,
            "Top pods by CPU",
            0,
            41,
            12,
            8,
            [
                {
                    "expr": 'topk(10, sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!="", image!=""}[5m])))',
                    "legendFormat": "{{namespace}}/{{pod}}",
                    "refId": "A",
                }
            ],
            unit="none",
            desc="cAdvisor CPU cores per pod, top 10. Optional observability dependency.",
        ),
        ts_panel(
            12,
            "Top pods by memory (working set)",
            12,
            41,
            12,
            8,
            [
                {
                    "expr": 'topk(10, sum by (namespace, pod) (container_memory_working_set_bytes{container!="", image!=""}))',
                    "legendFormat": "{{namespace}}/{{pod}}",
                    "refId": "A",
                }
            ],
            unit="bytes",
        ),
        table_panel(
            13,
            "Restarts (24h) — top offenders",
            0,
            49,
            12,
            8,
            [
                instant(
                    "topk(10, sum by (namespace, pod) (increase(kube_pod_container_status_restarts_total[24h])) )",
                    "{{namespace}}/{{pod}}",
                    "A",
                )
            ],
            desc="Restart-bombing candidates; >3 in 30m fires the LabContainerRestartStorm monitor.",
        ),
        table_panel(
            14,
            "Waiting containers (by reason)",
            12,
            49,
            12,
            8,
            [
                instant(
                    'kube_pod_container_status_waiting_reason{reason!=""}',
                    "{{namespace}}/{{pod}} — {{reason}}",
                    "A",
                )
            ],
            desc="Empty when every container is either running or terminally failed.",
        ),
        stat_panel(
            15,
            "OOM kills (24h)",
            0,
            57,
            6,
            8,
            'sum(increase(kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}[24h])) or vector(0)',
            unit="none",
            decimals=0,
            thresholds=[{"color": "green"}, {"color": "red", "value": 1}],
        ),
        table_panel(
            16,
            "Deployments below desired replicas",
            6,
            57,
            18,
            8,
            [
                instant(
                    "kube_deployment_status_replicas_available < kube_deployment_spec_replicas",
                    "{{namespace}}/{{deployment}}",
                    "A",
                )
            ],
            desc="Only deployments currently missing replicas appear. Empty table = every deployment at spec.",
        ),
        table_panel(
            17,
            "PVC usage",
            0,
            65,
            12,
            6,
            [
                instant(
                    "kubelet_volume_stats_available_bytes / kubelet_volume_stats_capacity_bytes",
                    "{{namespace}}/{{persistentvolumeclaim}}",
                    "A",
                )
            ],
            unit="percentunit",
            desc=(
                "Fills only for storage classes that report volume stats. The "
                "microk8s hostpath provisioner does not, so this stays empty in "
                "this lab — the LabPVCNearlyFull monitor shares the same blind "
                "spot (documented, not a data bug)."
            ),
        ),
        text_panel(
            18,
            "Reading this page",
            12,
            65,
            12,
            6,
            (
                "# From a cold open, in order\n\n"
                "1. **Availability** — any red tile means something is down right now "
                "(scrape target or HTTP probe).\n"
                "2. **Node** — CPU / memory / disk / load on the single lab node "
                "(USE method).\n"
                "3. **Kubernetes fleet** — which namespace is eating the box, what is "
                "restarting, which deployments are below spec.\n\n"
                "Next step for a degraded service: **Lab · Services** → tile → "
                "**Lab · Service Drilldown**."
            ),
        ),
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 7 — LAB · SERVICES (the APM Service Catalog page)
# ═══════════════════════════════════════════════════════════════════════════════

# GD-22 audit (verified live 2026-08-29): the only native per-service HTTP
# metric names in this cluster are the OTel Python old-semconv set
# http_server_duration_milliseconds_* (no status-code label — errors cannot be
# derived from it) reached via the gateway collector with the source workload
# in exported_job="namespace/service", and Traefik's traefik_service_requests_total
# / traefik_service_request_duration_seconds_bucket (has code). The lab:*
# recording rules in k8s/lab-prometheusrules.yaml normalise both into
# (namespace, service); the catalog tiles standardise on those so drift tests
# keep the names honest. Metric-less services fall back to blackbox probes.

CATALOG_RPS = (
    'lab:service:http:rps:rate5m{namespace=~"$namespace", service=~"$service"}'
    ' or lab:gateway:service:rps:rate5m{namespace=~"$namespace", service=~"$service"}'
)
CATALOG_P95 = (
    'lab:service:http:p95:rate5m{namespace=~"$namespace", service=~"$service"}'
    ' or lab:gateway:service:p95:rate5m{namespace=~"$namespace", service=~"$service"}'
)
CATALOG_ERR = 'lab:gateway:service:error_ratio:rate5m{namespace=~"$namespace", service=~"$service"}'

SERVICES = dash(
    title="Lab · Services",
    uid="lab-services",
    description=(
        "The Datadog 'Service Catalog': one repeating tile block per deployment — "
        "status, requests/s, p95, error ratio, restarts, CPU, memory, probe — with "
        "a Monitors row at the bottom (GD-21/23/51). An outliers band above the "
        "catalog draws one line per service for traffic, p95, errors and restarts, "
        "so the odd one out is visible in a single panel. Tiles key off the lab:* "
        "recording rules, so a new deployment appears automatically."
    ),
    tags=["lab", "services", "catalog", "apm"],
    variables=[
        template_var("namespace", "label_values(lab:deployment:restarts:increase1h, namespace)"),
        template_var(
            "service",
            'label_values(lab:deployment:restarts:increase1h{namespace=~"$namespace"}, workload)',
        ),
    ],
    panels=[
        row_panel(100, "At a glance", 0),
        stat_panel(
            1,
            "Deployments below spec",
            0,
            1,
            6,
            4,
            'count(kube_deployment_status_replicas_available{namespace=~"$namespace"} < kube_deployment_spec_replicas{namespace=~"$namespace"}) or vector(0)',
            unit="none",
            decimals=0,
            thresholds=[{"color": "green"}, {"color": "red", "value": 1}],
        ),
        stat_panel(
            2,
            "Monitors firing",
            6,
            1,
            6,
            4,
            'count(ALERTS{alertname=~"Lab.*", alertstate="firing"}) or vector(0)',
            unit="none",
            decimals=0,
            thresholds=[{"color": "green"}, {"color": "red", "value": 1}],
        ),
        stat_panel(
            3,
            "Probes failing",
            12,
            1,
            6,
            4,
            'count(probe_success{job=~"lab-http(-redirect)?-probes"} == 0) or vector(0)',
            unit="none",
            decimals=0,
            thresholds=[{"color": "green"}, {"color": "red", "value": 1}],
        ),
        stat_panel(
            4,
            "Services in view",
            18,
            1,
            6,
            4,
            'count(lab:deployment:cpu:rate5m{namespace=~"$namespace"})',
            unit="none",
            decimals=0,
        ),
        table_panel(
            5,
            "Monitors (lab-monitors PrometheusRule — GD-51 parity)",
            0,
            5,
            24,
            7,
            [instant('ALERTS{alertname=~"Lab.*"}', "{{alertname}} — {{alertstate}}", "A")],
            desc="Every Lab* monitor with its current state. An empty table means all monitors are OK; alerts route to the in-cluster Alertmanager.",
        ),
        # --- Outliers band: one line per service, every service in one panel ---
        # (the tiles below answer "what is each service's value right now"; these
        # charts answer "which service is the outlier over the window"). Same
        # CATALOG_* expressions as the tiles, so the `or` fallback keeps exactly
        # one line per service — the gateway series only exists where the service
        # has no native OTel one.
        row_panel(102, "Outliers at a glance — one line per service", 12),
        ts_panel(
            120,
            "Requests/s — all services",
            0,
            13,
            12,
            9,
            [{"expr": CATALOG_RPS, "legendFormat": "{{namespace}}/{{service}}", "refId": "A"}],
            unit="reqps",
            desc="Native OTel line where the service is instrumented, Traefik edge line otherwise; the `or` dedupes so each service draws once.",
        ),
        ts_panel(
            121,
            "p95 latency — all services",
            12,
            13,
            12,
            9,
            [{"expr": CATALOG_P95, "legendFormat": "{{namespace}}/{{service}}", "refId": "A"}],
            unit="s",
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 0.5},
                {"color": "red", "value": 2.0},
            ],
            desc="Same 0.5 s / 2 s guide lines as the p95 tiles below — the outlier service and the offending spike show up without opening the drilldown.",
        ),
        ts_panel(
            122,
            "Error ratio (5xx) — all services",
            0,
            22,
            12,
            9,
            [{"expr": CATALOG_ERR, "legendFormat": "{{namespace}}/{{service}}", "refId": "A"}],
            unit="percentunit",
            thresholds=[{"color": "green"}, {"color": "red", "value": 0.05}],
            desc="5xx share at the Traefik edge, one line per service; the 5 % monitor threshold is drawn.",
        ),
        ts_panel(
            123,
            "Restarts (1h) — all deployments",
            12,
            22,
            12,
            9,
            [
                {
                    "expr": 'lab:deployment:restarts:increase1h{namespace=~"$namespace", workload=~"$service"}',
                    "legendFormat": "{{namespace}}/{{workload}}",
                    "refId": "A",
                }
            ],
            unit="none",
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 1},
                {"color": "red", "value": 4},
            ],
            desc="Restart spikes per deployment; same 1 / 4 thresholds as the tiles below.",
        ),
        row_panel(101, "Service catalog — one tile block per service", 31),
        # --- GD-21: repeated tiles. Each metric is one repeating panel so the
        # grid reads: status for everyone, then traffic for everyone, ... ---
        service_stat(
            110,
            "Status — $service",
            0,
            32,
            6,
            4,
            'kube_deployment_status_replicas_available{namespace=~"$namespace", deployment=~"$service"}'
            ' / kube_deployment_spec_replicas{namespace=~"$namespace", deployment=~"$service"}',
            unit="percentunit",
            decimals=0,
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            legend="available / spec",
            repeat="service",
            desc="Available / desired replicas. Grey 'No data' = not a Deployment (StatefulSet, DaemonSet, Job).",
        ),
        service_stat(
            111,
            "Requests/s — $service",
            0,
            37,
            6,
            4,
            CATALOG_RPS,
            unit="reqps",
            decimals=2,
            thresholds=[{"color": "blue", "value": None}],
            legend="req/s",
            repeat="service",
            desc="Native OTel HTTP when the service is instrumented, Traefik gateway counts otherwise (GD-22).",
        ),
        service_stat(
            112,
            "p95 latency — $service",
            0,
            42,
            6,
            4,
            CATALOG_P95,
            unit="s",
            decimals=2,
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 0.5},
                {"color": "red", "value": 2.0},
            ],
            legend="p95",
            repeat="service",
        ),
        service_stat(
            113,
            "Error ratio (5xx) — $service",
            0,
            47,
            6,
            4,
            CATALOG_ERR,
            unit="percentunit",
            decimals=2,
            thresholds=[{"color": "green"}, {"color": "red", "value": 0.05}],
            legend="5xx ratio",
            repeat="service",
            desc="5xx share measured at the Traefik edge; 0 until the service has gateway traffic.",
        ),
        service_stat(
            114,
            "Restarts (1h) — $service",
            0,
            52,
            6,
            4,
            'lab:deployment:restarts:increase1h{namespace=~"$namespace", workload=~"$service"}',
            unit="none",
            decimals=0,
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 1},
                {"color": "red", "value": 4},
            ],
            legend="restarts",
            repeat="service",
        ),
        service_stat(
            115,
            "CPU (cores) — $service",
            0,
            57,
            6,
            4,
            'lab:deployment:cpu:rate5m{namespace=~"$namespace", workload=~"$service"}',
            unit="none",
            decimals=3,
            legend="cores",
            repeat="service",
        ),
        service_stat(
            116,
            "Memory (working set) — $service",
            0,
            62,
            6,
            4,
            'lab:deployment:memory:workingset{namespace=~"$namespace", workload=~"$service"}',
            unit="bytes",
            decimals=1,
            legend="working set",
            repeat="service",
        ),
        updown_tile(
            117,
            "HTTP probe — $service",
            0,
            67,
            6,
            4,
            'probe_success{job=~"lab-http(-redirect)?-probes", instance=~".*$service.*"}',
            legend="{{instance}}",
            repeat="service",
            desc="Blackbox probe whose target URL mentions the service. Grey 'No data' = no probe for this service (not internal HTTP).",
        ),
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 8 — LAB · SERVICE DRILLDOWN (the APM service page)
# ═══════════════════════════════════════════════════════════════════════════════

DRILLDOWN = dash(
    title="Lab · Service Drilldown",
    uid="lab-service-drilldown",
    description=(
        "One templated APM-service page for every service in the lab: RED, "
        "per-pod saturation, Kubernetes context, and cross-plane links into "
        "Tempo/Loki (GD-30..33). TenantChat services additionally link to their "
        "four app-specific dashboards."
    ),
    tags=["lab", "services", "drilldown", "apm"],
    variables=[
        template_var("namespace", "label_values(lab:deployment:restarts:increase1h, namespace)"),
        template_var(
            "service",
            'label_values(lab:deployment:restarts:increase1h{namespace=~"$namespace"}, workload)',
        ),
    ],
    panels=[
        row_panel(100, "RED — rate, errors, duration (GD-30)", 0),
        ts_panel(
            1,
            "Request rate",
            0,
            1,
            8,
            8,
            [
                {
                    "expr": 'lab:service:http:rps:rate5m{namespace=~"$namespace", service=~"$service"}',
                    "legendFormat": "native (OTel)",
                    "refId": "A",
                },
                {
                    "expr": 'lab:gateway:service:rps:rate5m{namespace=~"$namespace", service=~"$service"}',
                    "legendFormat": "gateway (Traefik)",
                    "refId": "B",
                },
            ],
            desc="Native OTel HTTP for the instrumented trio, Traefik edge counts for ingress-routed services. Only probes = the tile stays empty (synthetic-only service).",
        ),
        ts_panel(
            2,
            "Error ratio (5xx at the edge)",
            8,
            1,
            8,
            8,
            [
                {
                    "expr": 'lab:gateway:service:error_ratio:rate5m{namespace=~"$namespace", service=~"$service"}',
                    "legendFormat": "5xx share",
                    "refId": "A",
                }
            ],
            unit="percentunit",
            desc="The OTel histogram carries no status code (old semconv), so error ratios come from Traefik. Synthetic-only services show 'No data' here by design.",
        ),
        ts_panel(
            3,
            "Latency percentiles",
            16,
            1,
            8,
            8,
            [
                {
                    "expr": 'histogram_quantile(0.50, sum by (le) (rate(http_server_duration_milliseconds_bucket{exported_job=~"$namespace/$service"}[$__rate_interval])))',
                    "legendFormat": "p50 (OTel)",
                    "refId": "A",
                },
                {
                    "expr": 'histogram_quantile(0.95, sum by (le) (rate(http_server_duration_milliseconds_bucket{exported_job=~"$namespace/$service"}[$__rate_interval])))',
                    "legendFormat": "p95 (OTel)",
                    "refId": "B",
                },
                {
                    "expr": 'histogram_quantile(0.99, sum by (le) (rate(http_server_duration_milliseconds_bucket{exported_job=~"$namespace/$service"}[$__rate_interval])))',
                    "legendFormat": "p99 (OTel)",
                    "refId": "C",
                },
                {
                    "expr": 'histogram_quantile(0.95, sum by (le) (rate(traefik_service_request_duration_seconds_bucket{service=~".*$service-[0-9]+@kubernetes"}[$__rate_interval])))',
                    "legendFormat": "p95 (gateway)",
                    "refId": "D",
                },
            ],
            unit="s",
            links=[
                {
                    "title": "Search traces in Tempo (service=$service)",
                    "url": '/explore?left={"datasource":"tempo","queries":[{"refId":"A","queryType":"search","serviceName":"$service","limit":20}],"range":"now-1h/now"}',
                },
                {
                    "title": "Logs in Loki (pod)",
                    "url": '/explore?left={"datasource":"loki","queries":[{"refId":"A","expr":"{namespace=\\"$namespace\\", pod=~\\"$pod\\"}","queryType":"range"}],"range":"now-1h/now"}',
                },
            ],
            desc="p50/p95/p99 from the native OTel histogram; the gateway p95 line appears for non-instrumented services. Data links: Tempo trace search and Loki pod logs (GD-33).",
        ),
        row_panel(101, "Saturation — per pod (GD-31)", 9),
        ts_panel(
            4,
            "CPU per pod",
            0,
            10,
            8,
            8,
            [
                {
                    "expr": 'sum by (pod) (rate(container_cpu_usage_seconds_total{namespace="$namespace", pod=~"$service.*", container!="", image!=""}[$__rate_interval]))',
                    "legendFormat": "{{pod}}",
                    "refId": "A",
                }
            ],
            unit="none",
        ),
        ts_panel(
            5,
            "Memory (working set) per pod",
            8,
            10,
            8,
            8,
            [
                {
                    "expr": 'sum by (pod) (container_memory_working_set_bytes{namespace="$namespace", pod=~"$service.*", container!="", image!=""})',
                    "legendFormat": "{{pod}}",
                    "refId": "A",
                }
            ],
            unit="bytes",
        ),
        ts_panel(
            6,
            "Restarts per pod (1h)",
            16,
            10,
            8,
            8,
            [
                {
                    "expr": 'sum by (pod) (increase(kube_pod_container_status_restarts_total{namespace="$namespace", pod=~"$service.*"}[1h]))',
                    "legendFormat": "{{pod}}",
                    "refId": "A",
                }
            ],
            unit="none",
            desc="A climbing line is a restart loop; pair with the termination reasons table below.",
        ),
        row_panel(102, "Kubernetes context (GD-32)", 18),
        stat_panel(
            7,
            "Replicas (available / spec)",
            0,
            19,
            6,
            4,
            'kube_deployment_status_replicas_available{namespace=~"$namespace", deployment=~"$service"}',
            unit="none",
            decimals=0,
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
        ),
        stat_panel(
            8,
            "Pods running",
            6,
            19,
            6,
            4,
            'sum(kube_pod_status_phase{namespace="$namespace", pod=~"$service.*", phase="Running"}) or vector(0)',
            unit="none",
            decimals=0,
        ),
        stat_panel(
            9,
            "Oldest pod age",
            12,
            19,
            6,
            4,
            'time() - min(kube_pod_start_time{namespace="$namespace", pod=~"$service.*"})',
            unit="s",
            decimals=0,
            thresholds=[{"color": "blue", "value": None}],
            desc="The 'deployed 5h ago' line — age of the oldest pod of this service.",
        ),
        stat_panel(
            10,
            "Probe latency",
            18,
            19,
            6,
            4,
            'max(probe_duration_seconds{instance=~".*$service.*"})',
            unit="s",
            decimals=3,
            thresholds=[{"color": "blue", "value": None}],
            desc="Synthetic end-to-end latency from the blackbox probe (the fallback RED source for metric-less services).",
        ),
        table_panel(
            11,
            "Containers & images",
            0,
            23,
            12,
            7,
            [
                instant(
                    'kube_pod_container_info{namespace="$namespace", pod=~"$service.*"}',
                    "{{pod}} / {{container}} — {{image}}",
                    "A",
                )
            ],
        ),
        table_panel(
            12,
            "Last termination reasons",
            12,
            23,
            12,
            7,
            [
                instant(
                    'kube_pod_container_status_last_terminated_reason{namespace="$namespace", pod=~"$service.*"}',
                    "{{pod}} / {{container}} — {{reason}}",
                    "A",
                )
            ],
        ),
        text_panel(
            13,
            "Cross-plane links (GD-33)",
            0,
            30,
            24,
            5,
            (
                "# Where to next\n\n"
                "- **Traces**: latency panel data links → Tempo search scoped to `service.name=$service`.\n"
                '- **Logs**: latency panel data links → Loki `{namespace="$namespace", pod=~"$service.*"}`.\n'
                "- **TenantChat app dashboards** (keep the generic layer generic; tenant detail lives here):\n"
                "  - [Chat Turn Outcomes](/d/tenantchat-turn-outcomes?from=${__from}&to=${__to})\n"
                "  - [Retrieval & Routing Quality](/d/tenantchat-retrieval-routing?from=${__from}&to=${__to})\n"
                "  - [LLM Operations & Token Cost](/d/tenantchat-llm-operations?from=${__from}&to=${__to})\n"
                "  - [Safety & Governance](/d/tenantchat-safety-governance?from=${__from}&to=${__to})\n"
            ),
        ),
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 9 — LAB · DATASTORES (the Integrations page)
# ═══════════════════════════════════════════════════════════════════════════════

DATASTORES = dash(
    title="Lab · Datastores",
    uid="lab-datastores",
    description=(
        "Postgres, Elasticsearch and Kafka sections — the Datadog 'Integrations' "
        "page. Postgres/ES metrics come from the community exporters deployed by "
        "k8s/lab-observability.yaml; Kafka from the Strimzi kafka exporter "
        "(optional observability dependencies)."
    ),
    tags=["lab", "datastores", "postgres", "elasticsearch", "kafka"],
    panels=[
        row_panel(100, "Postgres — llm-chat application database (GD-40)", 0),
        stat_panel(
            1,
            "Connections / max",
            0,
            1,
            6,
            4,
            "sum(pg_stat_activity_count) / max(pg_settings_max_connections)",
            unit="percentunit",
            decimals=1,
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 0.6},
                {"color": "red", "value": 0.8},
            ],
            desc="Total connections across all databases against max_connections (>80% fires LabPostgresConnectionsHigh).",
        ),
        stat_panel(
            2,
            "Cache hit ratio",
            6,
            1,
            6,
            4,
            "sum(rate(pg_stat_database_blks_hit[5m])) / (sum(rate(pg_stat_database_blks_hit[5m])) + sum(rate(pg_stat_database_blks_read[5m])))",
            unit="percentunit",
            decimals=3,
            thresholds=[
                {"color": "red", "value": None},
                {"color": "green", "value": 0.95},
            ],
        ),
        stat_panel(
            3,
            "Instances (replication: single node)",
            12,
            1,
            6,
            4,
            "count(pg_settings_max_connections)",
            unit="none",
            decimals=0,
            thresholds=[{"color": "blue", "value": None}],
            desc="Deliberately explicit: replication lag panels are n/a because the lab runs a single instance.",
        ),
        stat_panel(
            4,
            "Longest running transaction",
            18,
            1,
            6,
            4,
            "max(pg_stat_activity_max_tx_duration)",
            unit="s",
            decimals=1,
            thresholds=[
                {"color": "green"},
                {"color": "orange", "value": 60},
                {"color": "red", "value": 300},
            ],
        ),
        ts_panel(
            5,
            "Connections by state",
            0,
            5,
            12,
            7,
            [
                {
                    "expr": 'sum by (state) (pg_stat_activity_count{datname!~"template0|template1"})',
                    "legendFormat": "{{state}}",
                    "refId": "A",
                }
            ],
            unit="none",
        ),
        ts_panel(
            6,
            "Transactions — commit vs rollback",
            12,
            5,
            12,
            7,
            [
                {
                    "expr": "sum(rate(pg_stat_database_xact_commit[5m]))",
                    "legendFormat": "commit",
                    "refId": "A",
                },
                {
                    "expr": "sum(rate(pg_stat_database_xact_rollback[5m]))",
                    "legendFormat": "rollback",
                    "refId": "B",
                },
            ],
            overrides=[
                {
                    "matcher": {"id": "byName", "options": "commit"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "green", "mode": "fixed"}}
                    ],
                },
                {
                    "matcher": {"id": "byName", "options": "rollback"},
                    "properties": [
                        {"id": "color", "value": {"fixedColor": "red", "mode": "fixed"}}
                    ],
                },
            ],
        ),
        ts_panel(
            7,
            "Database sizes",
            0,
            12,
            12,
            7,
            [
                {
                    "expr": 'pg_database_size_bytes{datname!~"template0|template1"}',
                    "legendFormat": "{{datname}}",
                    "refId": "A",
                }
            ],
            unit="bytes",
        ),
        ts_panel(
            8,
            "Longest transaction by database",
            12,
            12,
            12,
            7,
            [
                {
                    "expr": 'max by (datname) (pg_stat_activity_max_tx_duration{datname!~"template0|template1"})',
                    "legendFormat": "{{datname}}",
                    "refId": "A",
                }
            ],
            unit="s",
        ),
        row_panel(101, "Elasticsearch — retrieval index (GD-41)", 19),
        stat_panel(
            9,
            "Cluster health",
            0,
            20,
            6,
            4,
            'max(elasticsearch_cluster_health_status{color="green"})',
            unit="none",
            decimals=0,
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            desc="1 = green. Yellow/red for 5m fires LabElasticsearchNotGreen.",
        ),
        stat_panel(
            10,
            "JVM heap used",
            6,
            20,
            6,
            4,
            'elasticsearch_jvm_memory_used_bytes{area="heap"}',
            unit="bytes",
            decimals=1,
            thresholds=[{"color": "blue", "value": None}],
        ),
        stat_panel(
            11,
            "Segments memory",
            12,
            20,
            6,
            4,
            "elasticsearch_indices_segments_memory_bytes",
            unit="bytes",
            decimals=1,
            thresholds=[{"color": "purple", "value": None}],
            desc="The retrieval index lives here — watch this before demo days (segment memory grows with indexed knowledge).",
        ),
        stat_panel(
            12,
            "Segment count",
            18,
            20,
            6,
            4,
            "elasticsearch_indices_segments_count",
            unit="none",
            decimals=0,
            thresholds=[{"color": "purple", "value": None}],
        ),
        ts_panel(
            13,
            "JVM heap used vs max",
            0,
            24,
            12,
            7,
            [
                {
                    "expr": 'elasticsearch_jvm_memory_used_bytes{area="heap"}',
                    "legendFormat": "used",
                    "refId": "A",
                },
                {
                    "expr": 'elasticsearch_jvm_memory_max_bytes{area="heap"}',
                    "legendFormat": "max",
                    "refId": "B",
                },
            ],
            unit="bytes",
        ),
        ts_panel(
            14,
            "Search & indexing rates",
            12,
            24,
            12,
            7,
            [
                {
                    "expr": "rate(elasticsearch_indices_search_query_total[5m])",
                    "legendFormat": "searches/s",
                    "refId": "A",
                },
                {
                    "expr": "rate(elasticsearch_indices_indexing_index_total[5m])",
                    "legendFormat": "indexes/s",
                    "refId": "B",
                },
            ],
        ),
        row_panel(102, "Kafka — demo stack (GD-42)", 31),
        stat_panel(
            15,
            "Brokers",
            0,
            32,
            6,
            4,
            "kafka_brokers",
            unit="none",
            decimals=0,
            thresholds=[{"color": "green", "value": None}],
        ),
        stat_panel(
            16,
            "Topics",
            6,
            32,
            6,
            4,
            "count(kafka_topic_partitions) or vector(0)",
            unit="none",
            decimals=0,
            thresholds=[{"color": "blue", "value": None}],
            desc="The demo cluster currently has no topics; the exporter is live and the series appear as soon as topics exist.",
        ),
        table_panel(
            17,
            "Consumer group lag",
            12,
            32,
            12,
            8,
            [instant("kafka_consumergroup_lag", "{{consumergroup}} / {{topic}}", "A")],
            desc="Feeds the job-queue story if Kafka is ever promoted beyond the demo stack. Empty until consumer groups exist.",
        ),
        ts_panel(
            18,
            "Partitions by topic",
            0,
            36,
            12,
            8,
            [
                {
                    "expr": "sum by (topic) (kafka_topic_partitions)",
                    "legendFormat": "{{topic}}",
                    "refId": "A",
                }
            ],
            unit="none",
        ),
    ],
)

# ═══════════════════════════════════════════════════════════════════════════════
# DASHBOARD 10 — LAB · GATEWAY (Traefik at the edge)
# ═══════════════════════════════════════════════════════════════════════════════

GATEWAY = dash(
    title="Lab · Gateway",
    uid="lab-gateway",
    description=(
        "Traefik edge view (GD-43): entrypoint traffic, latency and status codes, "
        "per-service request/error tables, and the blackbox probes as the "
        "external-reachability overlay. Metric source: the traefik PodMonitor "
        "(k8s/lab-observability.yaml)."
    ),
    tags=["lab", "gateway", "traefik", "edge"],
    panels=[
        row_panel(100, "Entrypoints", 0),
        ts_panel(
            1,
            "Requests/s by entrypoint",
            0,
            1,
            12,
            8,
            [
                {
                    "expr": "sum by (entrypoint) (rate(traefik_entrypoint_requests_total[5m]))",
                    "legendFormat": "{{entrypoint}}",
                    "refId": "A",
                }
            ],
        ),
        ts_panel(
            2,
            "p95 latency by entrypoint",
            12,
            1,
            12,
            8,
            [
                {
                    "expr": "histogram_quantile(0.95, sum by (le, entrypoint) (rate(traefik_entrypoint_request_duration_seconds_bucket[5m])))",
                    "legendFormat": "{{entrypoint}}",
                    "refId": "A",
                }
            ],
            unit="s",
        ),
        ts_panel(
            3,
            "Status codes",
            0,
            9,
            12,
            8,
            [
                {
                    "expr": "sum by (code) (rate(traefik_entrypoint_requests_total[5m]))",
                    "legendFormat": "{{code}}",
                    "refId": "A",
                }
            ],
            stack=True,
            desc="Stacked response classes at the edge; 4xx/5xx growth without matching service errors points at auth/proxy, not the app.",
        ),
        ts_panel(
            4,
            "Probe duration per target",
            12,
            9,
            12,
            8,
            [
                {
                    "expr": 'probe_duration_seconds{job=~"lab-http(-redirect)?-probes"}',
                    "legendFormat": "{{instance}}",
                    "refId": "A",
                }
            ],
            unit="s",
            desc="Blackbox overlay: how long each probed surface takes to answer. Spikes here with flat entrypoint latency mean the path, not the gateway.",
        ),
        row_panel(101, "Services behind the gateway", 17),
        table_panel(
            5,
            "Per-service traffic (requests/s by code)",
            0,
            18,
            12,
            8,
            [
                instant(
                    "lab:gateway:service:requests:rate5m",
                    "{{namespace}}/{{service}} code {{code}}",
                    "A",
                )
            ],
            unit="reqps",
            desc="Traefik service names resolved to namespace/service by the lab:gateway recording rules.",
        ),
        table_panel(
            6,
            "Per-service error ratio",
            12,
            18,
            12,
            8,
            [instant("lab:gateway:service:error_ratio:rate5m", "{{namespace}}/{{service}}", "A")],
            unit="percentunit",
        ),
        text_panel(
            7,
            "Host breakdown — design note",
            0,
            26,
            24,
            5,
            (
                "# Why there is no per-host (nip.io) panel\n\n"
                "Router labels (`traefik_router_*`) are disabled by default and enabling "
                "them adds a label per nip.io host per router. Host-level questions are "
                "answered from Loki web logs or the ingress access paths; the gateway "
                "dashboard stays on entrypoint/service granularity. If a host split "
                "becomes necessary, set `metrics.prometheus.addRoutersLabels=true` in "
                "the Traefik helm values and add a router table here."
            ),
        ),
    ],
)

DASHBOARDS = {
    "turn-outcomes.json": TURN_OUTCOMES,
    "retrieval-routing.json": RETRIEVAL_ROUTING,
    "llm-operations.json": LLM_OPS,
    "exemplar-drillthrough.json": EXEMPLAR,
    "safety-governance.json": SAFETY,
    "lab-infra-overview.json": INFRA,
    "lab-services.json": SERVICES,
    "lab-service-drilldown.json": DRILLDOWN,
    "lab-datastores.json": DATASTORES,
    "lab-gateway.json": GATEWAY,
}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if a generated dashboard differs, without writing files",
    )
    args = parser.parse_args(argv)
    drifted: list[Path] = []
    for filename, data in DASHBOARDS.items():
        path = OUT_DIR / filename
        rendered = json.dumps(data, indent=2) + "\n"
        if args.check:
            if not path.exists() or path.read_text() != rendered:
                drifted.append(path)
            continue
        path.write_text(rendered)
        print(f"Wrote {path}")
    if drifted:
        for path in drifted:
            print(f"Generated dashboard differs: {path}")
        raise SystemExit(1)
    if args.check:
        print(f"Generated dashboards are in sync ({len(DASHBOARDS)} files)")


if __name__ == "__main__":
    main()
