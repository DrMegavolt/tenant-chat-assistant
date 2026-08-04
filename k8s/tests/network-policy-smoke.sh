#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SMOKE_CLIENT_IMAGE="${SMOKE_CLIENT_IMAGE:-curlimages/curl:8.11.1@sha256:c1fe1679c34d9784c1b0d1e5f62ac0a79fca01fb6377cdd33e90473c6f9f9a69}"
SMOKE_SERVER_IMAGE="${SMOKE_SERVER_IMAGE:-python:3.12.13-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b}"
suffix="$(date +%s)-$$"
target_ns="sec004-target-$suffix"
ingress_ns="sec004-ingress-$suffix"
observability_ns="sec004-observe-$suffix"
attacker_ns="sec004-attacker-$suffix"
rendered_policy="$(mktemp -t sec004-network-policy.XXXXXX.yaml)"

validate_namespace() {
  case "$1" in
    sec004-target-*|sec004-ingress-*|sec004-observe-*|sec004-attacker-*) return 0 ;;
    *) echo "refusing to manage unexpected namespace: $1" >&2; return 1 ;;
  esac
}

cleanup() {
  local namespace
  for namespace in "$target_ns" "$ingress_ns" "$observability_ns" "$attacker_ns"; do
    if validate_namespace "$namespace"; then
      kubectl delete namespace "$namespace" --ignore-not-found=true \
        --wait=true --timeout=120s >/dev/null 2>&1 || true
    fi
  done
  rm -f "$rendered_policy"
}
trap cleanup EXIT INT TERM

for namespace in "$target_ns" "$ingress_ns" "$observability_ns" "$attacker_ns"; do
  validate_namespace "$namespace"
  kubectl create namespace "$namespace" >/dev/null
done

sed \
  -e "s/namespace: llm-chat/namespace: $target_ns/g" \
  -e "s/kubernetes.io\/metadata.name: ingress/kubernetes.io\/metadata.name: $ingress_ns/g" \
  -e "s/kubernetes.io\/metadata.name: observability/kubernetes.io\/metadata.name: $observability_ns/g" \
  "$ROOT_DIR/k8s/network-policies.yaml" >"$rendered_policy"

kubectl apply -f "$rendered_policy" >/dev/null

create_server() {
  local name="$1"
  local port="$2"
  kubectl -n "$target_ns" run "$name" \
    --image="$SMOKE_SERVER_IMAGE" \
    --labels="app=$name,smoke-role=server" \
    --command -- /bin/sh -c \
    "mkdir -p /tmp/www; echo ok >/tmp/www/index.html; for listen_port in 8000 8001 8002 8003 8004 8080 8081 $port; do if [ ! -e /tmp/port-\$listen_port ]; then touch /tmp/port-\$listen_port; python -m http.server \$listen_port --directory /tmp/www & fi; done; wait" \
    >/dev/null
  kubectl -n "$target_ns" expose pod "$name" --port="$port" --target-port="$port" >/dev/null
}

create_client() {
  local namespace="$1"
  local name="$2"
  local app_label="$3"
  shift 3
  kubectl -n "$namespace" run "$name" \
    --image="$SMOKE_CLIENT_IMAGE" \
    --labels="app=$app_label,smoke-role=client" \
    --command -- /bin/sh -c 'sleep 3600' >/dev/null
  local label
  for label in "$@"; do
    kubectl -n "$namespace" label pod "$name" "$label" >/dev/null
  done
}

create_server web 8080
# One Service backs the single-port API workload; prometheus has no scrape
# policy for it because the API exposes no /metrics yet (OBS-002).
create_server chat-backend 8004
kubectl -n "$target_ns" expose pod chat-backend \
  --name=chat-admin --port=8004 --target-port=8004 >/dev/null
create_server oauth2-proxy 4180
create_server financing-agent 8003
create_server embedding-service 8001
create_server ingestion-service 8002
create_server postgres 5432
create_server elasticsearch 9200
create_server kibana 5601

create_client "$target_ns" web-client web
create_client "$target_ns" oauth2-client oauth2-proxy
create_client "$target_ns" chat-client chat-backend
create_client "$target_ns" financing-client financing-agent
create_client "$target_ns" ingestion-client ingestion-service
create_client "$target_ns" kibana-client kibana
create_client "$target_ns" seed-client seed-financing-docs app.kubernetes.io/name=seed-financing-docs
create_client "$target_ns" migration-client tenantchat-api-migrate app.kubernetes.io/name=tenantchat-api-migrate
create_client "$target_ns" kibana-bootstrap-client configure-kibana-system-user
create_client "$target_ns" random-client random-client
create_client "$ingress_ns" traefik-client traefik app.kubernetes.io/name=traefik
create_client "$observability_ns" prometheus-client prometheus app.kubernetes.io/name=prometheus
create_client "$attacker_ns" attacker-client attacker

for namespace in "$target_ns" "$ingress_ns" "$observability_ns" "$attacker_ns"; do
  if ! kubectl -n "$namespace" wait --for=condition=Ready pod --all --timeout=180s >/dev/null; then
    kubectl -n "$namespace" get pods -o wide >&2
    kubectl -n "$namespace" get events --sort-by=.lastTimestamp >&2
    exit 1
  fi
done

pod_for() {
  kubectl -n "$1" get pod -l "app=$2,smoke-role=client" \
    -o jsonpath='{.items[0].metadata.name}'
}

request() {
  local client_ns="$1"
  local client_app="$2"
  local service="$3"
  local port="$4"
  local pod
  pod="$(pod_for "$client_ns" "$client_app")"
  kubectl -n "$client_ns" exec "$pod" -- \
    curl -fsS --connect-timeout 2 --max-time 5 \
    "http://$service.$target_ns.svc.cluster.local:$port/" >/dev/null 2>&1
}

request_pod_port() {
  local client_ns="$1"
  local client_app="$2"
  local server_app="$3"
  local port="$4"
  local client_pod server_ip
  client_pod="$(pod_for "$client_ns" "$client_app")"
  server_ip="$(kubectl -n "$target_ns" get pod -l "app=$server_app,smoke-role=server" \
    -o jsonpath='{.items[0].status.podIP}')"
  kubectl -n "$client_ns" exec "$client_pod" -- \
    curl -fsS --connect-timeout 2 --max-time 5 "http://$server_ip:$port/" >/dev/null 2>&1
}

expect_allowed() {
  if ! request "$@"; then
    echo "ALLOW failed: $2 -> $3:$4" >&2
    exit 1
  fi
  echo "ALLOW passed: $2 -> $3:$4"
}

expect_denied() {
  if request "$@"; then
    echo "DENY failed: $2 unexpectedly reached $3:$4" >&2
    exit 1
  fi
  echo "DENY passed: $2 -X-> $3:$4"
}

expect_denied_pod_port() {
  if request_pod_port "$@"; then
    echo "DENY failed: $2 unexpectedly reached $3 pod port $4" >&2
    exit 1
  fi
  echo "DENY passed: $2 -X-> $3 pod port $4"
}

expect_allowed "$ingress_ns" traefik web 8080
expect_allowed "$target_ns" web chat-admin 8004
expect_allowed "$target_ns" web oauth2-proxy 4180
expect_allowed "$observability_ns" prometheus embedding-service 8001
expect_allowed "$observability_ns" prometheus ingestion-service 8002
expect_allowed "$observability_ns" prometheus financing-agent 8003
expect_allowed "$target_ns" chat-backend postgres 5432
expect_allowed "$target_ns" financing-agent embedding-service 8001
expect_allowed "$target_ns" financing-agent elasticsearch 9200
expect_allowed "$target_ns" ingestion-service embedding-service 8001
expect_allowed "$target_ns" ingestion-service elasticsearch 9200
expect_allowed "$target_ns" seed-financing-docs ingestion-service 8002
expect_allowed "$target_ns" tenantchat-api-migrate postgres 5432
expect_allowed "$target_ns" kibana elasticsearch 9200
expect_allowed "$target_ns" configure-kibana-system-user elasticsearch 9200

for service_port in \
  web:8080 chat-backend:8004 financing-agent:8003 embedding-service:8001 \
  ingestion-service:8002 postgres:5432 elasticsearch:9200 oauth2-proxy:4180; do
  expect_denied "$attacker_ns" attacker "${service_port%:*}" "${service_port#*:}"
  expect_denied "$target_ns" random-client "${service_port%:*}" "${service_port#*:}"
done
expect_denied "$ingress_ns" traefik chat-backend 8004
expect_denied "$target_ns" web postgres 5432
expect_denied "$target_ns" web financing-agent 8003
expect_denied "$target_ns" chat-backend financing-agent 8003
expect_denied "$target_ns" chat-backend ingestion-service 8002
expect_denied "$target_ns" chat-backend embedding-service 8001
expect_denied "$target_ns" chat-backend elasticsearch 9200
expect_denied "$ingress_ns" traefik financing-agent 8003
expect_denied "$observability_ns" prometheus postgres 5432
expect_denied_pod_port "$observability_ns" prometheus chat-backend 8000
expect_denied_pod_port "$observability_ns" prometheus embedding-service 8002
expect_denied_pod_port "$observability_ns" prometheus ingestion-service 8003
expect_denied_pod_port "$observability_ns" prometheus financing-agent 8001

echo "network-policy smoke test passed in disposable namespaces"
