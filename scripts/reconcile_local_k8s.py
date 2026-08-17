#!/usr/bin/env python3
"""Reconcile the deployed namespace against the current release's object set.

The local release deletes nothing, and `kubectl apply` cannot delete objects a
newer release dropped. A Service or ServiceMonitor that an old release created
then survives into the new deployment: BUG-014's orphan `chat-backend` Service
pointed at port 8000 (nothing listens) and stayed a permanently-down Prometheus
target, and the `financing-agent`/`ingestion-service` monitors kept scraping
services `DEP-001` had removed.

This check fails instead of silently tolerating that drift, so a fresh cluster
cannot accumulate the same orphans:

1. every Service in the namespace has at least one ready endpoint (an orphan
   Service's endpoints are empty), and
2. every ServiceMonitor's selector matches at least one Service.

Run it after `make deploy-local`; the release target calls it as its final
verification step. Deleting the named orphans is a one-time manual step on
clusters that already drifted; this script only reports.

Environment:
    NAMESPACE          namespace to reconcile (default llm-chat)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from typing import Any


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


NAMESPACE = _env("NAMESPACE", "llm-chat")

_KUBECTL = shutil.which("kubectl")
if _KUBECTL is None:
    sys.stderr.write("kubectl is required on PATH\n")
    raise SystemExit(2)
KUBECTL: str = _KUBECTL


def _objects(kind: str) -> list[dict[str, Any]]:
    """One kind's objects in the namespace, as JSON, via kubectl.

    The argv is a fixed literal and `kind` is called only with constants from
    :func:`reconcile`; no value from the cluster reaches the command line.
    """
    completed = subprocess.run(  # noqa: S603 - fixed argv, nothing untrusted
        [KUBECTL, "-n", NAMESPACE, "get", kind, "-o", "json"],
        check=True,
        capture_output=True,
        text=True,
    )
    return list(json.loads(completed.stdout).get("items", []))


def _service_ready_endpoints(name: str) -> int:
    """The ready endpoint count of one Service, via the EndpointSlice API."""
    completed = subprocess.run(  # noqa: S603 - fixed argv, nothing untrusted
        [
            KUBECTL,
            "-n",
            NAMESPACE,
            "get",
            "endpointslice",
            "-l",
            f"kubernetes.io/service-name={name}",
            "-o",
            "json",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    ready = 0
    for slice_item in json.loads(completed.stdout).get("items", []):
        for endpoint in slice_item.get("endpoints", []):
            conditions = endpoint.get("conditions") or {}
            if conditions.get("ready", True) is True:
                ready += 1
    return ready


def _selector_matches(selector: dict[str, Any], services: list[dict[str, Any]]) -> bool:
    """Whether any Service's labels satisfy the monitor's label selector."""
    for service in services:
        labels = service.get("metadata", {}).get("labels") or {}
        if all(labels.get(key) == value for key, value in selector.items()):
            return True
    return False


def reconcile() -> list[str]:
    """Return every drift finding; an empty list means the namespace is clean."""
    findings: list[str] = []
    services = _objects("service")
    monitors = _objects("servicemonitor")

    for service in services:
        name = service["metadata"]["name"]
        spec = service.get("spec") or {}
        if spec.get("clusterIP") in ("None", ""):
            # Headless or externally defined services have no endpoints to hold.
            continue
        if _service_ready_endpoints(name) == 0:
            findings.append(
                f"Service {NAMESPACE}/{name} has no ready endpoints; "
                "it is either orphaned or pointing at a port nothing listens on"
            )

    for monitor in monitors:
        selector = (monitor.get("spec") or {}).get("selector", {}).get("matchLabels")
        if not isinstance(selector, dict) or not selector:
            continue
        if not _selector_matches(selector, services):
            findings.append(
                f"ServiceMonitor {NAMESPACE}/{monitor['metadata']['name']} selects no "
                f"Service ({selector}); its scrape target is permanently down"
            )
    return findings


def main() -> int:
    if NAMESPACE == "":
        sys.stderr.write("NAMESPACE must not be empty\n")
        return 2
    try:
        findings = reconcile()
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        sys.stderr.write(f"ERROR: could not read {NAMESPACE} state: {exc}\n")
        return 2
    if findings:
        for finding in findings:
            sys.stderr.write(f"DRIFT: {finding}\n")
        sys.stderr.write(
            "Delete the orphaned objects (one-time cleanup) before the next release;\n"
            "kubectl apply will not remove them on its own.\n"
        )
        return 1
    sys.stdout.write(f"namespace {NAMESPACE} reconciled: no orphaned Services or monitors\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
