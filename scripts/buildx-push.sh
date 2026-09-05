#!/usr/bin/env bash
set -euo pipefail

# Usage:
#   scripts/buildx-push.sh <image[:tag]> [platforms]
#
# Example:
#   scripts/buildx-push.sh myrepo/qwen2api:fix-20260509 linux/amd64,linux/arm64

IMAGE_TAG="${1:-}"
PLATFORMS="${2:-linux/amd64,linux/arm64}"
BUILDER_NAME="${BUILDER_NAME:-qwen2api-multiarch}"

if [[ -z "${IMAGE_TAG}" ]]; then
  echo "Usage: $0 <image[:tag]> [platforms]" >&2
  exit 1
fi

if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
  docker buildx create --name "${BUILDER_NAME}" --use
else
  docker buildx use "${BUILDER_NAME}"
fi

docker buildx inspect --bootstrap >/dev/null

echo "[buildx] building and pushing ${IMAGE_TAG}"
echo "[buildx] platforms: ${PLATFORMS}"

docker buildx build \
  --platform "${PLATFORMS}" \
  -t "${IMAGE_TAG}" \
  --push \
  .

echo "[buildx] done: ${IMAGE_TAG}"
