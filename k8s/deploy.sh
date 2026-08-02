#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="llm-chat"
OTEL_OPERATOR_VERSION="v0.116.0"

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

for required_secret in elastic-credentials postgres-credentials kibana-credentials; do
  if ! kubectl -n "$NS" get secret "$required_secret" >/dev/null 2>&1; then
    echo "missing required Secret $NS/$required_secret; create it out of band before deploy" >&2
    exit 1
  fi
done

kubectl apply -f "https://github.com/open-telemetry/opentelemetry-operator/releases/download/${OTEL_OPERATOR_VERSION}/opentelemetry-operator.yaml"
kubectl -n opentelemetry-operator-system rollout status deploy/opentelemetry-operator-controller-manager --timeout=240s
kubectl apply -f "$ROOT_DIR/k8s/otel-collector.yaml"
kubectl -n observability rollout status deploy/otel-gateway-collector --timeout=240s
kubectl -n observability delete deploy,svc,configmap,servicemonitor otel-collector --ignore-not-found=true
kubectl apply -f "$ROOT_DIR/k8s/observability-exposure.yaml"
kubectl apply -f "$ROOT_DIR/k8s/app.yaml"

kubectl -n "$NS" create configmap chat-backend-code \
  --from-file=server.py="$ROOT_DIR/server.py" \
  --from-file=requirements.txt="$ROOT_DIR/requirements.txt" \
  --from-file=index.html="$ROOT_DIR/index.html" \
  --from-file=app.js="$ROOT_DIR/app.js" \
  --from-file=admin.html="$ROOT_DIR/admin.html" \
  --from-file=admin.js="$ROOT_DIR/admin.js" \
  --from-file=styles.css="$ROOT_DIR/styles.css" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" create configmap embedding-service-code \
  --from-file=app.py="$ROOT_DIR/services/embedding/app.py" \
  --from-file=requirements.txt="$ROOT_DIR/services/embedding/requirements.txt" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" create configmap ingestion-service-code \
  --from-file=app.py="$ROOT_DIR/services/ingestion/app.py" \
  --from-file=requirements.txt="$ROOT_DIR/services/ingestion/requirements.txt" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" create configmap financing-agent-code \
  --from-file=app.py="$ROOT_DIR/services/financing-agent/app.py" \
  --from-file=requirements.txt="$ROOT_DIR/services/financing-agent/requirements.txt" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" create configmap financing-docs \
  --from-file=apex-financing-options.md="$ROOT_DIR/docs/apex/financing/financing-options.md" \
  --from-file=clearview-financing-options.md="$ROOT_DIR/docs/clearview/financing/financing-options.md" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" delete job configure-kibana-system-user --ignore-not-found=true
kubectl apply -f "$ROOT_DIR/k8s/kibana-setup-job.yaml"
kubectl -n "$NS" wait --for=condition=complete job/configure-kibana-system-user --timeout=300s

kubectl -n "$NS" rollout status statefulset/postgres --timeout=300s
kubectl -n "$NS" rollout restart deploy/chat-backend deploy/embedding-service deploy/ingestion-service deploy/financing-agent deploy/kibana
kubectl -n "$NS" rollout status deploy/chat-backend --timeout=180s
kubectl -n "$NS" rollout status deploy/embedding-service --timeout=900s
kubectl -n "$NS" rollout status deploy/ingestion-service --timeout=300s
kubectl -n "$NS" rollout status deploy/financing-agent --timeout=300s
kubectl -n "$NS" rollout status deploy/kibana --timeout=600s

kubectl -n "$NS" delete job seed-financing-docs --ignore-not-found=true
kubectl apply -f "$ROOT_DIR/k8s/seed-ingestion-job.yaml"
kubectl -n "$NS" wait --for=condition=complete job/seed-financing-docs --timeout=900s
