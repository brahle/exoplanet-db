#!/usr/bin/env bash
set -euo pipefail

# Homelab standard build script
# Target registry: harbor.brahle.com
# Rule: Merge != Deploy

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION_FILE="${REPO_ROOT}/VERSION"

if [[ ! -f "${VERSION_FILE}" ]]; then
  echo "Error: VERSION file not found at ${VERSION_FILE}" >&2
  exit 1
fi

VERSION="$(tr -d '[:space:]' < "${VERSION_FILE}")"
IMAGE_TAG="v${VERSION}"
REGISTRY="harbor.brahle.com"
IMAGE_NAME="${REGISTRY}/brahle/exoplanet-db:${IMAGE_TAG}"

echo "=================================================="
echo "Building exoplanet-db"
echo "Version:  ${IMAGE_TAG}"
echo "Registry: ${REGISTRY}"
echo "Image:    ${IMAGE_NAME}"
echo "=================================================="

docker build \
  --platform linux/amd64 \
  -t "${IMAGE_NAME}" \
  -f "${REPO_ROOT}/Dockerfile" \
  "${REPO_ROOT}"

echo ""
echo "Build complete: ${IMAGE_NAME}"
echo ""
echo "To publish to Harbor (requires 'docker login harbor.brahle.com'):"
echo "  docker push ${IMAGE_NAME}"
echo ""
echo "Remember: Merge != Deploy. Update 'newTag: ${IMAGE_TAG}' in k8s kustomization to deploy."
