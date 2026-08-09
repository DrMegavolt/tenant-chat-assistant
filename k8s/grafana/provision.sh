#!/usr/bin/env bash
# Provision Grafana dashboards from k8s/grafana/*.json as ConfigMaps.
#
# The kube-prometheus-stack Grafana sidecar discovers ConfigMaps labelled
# grafana_dashboard: "1" in the Grafana namespace and imports every .json key.
# This script creates or updates one ConfigMap per dashboard JSON file.
#
# Usage:
#   ./k8s/grafana/provision.sh [--verify]
#
# Prerequisites:
#   - kubectl targeting the cluster that runs kube-prometheus-stack
#   - Grafana in the 'observability' namespace
#   - kube-prometheus-stack helm release named 'kube-prom-stack'
#
# Idempotent: re-running replaces the ConfigMap with the latest JSON.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
NAMESPACE="${GRAFANA_NAMESPACE:-observability}"
VERIFY="${1:-}"
VERIFY_TIMEOUT_SECONDS=120
VERIFY_POLL_INTERVAL=5

EXPECTED_UIDS=(
    tenantchat-turn-outcomes
    tenantchat-retrieval-routing
    tenantchat-llm-operations
    tenantchat-exemplar-drillthrough
    tenantchat-safety-governance
)

cd "$SCRIPT_DIR"

for json_file in *.json; do
    name="${json_file%.json}"
    configmap_name="grafana-dashboard-${name//_/-}"

    echo "Provisioning $configmap_name from $json_file ..."

    kubectl create configmap "$configmap_name" \
        --from-file="$json_file" \
        --namespace "$NAMESPACE" \
        --dry-run=client -o yaml \
        | kubectl apply -f -

    kubectl label configmap "$configmap_name" \
        --namespace "$NAMESPACE" \
        grafana_dashboard="1" \
        app.kubernetes.io/part-of=tenant-chat \
        --overwrite

    echo "  Done."
done

echo ""
echo "All dashboards provisioned. Grafana sidecar will pick them up within 2 minutes."
echo "Verify:  kubectl get configmap -n $NAMESPACE -l grafana_dashboard=1"

if [[ "$VERIFY" != "--verify" ]]; then
    exit 0
fi

resolve_grafana_url() {
    local grafana_svc grafana_port
    grafana_svc="$(kubectl -n "$NAMESPACE" get svc -l app.kubernetes.io/name=grafana,app.kubernetes.io/instance=kube-prom-stack -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
    if [[ -z "$grafana_svc" ]]; then
        grafana_svc="$(kubectl -n "$NAMESPACE" get svc -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
    fi
    if [[ -z "$grafana_svc" ]]; then
        grafana_svc="prometheus-grafana"
    fi
    grafana_port="$(kubectl -n "$NAMESPACE" get svc "$grafana_svc" -o jsonpath='{.spec.ports[?(@.name=="http-web" || @.name=="http")].port}' 2>/dev/null)" || true
    if [[ -z "$grafana_port" ]]; then
        grafana_port=80
    fi
    echo "http://${grafana_svc}.${NAMESPACE}.svc.cluster.local:${grafana_port}"
}

query_dashboard_uids() {
    local grafana_url="$1"
    local grafana_pod
    grafana_pod="$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
    if [[ -z "$grafana_pod" ]]; then
        grafana_pod="$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=grafana,app.kubernetes.io/instance=kube-prom-stack -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
    fi
    if [[ -z "$grafana_pod" ]]; then
        echo "  WARNING: could not find Grafana pod; cannot verify dashboards via exec" >&2
        return 2
    fi
    kubectl -n "$NAMESPACE" exec "$grafana_pod" -- \
        wget -q -O - "${grafana_url}/api/search?type=dash-db" 2>/dev/null \
        | python3 -c "import json,sys; [print(d['uid']) for d in json.load(sys.stdin)]" 2>/dev/null
}

verify_dashboards() {
    local grafana_url
    grafana_url="$(resolve_grafana_url)"

    local elapsed=0
    while [[ $elapsed -lt $VERIFY_TIMEOUT_SECONDS ]]; do
        local found_uids
        found_uids="$(query_dashboard_uids "$grafana_url")" || {
            echo "  Waiting for Grafana to be reachable (${elapsed}s/${VERIFY_TIMEOUT_SECONDS}s)..."
            sleep "$VERIFY_POLL_INTERVAL"
            elapsed=$((elapsed + VERIFY_POLL_INTERVAL))
            continue
        }

        local missing=()
        for uid in "${EXPECTED_UIDS[@]}"; do
            if ! echo "$found_uids" | grep -qxF "$uid"; then
                missing+=("$uid")
            fi
        done

        if [[ ${#missing[@]} -eq 0 ]]; then
            echo ""
            echo "All ${#EXPECTED_UIDS[@]} dashboards verified in Grafana."
            return 0
        fi

        echo "  Waiting for ${#missing[@]} dashboard(s): ${missing[*]} (${elapsed}s/${VERIFY_TIMEOUT_SECONDS}s)"
        sleep "$VERIFY_POLL_INTERVAL"
        elapsed=$((elapsed + VERIFY_POLL_INTERVAL))
    done

    echo ""
    echo "ERROR: Timed out after ${VERIFY_TIMEOUT_SECONDS}s waiting for dashboards." >&2
    local found_uids
    found_uids="$(query_dashboard_uids "$grafana_url")" || found_uids=""
    local missing=()
    for uid in "${EXPECTED_UIDS[@]}"; do
        if ! echo "$found_uids" | grep -qxF "$uid"; then
            missing+=("$uid")
        fi
    done
    if [[ ${#missing[@]} -gt 0 ]]; then
        echo "Missing dashboards: ${missing[*]}" >&2
    fi
    return 1
}

verify_dashboards
