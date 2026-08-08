#!/usr/bin/env bash
# Provision Grafana dashboards from k8s/grafana/*.json as ConfigMaps.
#
# The kube-prometheus-stack Grafana sidecar discovers ConfigMaps labelled
# grafana_dashboard: "1" in the Grafana namespace and imports every .json key.
# This script creates or updates one ConfigMap per dashboard JSON file.
#
# Usage:
#   ./k8s/grafana/provision.sh
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
