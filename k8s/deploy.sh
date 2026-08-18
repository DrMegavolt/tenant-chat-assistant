#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NS="llm-chat"
APP_MANIFEST="${1:-}"
SEED_MANIFEST="${2:-}"

if [[ -z "$APP_MANIFEST" || ! -f "$APP_MANIFEST" || -z "$SEED_MANIFEST" || ! -f "$SEED_MANIFEST" ]]; then
  echo "usage: $0 <release-app-manifest.yaml> <release-seed-manifest.yaml>" >&2
  echo "render both release templates with immutable API/image digests first" >&2
  exit 2
fi
"$ROOT_DIR/scripts/verify_deployment_security.py"
"$ROOT_DIR/scripts/verify_image_contracts.py"
"$ROOT_DIR/scripts/verify_release_manifest.py" "$APP_MANIFEST"
"$ROOT_DIR/scripts/verify_release_manifest.py" --images-only "$SEED_MANIFEST"

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

require_present_key() {
  local resource_type="$1"
  local resource_name="$2"
  local key="$3"
  local template
  template="{{range \$data_key, \$data_value := .data}}{{if eq \$data_key \"$key\"}}present{{end}}{{end}}"
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
require_key secret privacy-database-credentials databaseUrl
require_key secret kibana-credentials username
require_key secret kibana-credentials password
require_key secret llm-provider-credentials apiKey
require_key secret ingestion-to-embedding-credentials token
require_key secret chat-to-embedding-credentials token
require_key configmap llm-runtime baseUrl
require_key configmap llm-runtime model
require_key configmap llm-runtime timeoutSeconds

# OIDC authentication secrets for oauth2-proxy (SEC-001 single-origin gateway).
require_key secret oidc-credentials clientId
require_key secret oidc-credentials clientSecret
require_key secret oidc-credentials issuerUrl
require_key secret oidc-credentials cookieSecret
# Browser-facing and backchannel OIDC endpoints. Discovery is off, so a missing
# key here means oauth2-proxy starts with no login URL at all.
require_key configmap oidc-endpoints loginUrl
require_key configmap oidc-endpoints redeemUrl
require_key configmap oidc-endpoints jwksUrl
require_key configmap oidc-endpoints profileUrl
require_key configmap oidc-endpoints redirectUrl
require_present_key configmap widget-cors-origins origins

# Admin CSRF secret used by Python to validate double-submit tokens.
require_key secret admin-csrf-secret secret
require_key secret admin-gateway-credentials token

# SEC-002: signs the visitor credentials the widget exchanges for identity;
# without it the visitor routes fail closed at startup.
require_key secret visitor-credential-signing-key key

# The OpenTelemetry operator is a cluster prerequisite managed by platform
# automation. Fetching a mutable remote manifest during an application deploy
# would bypass image-digest review and make this release non-reproducible.
kubectl -n opentelemetry-operator-system rollout status deploy/opentelemetry-operator-controller-manager --timeout=240s
kubectl apply -f "$ROOT_DIR/k8s/otel-collector.yaml"
kubectl -n observability rollout status deploy/otel-gateway-collector --timeout=240s
kubectl -n observability delete deploy,svc,configmap,servicemonitor otel-collector --ignore-not-found=true
kubectl apply -f "$ROOT_DIR/k8s/observability-exposure.yaml"
kubectl apply -f "$ROOT_DIR/k8s/public-loadbalancers.yaml"
kubectl apply -f "$ROOT_DIR/k8s/network-policies.yaml"
kubectl apply -f "$APP_MANIFEST"

kubectl -n "$NS" delete job configure-kibana-system-user --ignore-not-found=true
kubectl apply -f "$ROOT_DIR/k8s/kibana-setup-job.yaml"
kubectl -n "$NS" wait --for=condition=complete job/configure-kibana-system-user --timeout=300s

kubectl -n "$NS" rollout status statefulset/postgres --timeout=300s
# The API rolls out and becomes ready before the widget bundle that reads it.
# The widget renders fields the API publishes — the consent statement is one
# (`BUG-023`) — so a new bundle served by an older API renders them empty. The
# reverse order is safe: an older bundle ignores a field it does not know about.
kubectl -n "$NS" rollout restart deploy/oauth2-proxy deploy/chat-backend deploy/job-worker deploy/embedding-service deploy/kibana
kubectl -n "$NS" rollout status deploy/oauth2-proxy --timeout=180s
kubectl -n "$NS" rollout status deploy/chat-backend --timeout=180s
kubectl -n "$NS" rollout status deploy/job-worker --timeout=180s
kubectl -n "$NS" rollout restart deploy/web
kubectl -n "$NS" rollout status deploy/web --timeout=180s
# These names belonged to the former split-port gateway. Applying the new
# manifests does not prune renamed resources, so remove them only after the
# authenticated single-origin gateway is ready.
kubectl -n "$NS" delete svc/web-admin --ignore-not-found=true
kubectl -n "$NS" delete networkpolicy/allow-public-ingress-to-chat --ignore-not-found=true
kubectl -n "$NS" rollout status deploy/embedding-service --timeout=900s
kubectl -n "$NS" rollout status deploy/kibana --timeout=600s

kubectl -n "$NS" delete job seed-knowledge --ignore-not-found=true
kubectl apply -f "$SEED_MANIFEST"
kubectl -n "$NS" wait --for=condition=complete job/seed-knowledge --timeout=900s

echo ""
echo "provisioning Grafana dashboards..."
"$ROOT_DIR/k8s/grafana/provision.sh" --verify
