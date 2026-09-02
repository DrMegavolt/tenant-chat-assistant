#!/usr/bin/env python3
# ruff: noqa: T201
"""Validate every generated PromQL target against a live Prometheus API.

Grafana dashboard JSON is syntactically valid even when a PromQL expression is
not. This smoke test expands the small set of Grafana variables used by the
repository, executes every unique Prometheus target as an instant query, and
reports both query errors and targets that currently return no series.
"""

from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_DIR = ROOT / "k8s" / "grafana"

VARIABLES = {
    "$__rate_interval": "5m",
    "$__interval": "5m",
    # Representative values exercise both exact-match and regex-match panels.
    "$namespace": "llm-chat",
    "$service": "chat-backend",
    "$pod": "chat-backend.*",
}


def expanded(expression: str) -> str:
    for variable, value in VARIABLES.items():
        expression = expression.replace(variable, value)
    return expression


def prometheus_targets(dashboard: dict[str, Any]) -> list[tuple[str, str, str]]:
    targets: list[tuple[str, str, str]] = []
    for panel in dashboard.get("panels", []):
        panel_ds = panel.get("datasource", {})
        if panel_ds.get("type") != "prometheus":
            continue
        for target in panel.get("targets", []):
            expression = target.get("expr")
            if expression and not target.get("hide", False):
                targets.append(
                    (panel.get("title", "untitled"), target.get("refId", "?"), expression)
                )
    return targets


def query(base_url: str, expression: str) -> dict[str, Any]:
    if urllib.parse.urlparse(base_url).scheme not in {"http", "https"}:
        raise ValueError("Prometheus URL must use http or https")
    query_string = urllib.parse.urlencode({"query": expression})
    url = f"{base_url.rstrip('/')}/api/v1/query?{query_string}"
    with urllib.request.urlopen(url, timeout=20) as response:  # noqa: S310
        return json.load(response)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prometheus-url",
        default="http://192.168.1.174:9090",
        help="Prometheus base URL (default: local MicroK8s LoadBalancer)",
    )
    args = parser.parse_args()

    checked = 0
    empty: list[str] = []
    failures: list[str] = []
    seen: set[str] = set()

    for path in sorted(DASHBOARD_DIR.glob("*.json")):
        dashboard = json.loads(path.read_text())
        for panel, ref_id, raw_expression in prometheus_targets(dashboard):
            expression = expanded(raw_expression)
            if expression in seen:
                continue
            seen.add(expression)
            checked += 1
            try:
                payload = query(args.prometheus_url, expression)
            except Exception as exc:  # network and HTTP errors need the same report
                failures.append(f"{path.name} · {panel} [{ref_id}]: {exc}")
                continue
            if payload.get("status") != "success":
                failures.append(
                    f"{path.name} · {panel} [{ref_id}]: "
                    f"{payload.get('errorType', 'query error')}: {payload.get('error', payload)}"
                )
                continue
            if not payload.get("data", {}).get("result"):
                empty.append(f"{path.name} · {panel} [{ref_id}]")

    print(f"Validated {checked} unique PromQL targets against {args.prometheus_url}")
    if empty:
        print(f"No live series for {len(empty)} valid targets (informational):")
        for item in empty:
            print(f"  - {item}")
    if failures:
        print(f"Failed targets: {len(failures)}")
        for item in failures:
            print(f"  - {item}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
