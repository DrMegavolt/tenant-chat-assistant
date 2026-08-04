#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${IMAGE_OUTPUT_DIR:-$ROOT_DIR/artifacts/images}"
IMAGE_PREFIX="${IMAGE_PREFIX:-tenantchat-local}"

ALL_IMAGES=(api embedding ingestion financing web)
if (( $# )); then
  IMAGES=("$@")
else
  IMAGES=("${ALL_IMAGES[@]}")
fi

mkdir -p "$OUTPUT_DIR"
VCS_REF="$(git -C "$ROOT_DIR" rev-parse HEAD)"
SOURCE_DATE_EPOCH="$(git -C "$ROOT_DIR" show -s --format=%ct HEAD)"

for image in "${IMAGES[@]}"; do
  case "$image" in
    api) dockerfile="services/api/Dockerfile" ;;
    embedding) dockerfile="services/embedding/Dockerfile" ;;
    ingestion) dockerfile="services/ingestion/Dockerfile" ;;
    financing) dockerfile="services/financing-agent/Dockerfile" ;;
    web) dockerfile="frontend/Dockerfile" ;;
    *)
      echo "unknown image '$image'; choose: ${ALL_IMAGES[*]}" >&2
      exit 2
      ;;
  esac
  tag="$IMAGE_PREFIX/$image:$VCS_REF"
  metadata="$OUTPUT_DIR/$image.metadata.json"
  echo "building $tag from $dockerfile"
  docker buildx build \
    --file "$ROOT_DIR/$dockerfile" \
    --tag "$tag" \
    --load \
    --provenance=false \
    --sbom=false \
    --build-arg "SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH" \
    --build-arg "VCS_REF=$VCS_REF" \
    --metadata-file "$metadata" \
    "$ROOT_DIR"
  docker image inspect "$tag" >"$OUTPUT_DIR/$image.inspect.json"
  docker image inspect --format '{{index .RepoDigests 0}}' "$tag" >"$OUTPUT_DIR/$image.digest"
  printf '%s\n' "$tag" >"$OUTPUT_DIR/$image.tag"
done

echo "image metadata written to $OUTPUT_DIR"
