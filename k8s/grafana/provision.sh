#!/usr/bin/env bash
# Provision Grafana dashboards and the recording/alert rules that back the Lab
# dashboards. Correlated data sources are owned by k8s-infra Helm values and
# verified here before a dashboard rollout is accepted.
#
# The kube-prometheus-stack Grafana sidecar normally discovers ConfigMaps
# labelled grafana_dashboard: "1" in the Grafana namespace. Some local
# MicroK8s installations use a legacy API CA that newer sidecar images reject,
# so this script also stages the files in the sidecar's shared provisioning
# volume and asks Grafana to reload them through its authenticated local API.
# This keeps the ConfigMaps as desired state without weakening Kubernetes TLS.
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
    lab-infra-overview
    lab-services
    lab-service-drilldown
    lab-datastores
    lab-gateway
)

# The "Lab *" dashboards land in a dedicated Grafana folder next to the
# TenantChat ones. The dashboard sidecar reads the folder from this annotation
# (kube-prom-stack sidecar.folderAnnotation default); the TenantChat
# dashboards stay in General.
LAB_UIDS=(
    lab-infra-overview
    lab-services
    lab-service-drilldown
    lab-datastores
    lab-gateway
)

cd "$SCRIPT_DIR"

echo "Provisioning Lab PrometheusRules ..."
kubectl apply -f "$SCRIPT_DIR/../lab-prometheusrules.yaml"

for json_file in *.json; do
    name="${json_file%.json}"
    uid="$name"
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

    for lab_uid in "${LAB_UIDS[@]}"; do
        if [[ "$uid" == "$lab_uid" ]]; then
            kubectl annotate configmap "$configmap_name" \
                --namespace "$NAMESPACE" \
                grafana_folder="Lab" \
                --overwrite
        fi
    done

    echo "  Done."
done

resolve_grafana_pod() {
    local grafana_pod
    grafana_pod="$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=grafana,app.kubernetes.io/instance=kube-prom-stack -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
    if [[ -z "$grafana_pod" ]]; then
        grafana_pod="$(kubectl -n "$NAMESPACE" get pod -l app.kubernetes.io/name=grafana -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)" || true
    fi
    echo "$grafana_pod"
}

stage_dashboards_in_grafana() {
    local grafana_pod
    grafana_pod="$(resolve_grafana_pod)"
    if [[ -z "$grafana_pod" ]]; then
        echo "ERROR: could not find a Grafana pod in namespace '$NAMESPACE'." >&2
        return 1
    fi

    echo "Staging dashboards in Grafana pod $grafana_pod ..."
    for json_file in *.json; do
        kubectl -n "$NAMESPACE" cp \
            "$json_file" \
            "$grafana_pod:/tmp/dashboards/$json_file" \
            -c grafana-sc-dashboard
    done

    # The dashboard sidecar already receives these credentials from the
    # Grafana Secret. Expand them only inside the container and never print or
    # decode them in the deployment process.
    kubectl -n "$NAMESPACE" exec "$grafana_pod" -c grafana-sc-dashboard -- \
        python -c 'import base64, os, urllib.request; token = base64.b64encode((os.environ["REQ_USERNAME"] + ":" + os.environ["REQ_PASSWORD"]).encode()).decode(); request = urllib.request.Request(os.environ["REQ_URL"], data=b"", headers={"Authorization": "Basic " + token}, method="POST"); urllib.request.urlopen(request).read()'

}

stage_dashboards_in_grafana

echo ""
echo "All dashboards provisioned and Grafana reload requested."
echo "Verify:  kubectl get configmap -n $NAMESPACE -l grafana_dashboard=1"

if [[ "$VERIFY" != "--verify" ]]; then
    exit 0
fi

query_dashboard_uids() {
    local grafana_pod
    grafana_pod="$(resolve_grafana_pod)"
    if [[ -z "$grafana_pod" ]]; then
        echo "  WARNING: could not find Grafana pod; cannot verify dashboards via exec" >&2
        return 2
    fi
    kubectl -n "$NAMESPACE" exec "$grafana_pod" -c grafana-sc-dashboard -- \
        python -c 'import base64, os, urllib.request; token = base64.b64encode((os.environ["REQ_USERNAME"] + ":" + os.environ["REQ_PASSWORD"]).encode()).decode(); request = urllib.request.Request("http://localhost:3000/api/search?type=dash-db", headers={"Authorization": "Basic " + token}); print(urllib.request.urlopen(request).read().decode())' 2>/dev/null \
        | python3 -c "import json,sys; [print(d['uid']) for d in json.load(sys.stdin)]" 2>/dev/null
}

verify_datasources() {
    local grafana_pod
    grafana_pod="$(resolve_grafana_pod)"
    if [[ -z "$grafana_pod" ]]; then
        echo "ERROR: could not find Grafana pod to verify data sources." >&2
        return 1
    fi
    kubectl -n "$NAMESPACE" exec "$grafana_pod" -c grafana-sc-dashboard -- \
        python -c 'import base64, json, os, urllib.request; token = base64.b64encode((os.environ["REQ_USERNAME"] + ":" + os.environ["REQ_PASSWORD"]).encode()).decode(); headers = {"Authorization": "Basic " + token}; expected = {"prometheus": ("prometheus", ":9090"), "loki": ("loki", ":3100"), "tempo": ("tempo", ":3200"), "pyroscope": ("grafana-pyroscope-datasource", ":4040")}; failures = [];
for uid, (type_, port) in expected.items():
 request = urllib.request.Request("http://localhost:3000/api/datasources/uid/" + uid, headers=headers)
 try: data = json.load(urllib.request.urlopen(request))
 except Exception as exc: failures.append(f"{uid}: missing ({exc})"); continue
 if data.get("type") != type_ or port not in data.get("url", ""): failures.append("{}: type={} url={}".format(uid, data.get("type"), data.get("url")))
 if uid == "prometheus" and not any(item.get("datasourceUid") == "tempo" for item in data.get("jsonData", {}).get("exemplarTraceIdDestinations", [])): failures.append("prometheus: missing Tempo exemplar destination")
print("Grafana data sources verified: prometheus, loki, tempo, pyroscope" if not failures else "\n".join(failures)); raise SystemExit(bool(failures))'
}

verify_dashboards() {
    local elapsed=0
    while [[ $elapsed -lt $VERIFY_TIMEOUT_SECONDS ]]; do
        local found_uids
        found_uids="$(query_dashboard_uids)" || {
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
            verify_datasources
            return 0
        fi

        echo "  Waiting for ${#missing[@]} dashboard(s): ${missing[*]} (${elapsed}s/${VERIFY_TIMEOUT_SECONDS}s)"
        sleep "$VERIFY_POLL_INTERVAL"
        elapsed=$((elapsed + VERIFY_POLL_INTERVAL))
    done

    echo ""
    echo "ERROR: Timed out after ${VERIFY_TIMEOUT_SECONDS}s waiting for dashboards." >&2
    local found_uids
    found_uids="$(query_dashboard_uids)" || found_uids=""
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
