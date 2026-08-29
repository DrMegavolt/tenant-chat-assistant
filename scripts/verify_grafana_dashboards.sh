#!/usr/bin/env bash
# Verify that the expected Grafana dashboards are present in the deployed cluster.
#
# Queries the Grafana API inside the cluster (via `kubectl exec`) for the five
# Tenant Chat dashboard UIDs and the five Lab dashboard UIDs. Exits 0 when all
# are found, 1 otherwise.
#
# Usage:
#   ./scripts/verify_grafana_dashboards.sh
#
# Prerequisites:
#   - kubectl targeting a cluster with kube-prometheus-stack deployed
#   - Grafana pod in the observability namespace (override with GRAFANA_NAMESPACE)

set -euo pipefail

NAMESPACE="${GRAFANA_NAMESPACE:-observability}"

EXPECTED_UIDS=(
    tenantchat-turn-outcomes
    tenantchat-retrieval-routing
    tenantchat-llm-operations
    tenantchat-exemplar-drillthrough
    tenantchat-safety-governance
    lab-infra-overview
    lab-services
    lab-service-drilldown
    lab-datastores
    lab-gateway
)

resolve_grafana_pod() {
    local pod
    pod="$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=grafana,app.kubernetes.io/instance=kube-prom-stack -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
    if [[ -z "$pod" ]]; then
        pod="$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
    fi
    echo "$pod"
}

grafana_pod="$(resolve_grafana_pod)"
if [[ -z "$grafana_pod" ]]; then
    echo "FAIL: no Grafana pod found in namespace '$NAMESPACE'" >&2
    exit 1
fi
echo "Grafana pod: $grafana_pod"

found_uids="$(kubectl -n "$NAMESPACE" exec "$grafana_pod" -c grafana-sc-dashboard -- \
    python -c 'import base64, os, urllib.request; token = base64.b64encode((os.environ["REQ_USERNAME"] + ":" + os.environ["REQ_PASSWORD"]).encode()).decode(); request = urllib.request.Request("http://localhost:3000/api/search?type=dash-db", headers={"Authorization": "Basic " + token}); print(urllib.request.urlopen(request).read().decode())' 2>/dev/null \
    | python3 -c "import json,sys; [print(d['uid']) for d in json.load(sys.stdin)]" 2>/dev/null)" || {
    echo "FAIL: could not query Grafana API in pod $grafana_pod" >&2
    exit 1
}

missing=()
for uid in "${EXPECTED_UIDS[@]}"; do
    if ! echo "$found_uids" | grep -qxF "$uid"; then
        missing+=("$uid")
    fi
done

if [[ ${#missing[@]} -eq 0 ]]; then
    echo "PASS: all ${#EXPECTED_UIDS[@]} expected Grafana dashboards are present."
    exit 0
fi

echo "FAIL: ${#missing[@]} dashboard(s) missing:" >&2
for uid in "${missing[@]}"; do
    echo "  - $uid" >&2
done
echo "" >&2
echo "Found UIDs in Grafana:" >&2
echo "$found_uids" | sed 's/^/  /' >&2
exit 1
