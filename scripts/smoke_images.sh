#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${IMAGE_OUTPUT_DIR:-$ROOT_DIR/artifacts/images}"
IMAGE_PREFIX="${IMAGE_PREFIX:-tenantchat-local}"
VCS_REF="${IMAGE_VCS_REF:-$(git -C "$ROOT_DIR" rev-parse HEAD)}"
ALL_IMAGES=(prototype api embedding ingestion financing)
if (( $# )); then
  IMAGES=("$@")
else
  IMAGES=("${ALL_IMAGES[@]}")
fi

container=""
cleanup() {
  if [[ -n "$container" ]]; then
    docker rm --force "$container" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

health_path() {
  case "$1" in
    prototype) echo "/api/tenants" ;;
    api) echo "/healthz" ;;
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
  esac
}

mkdir -p "$OUTPUT_DIR"
for image in "${IMAGES[@]}"; do
  case "$image" in prototype|api|embedding|ingestion|financing) ;; *)
    echo "unknown image '$image'; choose: ${ALL_IMAGES[*]}" >&2
    exit 2
  esac
  tag="$IMAGE_PREFIX/$image:$VCS_REF"
  configured_user="$(docker image inspect --format '{{.Config.User}}' "$tag")"
  if [[ "$configured_user" != "10001:10001" ]]; then
    echo "$tag is configured as unexpected user '$configured_user'" >&2
    exit 1
  fi

  case "$image" in
    prototype)
      docker run --rm --entrypoint python "$tag" -c \
        'import os, pathlib, psycopg; p=pathlib.Path(os.environ["CHATS_DIR"]); (p/".smoke").write_text("ok"); (p/".smoke").unlink()'
      ;;
    api)
      docker run --rm --entrypoint sh "$tag" -c \
        'python -c "import psycopg; from tenantchat.api.app import create_app; assert create_app()" && alembic --version'
      ;;
    embedding)
      docker run --rm --entrypoint python "$tag" -c \
        'import app; assert app.MODEL_REVISION == "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"'
      ;;
    ingestion|financing)
      docker run --rm --entrypoint python "$tag" -c 'import app; assert app.app'
      ;;
  esac

  port="$(container_port "$image")"
  container="tenantchat-smoke-$image-$$"
  docker run --detach --rm --name "$container" --publish "127.0.0.1::$port" "$tag" >/dev/null
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
    exit 1
  fi
  cleanup
  container=""
  echo "smoke passed: $tag"
done
