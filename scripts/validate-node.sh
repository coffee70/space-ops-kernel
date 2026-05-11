#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
KERNEL_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)
WORKSPACE_ROOT=$(cd "${KERNEL_ROOT}/.." && pwd)
NODE_IMAGE="${NODE_IMAGE:-node:20.19-alpine}"

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "error: required command '$1' is not installed" >&2
    exit 1
  fi
}

require_command docker

echo "==> Node image: ${NODE_IMAGE}"

docker run --rm \
  --init \
  --user "$(id -u):$(id -g)" \
  --volume "${WORKSPACE_ROOT}:/workspace" \
  --workdir /workspace \
  --env HOME=/tmp/node-home \
  --env npm_config_cache=/tmp/npm-cache \
  "${NODE_IMAGE}" \
  sh -lc '
    set -euo pipefail

    echo
    echo "==> space-ops-platform/backend/services/agent-runtime-service"
    cd /workspace/space-ops-platform/backend/services/agent-runtime-service
    npm ci
    npm run build
    npm test

    echo
    echo "==> space-ops-platform/backend/services/model-registry-service"
    cd /workspace/space-ops-platform/backend/services/model-registry-service
    npm ci
    npm run build
    npm test

    echo
    echo "==> space-ops-apps/mission-control-ui"
    cd /workspace/space-ops-apps/mission-control-ui
    npm ci
    npm run validate
  '
