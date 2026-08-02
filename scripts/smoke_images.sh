#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${IMAGE_OUTPUT_DIR:-$ROOT_DIR/artifacts/images}"
IMAGE_PREFIX="${IMAGE_PREFIX:-tenantchat-local}"
VCS_REF="${IMAGE_VCS_REF:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"
# The same pinned server the migration and repository suites use, so a passing
# smoke cannot mean "worked against whatever the floating 16 tag resolves to".
POSTGRES_IMAGE="${SMOKE_POSTGRES_IMAGE:-postgres:16.11-alpine3.23@sha256:4327b9fd295502f326f44153a1045a7170ddbfffed1c3829798328556cfd09e2}"

# Nothing to protect: this account only ever exists inside a throwaway container
# on a network that publishes no database port. Fixed rather than generated so
# reproducing a failed run never requires echoing the value.
SMOKE_DB_USER="smoke_owner"
SMOKE_DB_PASSWORD="image-smoke-test-only"
SMOKE_DB_NAME="tenantchat_smoke"

ALL_IMAGES=(prototype api embedding ingestion financing web)
if (( $# )); then
  IMAGES=("$@")
else
  IMAGES=("${ALL_IMAGES[@]}")
fi

container=""
database_container=""
network=""
database_url=""

cleanup() {
  if [[ -n "$container" ]]; then
    docker rm --force "$container" >/dev/null 2>&1 || true
    container=""
  fi
  if [[ -n "$database_container" ]]; then
    docker rm --force "$database_container" >/dev/null 2>&1 || true
    database_container=""
  fi
  if [[ -n "$network" ]]; then
    docker network rm "$network" >/dev/null 2>&1 || true
    network=""
  fi
  database_url=""
}
trap cleanup EXIT
# Signal traps terminate explicitly; returning from an INT/TERM handler would
# otherwise let Bash resume the interrupted smoke after its resources vanished.
trap 'exit 130' INT
trap 'exit 143' TERM

health_path() {
  case "$1" in
    prototype) echo "/api/tenants" ;;
    api|web) echo "/healthz" ;;
    *) echo "/health" ;;
  esac
}

container_port() {
  case "$1" in
    prototype) echo 8000 ;;
    embedding) echo 8001 ;;
    ingestion) echo 8002 ;;
    financing) echo 8003 ;;
    api) echo 8004 ;;
    web) echo 8080 ;;
  esac
}

# Give the API image the database it now requires: an isolated network with no
# published database port, so nothing outside this smoke can reach the server
# and the API resolves it by container name.
start_smoke_database() {
  local image="$1"
  network="tenantchat-smoke-net-$image-$$"
  database_container="tenantchat-smoke-db-$image-$$"

  docker network create "$network" >/dev/null
  docker run --detach --name "$database_container" --network "$network" \
    --env "POSTGRES_USER=$SMOKE_DB_USER" \
    --env "POSTGRES_PASSWORD=$SMOKE_DB_PASSWORD" \
    --env "POSTGRES_DB=$SMOKE_DB_NAME" \
    --env "POSTGRES_INITDB_ARGS=--locale=C --encoding=UTF8" \
    "$POSTGRES_IMAGE" >/dev/null
  database_url="postgresql+psycopg://$SMOKE_DB_USER:$SMOKE_DB_PASSWORD@$database_container:5432/$SMOKE_DB_NAME"

  local _attempt
  for _attempt in {1..60}; do
    if docker exec "$database_container" \
      pg_isready --username "$SMOKE_DB_USER" --dbname "$SMOKE_DB_NAME" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "smoke database never became ready" >&2
  docker logs "$database_container" >&2
  exit 1
}

mkdir -p "$OUTPUT_DIR"
for image in "${IMAGES[@]}"; do
  case "$image" in prototype|api|embedding|ingestion|financing|web) ;; *)
    echo "unknown image '$image'; choose: ${ALL_IMAGES[*]}" >&2
    exit 2
  esac
  tag="$IMAGE_PREFIX/$image:$VCS_REF"
  configured_user="$(docker image inspect --format '{{.Config.User}}' "$tag")"
  if [[ "$configured_user" != "10001:10001" ]]; then
    echo "$tag is configured as unexpected user '$configured_user'" >&2
    exit 1
  fi

  port="$(container_port "$image")"
  container="tenantchat-smoke-$image-$$"
  run_args=(--detach --rm --name "$container" --publish "127.0.0.1::$port")

  case "$image" in
    prototype)
      docker run --rm --entrypoint python "$tag" -c \
        'import os, pathlib, psycopg; p=pathlib.Path(os.environ["CHATS_DIR"]); (p/".smoke").write_text("ok"); (p/".smoke").unlink()'
      ;;
    api)
      docker run --rm --entrypoint sh "$tag" -c \
        'python -c "import psycopg, tenantchat.api.app" && alembic --version'
      start_smoke_database "$image"
      # Schema first, exactly as k8s/api-migration-job.yaml does it: the API
      # writes tenant seeds during startup and cannot create its own tables.
      docker run --rm --network "$network" \
        --env "DATABASE_MIGRATION_URL=$database_url" \
        --entrypoint alembic "$tag" upgrade head
      run_args+=(--network "$network" --env "DATABASE_URL=$database_url")
      ;;
    embedding)
      docker run --rm --entrypoint python "$tag" -c \
        'import app; assert app.MODEL_REVISION == "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"'
      ;;
    ingestion|financing)
      docker run --rm --entrypoint python "$tag" -c 'import app; assert app.app'
      ;;
    web)
      # The public document root is the boundary: an admin asset present here is
      # an admin console published to the internet.
      docker run --rm --entrypoint sh "$tag" -c \
        '! ls /srv/public | grep -q "^admin" && test -f /srv/public/embed.js && test -f /srv/admin/admin.html'
      # A malformed upstream must stop the container, not render a config that
      # rewrites or drops every proxied request.
      if docker run --rm --env "CHAT_BACKEND_ORIGIN=http://backend/path" "$tag" >/dev/null 2>&1; then
        echo "$tag accepted an upstream origin carrying a path" >&2
        exit 1
      fi
      # Resolvable but closed: nginx resolves proxy_pass hosts at startup, and
      # this smoke has no backend to point at.
      run_args+=(--env "CHAT_BACKEND_ORIGIN=http://127.0.0.1:9"
                 --env "CHAT_ADMIN_ORIGIN=http://127.0.0.1:9")
      ;;
  esac

  docker run "${run_args[@]}" "$tag" >/dev/null
  host_port="$(docker port "$container" "$port/tcp" | awk -F: 'NR == 1 {print $NF}')"
  ready=false
  for _attempt in {1..60}; do
    if curl --fail --silent --show-error "http://127.0.0.1:$host_port$(health_path "$image")" \
      >"$OUTPUT_DIR/$image.smoke.json" 2>/dev/null; then
      ready=true
      break
    fi
    sleep 1
  done
  if [[ "$ready" != true ]]; then
    docker logs "$container" >&2
    if [[ -n "$database_container" ]]; then
      docker logs "$database_container" >&2
    fi
    exit 1
  fi
  cleanup
  echo "smoke passed: $tag"
done
