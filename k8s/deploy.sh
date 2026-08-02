#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="llm-chat"
APP_MANIFEST="${1:-}"

if [[ -z "$APP_MANIFEST" || ! -f "$APP_MANIFEST" ]]; then
  echo "usage: $0 <release-app-manifest.yaml>" >&2
  echo "render k8s/app.yaml by replacing every REPLACE_WITH_*_DIGEST token first" >&2
  exit 2
fi
"$ROOT_DIR/scripts/verify_deployment_security.py"
"$ROOT_DIR/scripts/verify_image_contracts.py"
"$ROOT_DIR/scripts/verify_release_manifest.py" "$APP_MANIFEST"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

require_key() {
  local resource_type="$1"
  local resource_name="$2"
  local key="$3"
  local template
  template="{{if index .data \"$key\"}}present{{end}}"
  if ! kubectl -n "$NS" get "$resource_type" "$resource_name" \
    -o "go-template=$template" 2>/dev/null | grep -qx present; then
    echo "missing required $resource_type key $NS/$resource_name:$key; provision it out of band before deploy" >&2
    exit 1
  fi
}

require_key secret elastic-credentials username
require_key secret elastic-credentials password
require_key secret postgres-credentials username
require_key secret postgres-credentials password
require_key secret postgres-credentials database
require_key secret postgres-credentials databaseUrl
require_key secret postgres-migration-credentials databaseUrl
require_key secret kibana-credentials username
require_key secret kibana-credentials password
require_key secret llm-provider-credentials apiKey
require_key secret chat-to-financing-credentials token
require_key secret seed-to-ingestion-credentials token
require_key secret ingestion-to-embedding-credentials token
require_key secret financing-to-embedding-credentials token
require_key configmap llm-runtime baseUrl
require_key configmap llm-runtime model
require_key configmap llm-runtime timeoutSeconds

# The OpenTelemetry operator is a cluster prerequisite managed by platform
# automation. Fetching a mutable remote manifest during an application deploy
# would bypass image-digest review and make this release non-reproducible.
kubectl -n opentelemetry-operator-system rollout status deploy/opentelemetry-operator-controller-manager --timeout=240s
kubectl apply -f "$ROOT_DIR/k8s/otel-collector.yaml"
kubectl -n observability rollout status deploy/otel-gateway-collector --timeout=240s
kubectl -n observability delete deploy,svc,configmap,servicemonitor otel-collector --ignore-not-found=true
kubectl apply -f "$ROOT_DIR/k8s/observability-exposure.yaml"
kubectl apply -f "$ROOT_DIR/k8s/network-policies.yaml"
kubectl apply -f "$APP_MANIFEST"

kubectl -n "$NS" create configmap financing-docs \
  --from-file=apex-financing-options.md="$ROOT_DIR/docs/apex/financing/financing-options.md" \
  --from-file=clearview-financing-options.md="$ROOT_DIR/docs/clearview/financing/financing-options.md" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" delete job configure-kibana-system-user --ignore-not-found=true
kubectl apply -f "$ROOT_DIR/k8s/kibana-setup-job.yaml"
kubectl -n "$NS" wait --for=condition=complete job/configure-kibana-system-user --timeout=300s

kubectl -n "$NS" rollout status statefulset/postgres --timeout=300s
kubectl -n "$NS" rollout restart deploy/web deploy/chat-backend deploy/embedding-service deploy/ingestion-service deploy/financing-agent deploy/kibana
kubectl -n "$NS" rollout status deploy/chat-backend --timeout=180s
kubectl -n "$NS" rollout status deploy/web --timeout=180s
kubectl -n "$NS" rollout status deploy/embedding-service --timeout=900s
kubectl -n "$NS" rollout status deploy/ingestion-service --timeout=300s
kubectl -n "$NS" rollout status deploy/financing-agent --timeout=300s
kubectl -n "$NS" rollout status deploy/kibana --timeout=600s

kubectl -n "$NS" delete job seed-financing-docs --ignore-not-found=true
kubectl apply -f "$ROOT_DIR/k8s/seed-ingestion-job.yaml"
kubectl -n "$NS" wait --for=condition=complete job/seed-financing-docs --timeout=900s
