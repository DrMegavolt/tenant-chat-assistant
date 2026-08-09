#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
NAMESPACE="llm-chat"
PUSH_REPOSITORY="${LOCAL_K8S_PUSH_REPOSITORY:-192.168.1.89:32000/tenantchat}"
PULL_REPOSITORY="${LOCAL_K8S_PULL_REPOSITORY:-localhost:32000/tenantchat}"
RELEASE_DIR="${LOCAL_K8S_RELEASE_DIR:-$ROOT_DIR/.local/k8s}"
METADATA_DIR="$RELEASE_DIR/build-metadata"
BACKUP_DIR="$RELEASE_DIR/backups"
APP_RELEASE="$RELEASE_DIR/app.release.yaml"
MIGRATION_RELEASE="$RELEASE_DIR/api-migration-job.release.yaml"
SEED_RELEASE="$RELEASE_DIR/seed-knowledge-job.release.yaml"
# Reviewed digest used by the existing local release for oauth2-proxy v7.8.2.
OAUTH2_PROXY_DIGEST="${LOCAL_K8S_OAUTH2_PROXY_DIGEST:-sha256:6f01695a729a2f88d7bc6e1158797d3cbdc0381c358ba86e1aa5da739586b3e0}"
ALL_IMAGES=(api embedding web)
umask 077

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "required command not found: $1" >&2
    exit 1
  fi
}

dockerfile_for() {
  case "$1" in
    api) echo "services/api/Dockerfile" ;;
    embedding) echo "services/embedding/Dockerfile" ;;
    web) echo "frontend/Dockerfile" ;;
    *) echo "unknown image: $1" >&2; return 2 ;;
  esac
}

postgres_pod() {
  kubectl -n "$NAMESPACE" get pod -l app=postgres \
    -o jsonpath='{.items[0].metadata.name}'
}

backup_database() {
  local pod timestamp backup_file partial_file remote_file
  pod="$(postgres_pod)"
  timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup_file="$BACKUP_DIR/tenantchat-pre-release-$timestamp.dump"
  partial_file="$backup_file.partial"
  remote_file="/tmp/tenantchat-pre-release-$timestamp.dump"
  mkdir -p "$BACKUP_DIR"

  echo "creating pre-migration database backup"
  if ! kubectl -n "$NAMESPACE" exec "$pod" -- \
    sh -eu -c '
      archive="$1"
      trap '\''rm -f "$archive"'\'' EXIT
      pg_dump --format=custom --file="$archive" -U "$POSTGRES_USER" -d "$POSTGRES_DB"
      pg_restore --list "$archive" >/dev/null
      base64 "$archive"
    ' -- "$remote_file" | base64 --decode > "$partial_file"; then
    rm -f "$partial_file"
    return 1
  fi
  if [[ ! -s "$partial_file" ]]; then
    rm -f "$partial_file"
    return 1
  fi
  mv "$partial_file" "$backup_file"
  echo "verified pre-migration backup: $backup_file"
}

refresh_role_grants() {
  if [[ "${LOCAL_K8S_SKIP_ROLE_GRANTS:-false}" == "true" ]]; then
    echo "skipping database role grant refresh (LOCAL_K8S_SKIP_ROLE_GRANTS=true)"
    return
  fi

  local pod owner_role app_role privacy_role_exists
  pod="$(postgres_pod)"
  owner_role="$(kubectl -n "$NAMESPACE" exec "$pod" -- printenv POSTGRES_USER)"
  app_role="$(kubectl -n "$NAMESPACE" get secret postgres-credentials \
    -o jsonpath='{.data.username}' | base64 --decode)"

  if [[ "$app_role" == "$owner_role" ]]; then
    echo "runtime and PostgreSQL owner roles are identical; no separate app grants to refresh"
  else
    kubectl -n "$NAMESPACE" exec -i "$pod" -- \
      sh -eu -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --set="app_role=$1"' \
      -- "$app_role" < "$ROOT_DIR/services/api/migrations/provision_app_role.sql"
    echo "application database grants refreshed"
  fi

  privacy_role_exists="$(kubectl -n "$NAMESPACE" exec "$pod" -- \
    sh -eu -c 'psql -v ON_ERROR_STOP=1 -At -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
      "SELECT 1 FROM pg_roles WHERE rolname = '\''tenantchat_privacy'\''"')"
  if [[ "$privacy_role_exists" == "1" ]]; then
    kubectl -n "$NAMESPACE" exec -i "$pod" -- \
      sh -eu -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" --set="privacy_role=tenantchat_privacy"' \
      < "$ROOT_DIR/services/api/migrations/provision_privacy_role.sql"
    echo "privacy database grants refreshed"
  else
    echo "privacy role is not provisioned; skipping its grant refresh"
  fi
}

for command in kubectl python3 base64; do
  require_command "$command"
done
if [[ "${LOCAL_K8S_SKIP_BUILD:-false}" != "true" ]]; then
  require_command docker
  require_command git
  docker buildx version >/dev/null
fi
kubectl cluster-info >/dev/null
if ! kubectl -n "$NAMESPACE" get statefulset/postgres >/dev/null 2>&1; then
  echo "missing $NAMESPACE/statefulset/postgres; provision the base local deployment first" >&2
  exit 1
fi
kubectl -n "$NAMESPACE" rollout status statefulset/postgres --timeout=300s

"$ROOT_DIR/scripts/verify_deployment_security.py"
"$ROOT_DIR/scripts/verify_image_contracts.py"

if [[ "${LOCAL_K8S_SKIP_BUILD:-false}" == "true" ]]; then
  if [[ ! -f "$APP_RELEASE" || ! -f "$MIGRATION_RELEASE" || ! -f "$SEED_RELEASE" ]]; then
    echo "LOCAL_K8S_SKIP_BUILD=true requires existing rendered release manifests in $RELEASE_DIR" >&2
    exit 1
  fi
  echo "reusing rendered release manifests in $RELEASE_DIR"
else
  mkdir -p "$METADATA_DIR"
  VCS_REF="$(git -C "$ROOT_DIR" rev-parse HEAD)"
  SOURCE_DATE_EPOCH="$(git -C "$ROOT_DIR" show -s --format=%ct HEAD)"

  for image in "${ALL_IMAGES[@]}"; do
    dockerfile="$(dockerfile_for "$image")"
    tag="$PUSH_REPOSITORY/$image:$VCS_REF"
    metadata="$METADATA_DIR/$image.metadata.json"
    echo "building and publishing $tag"
    docker buildx build \
      --platform linux/amd64 \
      --file "$ROOT_DIR/$dockerfile" \
      --tag "$tag" \
      --output type=registry,registry.insecure=true \
      --provenance=false \
      --sbom=false \
      --build-arg "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH" \
      --build-arg "VCS_REF=$VCS_REF" \
      --metadata-file "$metadata" \
      "$ROOT_DIR"
  done

  python3 "$ROOT_DIR/scripts/render_local_k8s_release.py" \
    --metadata-dir "$METADATA_DIR" \
    --app-template "$ROOT_DIR/k8s/app.yaml" \
    --migration-template "$ROOT_DIR/k8s/api-migration-job.yaml" \
    --seed-template "$ROOT_DIR/k8s/seed-knowledge-job.yaml" \
    --app-output "$APP_RELEASE" \
    --migration-output "$MIGRATION_RELEASE" \
    --seed-output "$SEED_RELEASE" \
    --pull-repository "$PULL_REPOSITORY" \
    --oauth2-proxy-digest "$OAUTH2_PROXY_DIGEST"
fi

"$ROOT_DIR/scripts/verify_release_manifest.py" "$APP_RELEASE"
"$ROOT_DIR/scripts/verify_release_manifest.py" --images-only "$SEED_RELEASE"
kubectl apply --dry-run=client -f "$MIGRATION_RELEASE" >/dev/null
kubectl apply --dry-run=client -f "$APP_RELEASE" >/dev/null

if [[ "${LOCAL_K8S_SKIP_BACKUP:-false}" == "true" ]]; then
  echo "skipping pre-migration backup (LOCAL_K8S_SKIP_BACKUP=true)"
elif ! backup_database; then
  if [[ "${LOCAL_K8S_REQUIRE_BACKUP:-false}" == "true" ]]; then
    echo "database backup failed; migration was not started" >&2
    exit 1
  fi
  echo "WARNING: database backup failed; continuing with migration" >&2
fi
kubectl -n "$NAMESPACE" delete job tenantchat-api-migrate --ignore-not-found=true
kubectl apply -f "$MIGRATION_RELEASE"
if ! kubectl -n "$NAMESPACE" wait \
  --for=condition=complete job/tenantchat-api-migrate --timeout=900s; then
  kubectl -n "$NAMESPACE" get job tenantchat-api-migrate -o wide >&2
  kubectl -n "$NAMESPACE" logs job/tenantchat-api-migrate >&2 || true
  exit 1
fi

refresh_role_grants
"$ROOT_DIR/k8s/deploy.sh" "$APP_RELEASE" "$SEED_RELEASE"

echo "local MicroK8s release deployed successfully"
kubectl -n "$NAMESPACE" get deploy,pods -o wide
